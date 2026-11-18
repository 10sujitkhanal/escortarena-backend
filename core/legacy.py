"""Moving users and ads from escortside.dk to EscortArea.

1. `python manage.py import_escortside --wipe` reads the two admin exports (data/escortside/users/ and ads/, each a
   data.csv plus one folder of pictures per user/ad) and creates every user as an advertiser with one ad on the Free
   plan: text, categories, services, regions, pictures and videos. Ads with a title, age and picture go live with no
   end date; the rest stay drafts. Nobody can sign in until the owner claims the account (no password yet).
2. `--send` e-mails each owner a personal link (/welcome-back?token=…). The reward clock starts at that e-mail.
3. Opening the link, choosing a password and confirming 18+ claims the account. How soon decides the free plan:
      within 1 month  -> VIP for 2 months
      within 2 months -> Premium for 2 months
      within 3 months -> Standard for 2 months
      later           -> Free
   A live ad gets the plan at once; a draft keeps it waiting (pending_tier) until it is approved.
4. The same page lets the owner delete everything instead (GDPR).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import emails
from .models import Ad, AdRegion, Country, LegacyAccount, Tier, User

log = logging.getLogger(__name__)

OLD_SITE = "escortside.dk"
SALT = "escortside-claim"
LINK_MAX_AGE = timedelta(days=365)
REWARD_DAYS = 60
# (claimed within N days of the invite, plan)
REWARD_STEPS = [(30, Tier.VIP), (60, Tier.PREMIUM), (90, Tier.STANDARD)]

# Danish place names used on escortside.dk -> region slugs here
REGION_WORDS = {
    "copenhagen": ["københavn", "kobenhavn", "copenhagen", "taastrup", "rødovre", "søborg", "herlev", "frederiksberg",
                   "amager", "valby", "glostrup", "brøndby", "hvidovre", "lyngby", "ballerup"],
    "north-zealand": ["nordsjælland", "hillerød", "helsingør", "gilleleje", "frederiksund", "frederikssund", "hørsholm"],
    "rest-of-zealand": ["sydsjælland", "sjælland", "roskilde", "køge", "ringsted", "slagelse", "kalundborg", "næstved",
                        "holbæk"],
    "lolland": ["lolland", "falster", "nykøbing f"],
    "north-jutland": ["nordjylland", "aalborg", "hjørring", "frederikshavn", "hobro", "thisted"],
    "aarhus": ["aarhus", "århus", "randers", "horsens", "silkeborg", "skanderborg"],
    "mid-west-jutland": ["midtjylland", "herning", "billund", "esbjerg", "holstebro", "viborg", "ikast", "jylland"],
    "south-jutland": ["sydjylland", "kolding", "vejle", "fredericia", "frederecia", "sønderborg", "haderslev",
                      "aabenraa"],
    "funen": ["fyn", "odense", "svendborg", "nyborg", "middelfart"],
    "bornholm": ["bornholm", "rønne"],
}
WHOLE_COUNTRY = ["hele danmark", "all of denmark"]


class InvalidLink(Exception):
    pass


# ---------------------------------------------------------------------------
# Links and rewards
# ---------------------------------------------------------------------------


def claim_token(acc: LegacyAccount) -> str:
    return signing.dumps({"u": acc.user_id, "l": acc.legacy_id}, salt=SALT, compress=True)


def claim_link(acc: LegacyAccount) -> str:
    return f"/welcome-back?token={claim_token(acc)}"


def read_token(token: str) -> LegacyAccount:
    try:
        data = signing.loads(token or "", salt=SALT, max_age=LINK_MAX_AGE)
    except signing.BadSignature:
        raise InvalidLink("This link is not valid or has expired. Contact support and we'll help you.") from None
    acc = LegacyAccount.objects.select_related("user").filter(user_id=data.get("u"), legacy_id=data.get("l")).first()
    if acc is None:
        raise InvalidLink("This profile no longer exists.")
    return acc


def reward_for(acc: LegacyAccount, now: datetime | None = None) -> tuple[str, int]:
    """(plan, days) the owner gets if they claim now."""
    now = now or timezone.now()
    start = acc.invited_at or acc.created_at
    waited = (now - start).days if start else 0
    for limit, tier in REWARD_STEPS:
        if waited < limit:
            return tier, REWARD_DAYS
    return Tier.FREE, 0


def reward_deadlines(acc: LegacyAccount) -> list[dict]:
    start = acc.invited_at or acc.created_at or timezone.now()
    return [{"tier": tier, "label": Tier(tier).label, "days": REWARD_DAYS, "until": start + timedelta(days=limit)}
            for limit, tier in REWARD_STEPS]


@transaction.atomic
def claim(acc: LegacyAccount, password: str) -> LegacyAccount:
    acc = LegacyAccount.objects.select_for_update().select_related("user").get(pk=acc.pk)
    if acc.claimed_at or acc.user.legacy_accounts.filter(claimed_at__isnull=False).exists():
        raise InvalidLink("This profile was already moved. Sign in with your e-mail and password.")
    now = timezone.now()
    user = acc.user
    user.set_password(password)
    user.email_confirmed = True  # they opened the link we sent to this address
    user.is_active = True
    user.adult_confirmed_at = user.adult_confirmed_at or now
    user.save(update_fields=["password", "email_confirmed", "is_active", "adult_confirmed_at"])
    tier, days = reward_for(acc, now)
    user.legacy_accounts.filter(claimed_at__isnull=True).update(claimed_at=now, reward_tier=tier, reward_days=days)
    acc.refresh_from_db()
    if days:
        for ad in user.ads.all():
            give_plan(ad, tier, days)
    return acc


def give_plan(ad: Ad, tier: str, days: int) -> None:
    """Free plan as a thank-you: waits until the ad is approved, or starts now if it is already live."""
    now = timezone.now()
    if ad.status == Ad.Status.ACTIVE:
        start = ad.tier_until if (ad.tier == tier and ad.tier_until and ad.tier_until > now) else now
        ad.tier, ad.tier_until = tier, start + timedelta(days=days)
        ad.expires_at = max(ad.expires_at or now, ad.tier_until)
        ad.free_listing = False
        ad.grant_plan_extras()
    else:
        ad.pending_tier, ad.pending_days = tier, days
    ad.save()


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------


def send_invite(acc: LegacyAccount, *, to: str | None = None, mark: bool = True) -> None:
    """The invite e-mail. `to` sends it to another address (testing) without starting the owner's reward clock."""
    if mark and not to:
        acc.invited_at = timezone.now()
        acc.save(update_fields=["invited_at"])
    steps = reward_deadlines(acc) if acc.invited_at else [
        {"label": Tier(t).label, "until": timezone.now() + timedelta(days=d)} for d, t in REWARD_STEPS]
    fmt = "%d %B %Y"
    name = acc.user.display_name or acc.legacy_username
    emails.send(
        to=to or acc.user.email, user=acc.user, background=False, fail_silently=False,
        subject=f"{OLD_SITE} is moving – your profile is ready",
        heading=f"Hi {name}, {OLD_SITE} is moving",
        lines=[
            f"{OLD_SITE} is becoming {emails.brand()}. Your profile, pictures and ad have been moved over for you.",
            "Click the button, choose a password, and your account is yours again. Then you can change your ad, "
            "pictures and prices whenever you like.",
            "As a thank-you, the sooner you move, the better your free plan:",
            f"• Before {steps[0]['until']:{fmt}}: {steps[0]['label']} for 2 months, free",
            f"• Before {steps[1]['until']:{fmt}}: {steps[1]['label']} for 2 months, free",
            f"• Before {steps[2]['until']:{fmt}}: {steps[2]['label']} for 2 months, free",
            "After that your ad runs on the Free plan.",
            "Don't want to move? Open the same link and choose \"Delete my profile\" – we then remove everything.",
        ],
        button=("Move my profile", claim_link(acc)),
        note=f"You get this e-mail because you had a profile on {OLD_SITE} ({acc.legacy_username}). "
             "The link is personal – don't share it.",
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _bool(v: str) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes"}


def _dt(v: str) -> datetime | None:
    v = (v or "").strip()
    if not v:
        return None
    d = parse_datetime(v.replace(" ", "T", 1))
    if d and timezone.is_naive(d):
        d = timezone.make_aware(d, dt_timezone.utc)
    return d


def regions_for(text: str) -> list[str]:
    words = [w.strip().lower() for w in re.split(r"[,/;]", text or "") if w.strip()]
    if not words:
        return []
    if any(w in WHOLE_COUNTRY for w in words):
        return list(REGION_WORDS)
    found: list[str] = []
    for w in words:
        for slug, names in REGION_WORDS.items():
            if any(w == n or w.startswith(n) for n in names) and slug not in found:
                found.append(slug)
    return found


def _phone_country(dial: str) -> str:
    dial = (dial or "").strip()
    preferred = {"+1": "US", "+44": "GB", "+45": "DK", "+7": "RU"}
    if dial in preferred:
        return preferred[dial]
    c = Country.objects.filter(dial_code=dial).order_by("id").first()
    return c.code if c else "DK"




# What the old profile's tick boxes become here. Anything missing in the lookup tables is added on import.
CATEGORY_FLAGS = {  # old field -> (category slug, name)
    "escort": ("escort", "Escort"),
    "massage": ("massage", "Massage"),
    "tantra": ("tantric-massage", "Tantric massage"),
    "clinic": ("clinic", "Clinic"),
    "strip": ("strip", "Strip & shows"),
    "telephone_sex": ("phone", "Phone"),
    "webcam": ("webcam", "Webcam"),
    "event": ("event", "Events"),
    "lingerie_sale": ("lingerie-sale", "Lingerie sale"),
    "photo_video_sale": ("photo-video-sale", "Photo & video sale"),
}
GENDER_FLAGS = {"woman": ("female", "Woman"), "man": ("male", "Man"), "tran": ("trans", "Trans")}
AUDIENCE_FLAGS = {"men": ("men", "Men"), "women": ("women", "Women"), "couples": ("couples", "Couples"),
                  "tran_s": ("trans", "Trans")}
SERVICE_FLAGS = {  # old field -> service name (matched to Admin → Options → Service by name, else added)
    "sixtynine": "69", "anal": "Anal", "bdsm": "BDSM", "blowjob": "Blowjob", "cum_on_body": "Cum on Body / COB",
    "cum_on_face": "Cum on Face / COF", "danish": "Danish", "deep_throat": "Deep Throat", "dominance": "Dominance",
    "intimate_massage": "Intimate massage", "mega_french": "Mega french", "mutal_french": "Mutual french",
    "real_kiss": "Real kiss", "sadomasochism": "Sadomasochism / S/M", "vaginal": "Sex",
    "without_condom": "Sex without condom", "super_french": "Super french", "handjob": "Handjob",
    "threeway": "Threesome", "massage_service": "Massage", "tantra_massage": "Tantra massage",
    "tantra_sex": "Tantra sex", "happy_ending": "Happy ending", "lick_pussy": "Pussy licking",
}
TAG_FLAGS = {"incalls": "Incall", "outcalls": "Outcall"}
# Region used when the old profile had no area: the capital region of the phone number's country.
CAPITAL_REGION = {"DK": "copenhagen", "IT": "it-lazio", "GB": "gb-england", "DE": "de-state-of-berlin",
                  "SE": "se-stockholm", "ES": "es-madrid", "US": "us-new-york", "BG": "bg-sofia-capital",
                  "BE": "be-brussels-capital"}
NEVER = datetime(2100, 1, 1, tzinfo=dt_timezone.utc)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".mov"}


