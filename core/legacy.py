"""Moving users and ads from escortside.dk to EscortArea.

1. `python manage.py import_escortside` reads the two admin exports (data/escortside/users.csv and ads.csv) and
   creates every user as an advertiser with one draft ad on the Free plan. Nothing is public yet and nobody can
   sign in: there is no password until the owner claims the account.
2. `--send` e-mails each owner a personal link (/welcome-back?token=…). The reward clock starts at that e-mail.
3. Opening the link, choosing a password and confirming 18+ claims the account. How soon decides the free plan:
      within 1 month  -> VIP for 2 months
      within 2 months -> Premium for 2 months
      within 3 months -> Standard for 2 months
      later           -> Free
   The plan waits on the ad (pending_tier) and starts when the ad is approved, so no days are lost while the owner
   adds pictures and text.
4. The same page lets the owner delete everything instead (GDPR).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

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
            f"{OLD_SITE} is becoming {emails.brand()}. Your profile and ad have been moved over for you, "
            "but they are not public yet.",
            "Click the button, choose a password, and your account is yours again. Then add pictures and text to "
            "your ad and send it for approval.",
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
        d = timezone.make_aware(d, timezone.utc)
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


def import_rows(user_rows: list[dict], ad_rows: list[dict], *, include_inactive: bool = True) -> dict:
    """Create/update users and draft ads. Safe to run again (matched on the escortside.dk user id)."""
    stats = {"users_created": 0, "users_updated": 0, "users_merged": 0, "ads_created": 0, "skipped": []}
    ads_by_user = {}
    for row in ad_rows:
        ads_by_user.setdefault(row.get("user", ""), row)  # newest first in the export
    known_regions = set(AdRegion.objects.filter(country__code="DK").values_list("slug", flat=True))

    for row in user_rows:
        legacy_id = int(row["id"])
        email = (row.get("email") or "").strip().lower()
        username = (row.get("username") or "").strip()
        if _bool(row.get("is_deleted")):
            stats["skipped"].append((username, "deleted on the old site"))
            continue
        if _bool(row.get("is_staff")):
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

            if acc.ad_id is None:
                acc.ad = _create_ad(user, row, known_regions)
                acc.save(update_fields=["ad"])
                stats["ads_created"] += 1
    return stats


def _create_ad(user: User, row: dict, known_regions: set[str]) -> Ad:
    title = (row.get("title") or "").strip() or (row.get("username") or "My ad").strip()
    age = (row.get("age") or "").strip()
    regions = [r for r in regions_for(row.get("areas", "")) if r in known_regions]
    return Ad.objects.create(
        owner=user,
        title=title[:80],
        description="",
        category="escort",
        categories=["escort"],
        regions=regions,
        age=int(age) if age.isdigit() and 18 <= int(age) <= 99 else None,
        phone_country=user.phone_country,
        phone=user.phone,
        status=Ad.Status.DRAFT,
        tier=Tier.FREE,
    )
