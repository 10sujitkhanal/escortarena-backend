"""Numbers and lists for the admin dashboard (core/templates/admin/dashboard.html)."""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from .models import (
    Ad,
    AdRegion,
    ContactMessage,
    Country,
    IdVerification,
    LoginEvent,
    Order,
    Report,
    SiteSettings,
    User,
    UserReport,
)

LIST_SIZE = 10
CHART_DAYS = 14


def _countries() -> dict[str, dict]:
    return {c.code: {"code": c.code, "name": c.name, "flag": c.flag} for c in Country.objects.all()}


def _link(model: str, pk) -> str:
    return reverse(f"admin:core_{model}_change", args=[pk])


def _list(name: str, query: str = "") -> str:
    return reverse(f"admin:core_{name}_changelist") + (f"?{query}" if query else "")


def build(now=None) -> dict:
    now = now or timezone.now()
    week_ago, month_ago = now - timedelta(days=7), now - timedelta(days=30)
    countries = _countries()
    region_country = dict(AdRegion.objects.values_list("slug", "country__code"))

    def country(code: str) -> dict | None:
        return countries.get(code) or ({"code": code, "name": code, "flag": ""} if code else None)

    def user_row(u: User) -> dict:
        main_ad = next(iter(getattr(u, "dash_ads", [])), None)
        code = region_country.get(main_ad.region, "") if main_ad else ""
        return {
            "id": u.pk, "url": _link("user", u.pk), "name": u.display_name or u.email.split("@")[0], "email": u.email,
            "joined": u.date_joined, "last_login": u.last_login, "logins": getattr(u, "n_logins", None),
            "advertiser": u.is_advertiser, "verified": u.age_verified, "vip": u.is_vip if not u.is_advertiser else False,
            "country": country(code or u.phone_country), "live_ads": getattr(u, "n_live", None),
            "ads": getattr(u, "n_ads", None),
        }

    live = Ad.objects.live()
    users = User.objects.filter(is_staff=False)
    advertisers = users.filter(is_advertiser=True)
    per_user = dict(n_ads=Count("ads", distinct=True), n_logins=Count("logins", distinct=True),
                    n_live=Count("ads", filter=Q(ads__status=Ad.Status.ACTIVE) & (
                        Q(ads__expires_at__isnull=True) | Q(ads__expires_at__gt=now)), distinct=True))

    def with_ads(qs):
        rows = list(qs.annotate(**per_user)[:LIST_SIZE])
        ads = {}
        for ad in Ad.objects.filter(owner__in=rows).order_by("-bumped_at", "-created_at").only("owner_id", "region"):
            ads.setdefault(ad.owner_id, []).append(ad)
        for u in rows:
            u.dash_ads = ads.get(u.pk, [])
        return [user_row(u) for u in rows]

    paid = Order.objects.filter(status=Order.Status.PAID, paid_at__gte=month_ago)
    revenue = paid.aggregate(total=Sum("amount"))["total"] or 0
    pending_ids = IdVerification.objects.filter(status=IdVerification.Status.PENDING).select_related("user")
    review_ads = SiteSettings.load().review_ads

    stats = [
        {"label": "Users", "value": users.count(), "sub": f"+{users.filter(date_joined__gte=week_ago).count()} this week",
         "url": _list("user")},
        {"label": "Advertisers", "value": advertisers.count(),
         "sub": f"+{advertisers.filter(date_joined__gte=week_ago).count()} this week", "url": _list("user", "is_advertiser__exact=1")},
        {"label": "Live ads", "value": live.count(), "sub": f"{Ad.objects.filter(status=Ad.Status.DRAFT).count()} drafts",
         "url": _list("ad", "status__exact=active")},
        {"label": "VIP members", "value": users.filter(is_advertiser=False, vip_until__gt=now).count(), "sub": "visitors with VIP",
         "url": _list("user", "is_advertiser__exact=0")},
        {"label": "Logins today", "value": LoginEvent.objects.filter(at__date=timezone.localdate(now)).count(),
         "sub": f"{LoginEvent.objects.filter(at__gte=week_ago).values('user').distinct().count()} people this week",
         "url": _list("loginevent")},
        {"label": "Paid (30 days)", "value": f"{revenue / 100:,.0f}".replace(",", " "),
         "sub": f"{paid.count()} orders · {SiteSettings.load().currency}", "url": _list("order", "status__exact=paid")},
    ]

    alerts = [
        {"label": "ID verifications waiting", "count": pending_ids.count(), "url": _list("idverification", "status__exact=pending"),
         "tone": "hot"},
        {"label": "Ads waiting for approval", "count": Ad.objects.filter(status=Ad.Status.PENDING).count(),
         "url": _list("ad", "status__exact=pending"), "tone": "warn", "hide": not review_ads},
        {"label": "Open ad reports", "count": Report.objects.filter(status=Report.Status.OPEN).count(),
         "url": _list("report", "status__exact=open"), "tone": "warn"},
        {"label": "Open user reports", "count": UserReport.objects.filter(status=UserReport.Status.OPEN).count(),
         "url": _list("userreport", "status__exact=open"), "tone": "warn"},
        {"label": "Unanswered contact messages", "count": ContactMessage.objects.filter(handled=False).count(),
         "url": _list("contactmessage", "handled__exact=0"), "tone": "info"},
    ]
    alerts = [a for a in alerts if not a.get("hide")]

    # Sign-ups per day (users and advertisers) for the little chart.
    start = timezone.localdate(now) - timedelta(days=CHART_DAYS - 1)
    joined = users.filter(date_joined__date__gte=start).values_list("date_joined", "is_advertiser")
    days = {start + timedelta(days=i): [0, 0] for i in range(CHART_DAYS)}
    for when, is_adv in joined:
        d = timezone.localdate(when)
        if d in days:
            days[d][1 if is_adv else 0] += 1
    peak = max([sum(v) for v in days.values()] + [1])
    chart = [{"day": d, "visitors": v, "advertisers": a, "h_v": round(v / peak * 100), "h_a": round(a / peak * 100)}
             for d, (v, a) in days.items()]

    latest_ads = []
    for ad in Ad.objects.select_related("owner").order_by("-created_at")[:LIST_SIZE]:
        latest_ads.append({
            "url": _link("ad", ad.pk), "title": ad.title, "owner": ad.owner.display_name or ad.owner.email,
            "owner_url": _link("user", ad.owner_id), "status": ad.get_status_display(), "status_key": ad.status,
            "live": ad.is_live if hasattr(ad, "is_live") else ad.status == Ad.Status.ACTIVE,
            "tier": ad.effective_tier, "created": ad.created_at, "country": country(region_country.get(ad.region, "")),
            "pictures": ad.pictures.count(),
        })

    return {
        "stats": stats,
        "alerts": alerts,
        "verifications": [
            {"url": _link("idverification", v.pk), "name": v.full_name or v.user.display_name, "email": v.user.email,
             "user_url": _link("user", v.user_id), "type": v.get_document_type_display(), "at": v.created_at,
             "country": country(v.user.phone_country)}
            for v in pending_ids.order_by("created_at")[:LIST_SIZE]
        ],
        "chart": chart,
        "chart_total": sum(d["visitors"] + d["advertisers"] for d in chart),
        # Advertisers with at least one ad, most recently signed in first.
        "creators": with_ads(advertisers.filter(ads__isnull=False).distinct()
                             .order_by(models_desc("last_login"), "-date_joined")),
        "latest_ads": latest_ads,
        "latest_users": with_ads(users.order_by("-date_joined")),
        "latest_advertisers": with_ads(advertisers.order_by("-date_joined")),
        "logins": [
            {"user": e.user.display_name or e.user.email, "url": _link("user", e.user_id), "at": e.at,
             "method": e.get_method_display(), "device": e.device, "country": country(e.country),
             "advertiser": e.user.is_advertiser}
            for e in LoginEvent.objects.select_related("user").order_by("-at")[:12]
        ],
        "now": now,
    }


def models_desc(field: str):
    """Newest first with empty values last (never signed in)."""
    from django.db.models import F
    return F(field).desc(nulls_last=True)