class Lookups:
    """Categories, options, tags and regions, created on demand so every old tick box has a home."""

    def __init__(self):
        from .models import AdCategory, ChoiceOption, Tag
        self.AdCategory, self.ChoiceOption, self.Tag = AdCategory, ChoiceOption, Tag
        self.added: list[str] = []
        self._cats = set(AdCategory.objects.values_list("slug", flat=True))
        self._opts = {(o.group, o.value): o for o in ChoiceOption.objects.all()}
        self._services = {self._key(o.label): o.label for o in ChoiceOption.objects.filter(group="service")}
        self._countries_on: set[str] = set()

    @staticmethod
    def _key(label: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", label.lower())

    def category(self, slug: str, name: str) -> str:
        if slug not in self._cats:
            self.AdCategory.objects.create(slug=slug, name=name, sort_order=self.AdCategory.objects.count())
            self._cats.add(slug)
            self.added.append(f"category {name}")
        return slug

    def option(self, group: str, value: str, label: str) -> str:
        if (group, value) not in self._opts:
            n = self.ChoiceOption.objects.filter(group=group).count()
            self._opts[(group, value)] = self.ChoiceOption.objects.create(group=group, value=value, label=label,
                                                                          sort_order=n)
            self.added.append(f"{group} option {label}")
        return value

    def service(self, name: str) -> str:
        key = self._key(name)
        if key not in self._services:
            used = set(self.ChoiceOption.objects.filter(group="service").values_list("value", flat=True))
            n = len(used) + 1
            while f"s{n}" in used:
                n += 1
            self.option("service", f"s{n}", name)
            self._services[key] = name
        return self._services[key]

    def tag(self, name: str):
        tag = self.Tag.objects.filter(slug=re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")).first()
        if tag is None:
            tag = self.Tag.objects.create(name=name)
            self.added.append(f"tag {name}")
        return tag

    def regions(self, areas: str, country: str) -> list[str]:
        found = [r for r in regions_for(areas) if AdRegion.objects.filter(slug=r).exists()]
        if not found:
            slug = CAPITAL_REGION.get(country)
            region = (AdRegion.objects.filter(slug=slug).first() if slug else None) or \
                AdRegion.objects.filter(country__code=country).order_by("sort_order", "name").first()
            if region is None:  # no regions for this country yet: one for the whole country
                c = Country.objects.get(code=country)
                region = AdRegion.objects.create(country=c, slug=f"{country.lower()}-all", name=c.name)
                self.added.append(f"region {c.name}")
            found = [region.slug]
        for code in set(AdRegion.objects.filter(slug__in=found).values_list("country__code", flat=True)):
            if code not in self._countries_on:
                if Country.objects.filter(code=code, has_ads=False).update(has_ads=True, is_active=True):
                    self.added.append(f"ads switched on for {code}")
                self._countries_on.add(code)
        return found


def wipe_ads_and_users() -> dict:
    """Delete every ad and every user except staff/superusers (the admins keep their login)."""
    from .models import Order
    users = User.objects.filter(is_staff=False, is_superuser=False)
    out = {"ads": Ad.objects.count(), "users": users.count()}
    with transaction.atomic():
        Order.objects.filter(user__in=users).delete()  # orders protect their user
        Ad.objects.all().delete()
        users.delete()
    return out


def _media_dirs(root) -> dict[str, list]:
    """'123_Name' (users) or 'ad45_Name' (ads) folders -> {id: [folder, ...]}."""
    from pathlib import Path
    out: dict[str, list] = {}
    root = Path(root) if root else None
    if root and root.is_dir():
        for d in root.iterdir():
            m = re.match(r"(?:ad)?(\d+)_", d.name)
            if d.is_dir() and m:
                out.setdefault(m.group(1), []).append(d)
    return out


def _media_files(folders: list) -> tuple[list, list]:
    """Pictures (profile picture first) and videos from the 'images' sub-folders, without duplicates."""
    import hashlib
    seen, pics, vids = set(), [], []
    files = [f for d in folders for f in (d / "images").glob("*") if f.is_file()]

    def order(f):
        m = re.match(r"(profile|photo)_(\d+)", f.name)
        return (0 if f.name.startswith("profile") else 1, int(m.group(2)) if m else 0, f.name)

    for f in sorted(files, key=order):
        digest = hashlib.md5(f.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        ext = f.suffix.lower()
        if ext in IMAGE_EXT:
            pics.append(f)
        elif ext in VIDEO_EXT:
            vids.append(f)
    return pics, vids


def _add_media(ad: Ad, pics: list, vids: list) -> tuple[int, int, list[str]]:
    from django.conf import settings
    from django.core.files.base import File

    from .images import process_upload
    from .membership import mp4_duration
    from .models import AdPicture, AdVideo

    problems, n_pic, n_vid = [], 0, 0
    for f in pics[: settings.MAX_PICTURES_PER_AD]:
        try:
            with f.open("rb") as fh:
                upload = File(fh, name=f.name)
                content, w, h = process_upload(upload)
        except Exception as exc:  # noqa: BLE001 – a damaged picture is skipped, not fatal
            problems.append(f"{f.name}: {exc}")
            continue
        pic = AdPicture(ad=ad, width=w, height=h, position=n_pic)
        pic.image.save(content.name, content, save=True)
        n_pic += 1
    for f in vids:
        if n_vid >= AdVideo.MAX_PER_AD:
            break
        try:
            with f.open("rb") as fh:
                seconds = mp4_duration(fh)
                if seconds > AdVideo.MAX_SECONDS + 0.5 or seconds < 1 or f.stat().st_size > 50 * 1024 * 1024:
                    problems.append(f"{f.name}: video longer than {AdVideo.MAX_SECONDS} s or too big – skipped")
                    continue
                fh.seek(0)
                ext = f.suffix.lower().lstrip(".")
                video = AdVideo(ad=ad, duration=seconds, size=f.stat().st_size, position=n_vid,
                                content_type="video/quicktime" if ext == "mov" else "video/mp4")
                video.file.save(f"video.{ext}", File(fh), save=True)
                n_vid += 1
        except Exception as exc:  # noqa: BLE001 – a broken video must not stop the import
            problems.append(f"{f.name}: {exc}")
    return n_pic, n_vid, problems


def import_rows(user_rows: list[dict], ad_rows: list[dict], *, include_inactive: bool = True,
                user_media=None, ad_media=None, go_live: bool = True, log_line=None) -> dict:
    """Create/update users and one ad each (Free plan). Safe to run again (matched on the escortside.dk user id).

    With media folders, the pictures and videos are added; an ad with a title and at least one picture goes live on
    the Free plan with no end date (go_live), the rest stay drafts the owner finishes after claiming."""
    stats = {"users_created": 0, "users_updated": 0, "users_merged": 0, "ads_created": 0, "ads_live": 0,
             "pictures": 0, "videos": 0, "skipped": [], "problems": [], "added": []}
    ads_by_user = {}
    for row in ad_rows:
        ads_by_user.setdefault(row.get("user", ""), row)  # newest first in the export
    lookups = Lookups()
    user_dirs, ad_dirs = _media_dirs(user_media), _media_dirs(ad_media)

    for row in user_rows:
        legacy_id = int(row["id"])
        email = (row.get("email") or "").strip().lower()
        username = (row.get("username") or "").strip()
        if _bool(row.get("is_deleted")):
            stats["skipped"].append((username, "deleted on the old site"))
            continue
        if _bool(row.get("is_staff")) or _bool(row.get("is_superuser")):
            stats["skipped"].append((username, "staff account"))
            continue
        if not include_inactive and not _bool(row.get("is_active")):
            stats["skipped"].append((username, "inactive on the old site"))
            continue
        if not email or "@" not in email:
            stats["skipped"].append((username, "no e-mail"))
            continue

        with transaction.atomic():
            acc = LegacyAccount.objects.select_related("user").filter(legacy_id=legacy_id).first()
            ad_row = ads_by_user.get(username)
            if acc:
                user = acc.user
                stats["users_updated"] += 1
            else:
                user = User.objects.filter(email__iexact=email).first()
                if user is not None:
                    stats["users_merged"] += 1  # same e-mail as another old account: one account, both ads
                else:
                    user = User(email=email)
                    user.set_unusable_password()
                    stats["users_created"] += 1
                acc = LegacyAccount(user=user, legacy_id=legacy_id)
            if not user.pk or not user.is_advertiser or not user.display_name:
                user.display_name = user.display_name or username[:60]
                user.is_advertiser = True
            user.phone_country = _phone_country(row.get("contact_country_code"))
            user.phone = user.phone or (row.get("contact") or "").strip()[:30]
            if _bool(row.get("is_profile_verified")):
                user.age_verified = True  # checked on escortside.dk
            joined = _dt(row.get("date_joined"))
            if joined and not user.pk:
                user.date_joined = joined
            user.save()
            acc.user = user
            acc.legacy_username = username[:150]
            acc.legacy_ad_id = int(ad_row["id"]) if ad_row and ad_row.get("id") else None
            acc.data = {"user": row, "ad": ad_row}
            acc.save()

            if acc.ad_id is not None:
                continue
            ad = _create_ad(user, row, lookups)
            acc.ad = ad
            acc.save(update_fields=["ad"])
            stats["ads_created"] += 1
            folders = user_dirs.get(str(legacy_id)) or (ad_dirs.get(str(acc.legacy_ad_id)) if acc.legacy_ad_id else None)
            if folders:
                n_pic, n_vid, problems = _add_media(ad, *_media_files(folders))
                stats["pictures"] += n_pic
                stats["videos"] += n_vid
                stats["problems"] += [f"{username}: {p}" for p in problems]
            if go_live and ad.title and ad.age and ad.pictures.exists():
                ad.status, ad.expires_at, ad.free_listing = Ad.Status.ACTIVE, NEVER, True
                ad.save()  # goes live on the Free plan (no end date)
                stats["ads_live"] += 1
        if log_line:
            log_line(f"  {username}: done")
    stats["added"] = lookups.added
    return stats


def _create_ad(user: User, row: dict, lookups: Lookups | None = None) -> Ad:
    lookups = lookups or Lookups()
    title = (row.get("title") or "").strip() or (row.get("username") or "My ad").strip()
    age = (row.get("age") or "").strip()
    flags = {k for k, v in row.items() if _bool(v)}
    categories = [lookups.category(*CATEGORY_FLAGS[f]) for f in CATEGORY_FLAGS if f in flags] or ["escort"]
    gender = next((lookups.option("gender", *GENDER_FLAGS[f]) for f in GENDER_FLAGS if f in flags), "")
    audiences = [lookups.option("audience", *AUDIENCE_FLAGS[f]) for f in AUDIENCE_FLAGS if f in flags]
    services = [{"name": lookups.service(name), "price": ""} for f, name in SERVICE_FLAGS.items() if f in flags][:20]
    access = "yes" if "handicap_friendly_yes" in flags else "partly" if "handicap_friendly_partly" in flags else "no"
    ad = Ad.objects.create(
        owner=user,
        title=title[:80],
        description=(row.get("bio") or "").strip()[:20000],
        category=categories[0],
        categories=categories,
        gender=gender,
        audiences=audiences,
        services=services,
        accessibility=access,
        regions=lookups.regions(row.get("areas", ""), user.phone_country),
        age=int(age) if age.isdigit() and 18 <= int(age) <= 99 else None,
        phone_country=user.phone_country,
        phone=user.phone,
        status=Ad.Status.DRAFT,
        tier=Tier.FREE,
    )
    tags = [lookups.tag(name) for f, name in TAG_FLAGS.items() if f in flags]
    if tags:
        ad.tags.add(*tags)
    return ad
