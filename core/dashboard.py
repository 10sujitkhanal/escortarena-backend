"""Numbers and lists for the admin dashboard (core/templates/admin/dashboard.html)."""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from .admin_feed import verification_history

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
        {"label": "Users", "icon": "users", "tone": "violet", "value": users.count(), "sub": f"+{users.filter(date_joined__gte=week_ago).count()} this week",
         "url": _list("user")},
        {"label": "Advertisers", "icon": "star", "tone": "pink", "value": advertisers.count(),
         "sub": f"+{advertisers.filter(date_joined__gte=week_ago).count()} this week", "url": _list("user", "is_advertiser__exact=1")},
        {"label": "Live ads", "icon": "ad", "tone": "green", "value": live.count(), "sub": f"{Ad.objects.filter(status=Ad.Status.DRAFT).count()} drafts",
         "url": _list("ad", "status__exact=active")},
        {"label": "VIP visitors", "icon": "gift", "tone": "amber", "value": users.filter(is_advertiser=False, vip_until__gt=now).count(), "sub": "visitors with VIP",
         "url": _list("user", "is_advertiser__exact=0")},
        {"label": "Logins today", "icon": "login", "tone": "blue", "value": LoginEvent.objects.filter(at__date=timezone.localdate(now)).count(),
         "sub": f"{LoginEvent.objects.filter(at__gte=week_ago).values('user').distinct().count()} people this week",
         "url": _list("loginevent")},
        {"label": "Paid (30 days)", "icon": "money", "tone": "green", "value": f"{revenue / 100:,.0f}".replace(",", " "),
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

    waiting_ads = Ad.objects.filter(status=Ad.Status.PENDING).count() if review_ads else 0
    extra = _insights(now, country, region_country)
    quick = [
        {"label": "Check IDs", "icon": "id", "tone": "pink", "url": _list("idverification", "status__exact=pending"),
         "badge": pending_ids.count()},
        {"label": "Approve ads", "icon": "ad", "tone": "green", "url": _list("ad", "status__exact=pending"), "badge": waiting_ads},
        {"label": "Reports", "icon": "flag", "tone": "red", "url": _list("report", "status__exact=open"),
         "badge": Report.objects.filter(status=Report.Status.OPEN).count()
         + UserReport.objects.filter(status=UserReport.Status.OPEN).count()},
        {"label": "Messages to us", "icon": "mail", "tone": "blue", "url": _list("contactmessage", "handled__exact=0"),
         "badge": ContactMessage.objects.filter(handled=False).count()},
        {"label": "Plans & prices", "icon": "layers", "tone": "green", "url": reverse("admin:sales")},
        {"label": "New offer", "icon": "gift", "tone": "amber", "url": reverse("admin:core_offer_add")},
        {"label": "Add a price", "icon": "plus", "tone": "violet", "url": reverse("admin:core_product_add")},
        {"label": "Chats", "icon": "chat", "tone": "violet", "url": _list("conversation")},
        {"label": "Login history", "icon": "login", "tone": "blue", "url": _list("loginevent")},
        {"label": "Moved accounts", "icon": "move", "tone": "pink", "url": _list("legacyaccount")},
        {"label": "Site settings", "icon": "gear", "tone": "grey", "url": _list("sitesettings")},
        {"label": "Categories & options", "icon": "grid", "tone": "grey", "url": _list("adcategory")},
    ]
    return {
        **extra,
        "quick": quick,
        "stats": stats,
        "alerts": alerts,
        "verifications": [
            {"url": _link("idverification", v.pk), "name": v.full_name or v.user.display_name, "email": v.user.email,
             "user_url": _link("user", v.user_id), "type": v.get_document_type_display(), "at": v.created_at,
             "country": country(v.user.phone_country), "history": verification_history(v)}
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
             "method": e.get_method_display(), "device": e.device, "country": country(e.country), "ip": e.ip,
             "advertiser": e.user.is_advertiser}
            for e in LoginEvent.objects.select_related("user").order_by("-at")[:12]
        ],
        "now": now,
    }


def models_desc(field: str):
    """Newest first with empty values last (never signed in)."""
    from django.db.models import F
    return F(field).desc(nulls_last=True)


# ---------------------------------------------------------------------------
# Sales overview (Admin → Sales): every price list and what it earned, on one page
# ---------------------------------------------------------------------------

def _money(minor: int) -> str:
    return f"{(minor or 0) / 100:,.2f}".replace(",", " ")


def sales_overview(now=None) -> dict:
    from collections import Counter

    from .models import AddOnGroup, Offer, Plan, Product

    now = now or timezone.now()
    paid = Order.objects.filter(status=Order.Status.PAID)
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)

    def total(qs):
        agg = qs.aggregate(sum=Sum("amount"), n=Count("id"))
        return {"amount": _money(agg["sum"] or 0), "orders": agg["n"]}

    totals = [
        {"label": "Today", **total(paid.filter(paid_at__gte=today))},
        {"label": "Last 7 days", **total(paid.filter(paid_at__gte=now - timedelta(days=7)))},
        {"label": "Last 30 days", **total(paid.filter(paid_at__gte=now - timedelta(days=30)))},
        {"label": "All time", **total(paid)},
    ]

    per_product = {row["product"]: row for row in paid.values("product").annotate(n=Count("id"), sum=Sum("amount"))}
    month = {row["product"]: row["n"] for row in paid.filter(paid_at__gte=now - timedelta(days=30))
             .values("product").annotate(n=Count("id"))}

    def product_row(p: Product) -> dict:
        sold = per_product.get(p.pk, {})
        return {"name": p.name, "code": p.code, "days": p.get_days_display() if p.days else "", "coins": p.diamonds,
                "price": _money(p.price), "currency": p.currency, "active": p.is_active,
                "sold": sold.get("n", 0), "sold_30d": month.get(p.pk, 0), "earned": _money(sold.get("sum", 0)),
                "url": _link("product", p.pk)}

    products = list(Product.objects.order_by("sort_order", "price"))
    plans = []
    for plan in Plan.objects.order_by("sort_order"):
        rows = [product_row(p) for p in products if p.kind == Product.Kind.PLAN and p.plan_id == plan.pk]
        plans.append({"name": plan.name, "tier": plan.tier, "active": plan.is_active, "highlighted": plan.is_highlighted,
                      "url": _link("plan", plan.pk), "prices": rows})

    # Add-ons are stored on the order as [{code, ...}]: count them per code.
    addon_sold = Counter()
    for addons in paid.exclude(addons=[]).values_list("addons", flat=True):
        for a in addons or []:
            addon_sold[a.get("code", "")] += 1
    addons = []
    for g in AddOnGroup.objects.prefetch_related("options").order_by("sort_order"):
        addons.append({
            "name": g.name, "effect": g.get_effect_display(), "badge": g.badge, "active": g.is_active,
            "url": _link("addongroup", g.pk),
            "options": [{"name": o.name, "days": o.days, "price": _money(o.price), "currency": o.currency,
                         "active": o.is_active, "sold": addon_sold.get(o.code, 0)} for o in g.options.all()],
        })

    offers = [
        {"title": o.title, "discount": o.discount_percent, "applies": o.get_applies_to_display(),
         "until": None if o.no_end_date else o.ends_at, "active": o.is_active and (o.no_end_date or o.ends_at > now),
         "used": o.orders.filter(status=Order.Status.PAID).count(), "max": o.max_uses, "url": _link("offer", o.pk)}
        for o in Offer.objects.order_by("-is_active", "-starts_at")[:10]
    ]

    recent = [
        {"url": _link("order", o.pk), "id": o.order_id, "product": o.product_name, "amount": _money(o.amount),
         "currency": o.currency, "user": o.user.display_name or o.user.email, "user_url": _link("user", o.user_id),
         "at": o.paid_at, "addons": ", ".join(a.get("name", "") for a in (o.addons or []))}
        for o in paid.select_related("user").order_by("-paid_at")[:12]
    ]

    return {
        "totals": totals,
        "plans": plans,
        "addons": addons,
        "memberships": [product_row(p) for p in products if p.kind == Product.Kind.MEMBERSHIP],
        "coins": [product_row(p) for p in products if p.kind == Product.Kind.DIAMONDS],
        "offers": offers,
        "recent": recent,
        "currency": SiteSettings.load().currency,
        "links": {
            "add_price": reverse("admin:core_product_add"), "add_addon": reverse("admin:core_addongroup_add"),
            "add_offer": reverse("admin:core_offer_add"), "orders": _list("order"),
            "plans": _list("plan"), "products": _list("product"),
        },
    }


# ---------------------------------------------------------------------------
# More dashboard insight: who bought what, VIP members, reviews, reports, messages, top ads, plans ending
# ---------------------------------------------------------------------------

def _insights(now, country, region_country) -> dict:
    from .models import AdDailyView, AdReview, Product

    kind_label = {Product.Kind.PLAN: ("Ads plan", "pink"), Product.Kind.MEMBERSHIP: ("VIP plan", "amber"),
                  Product.Kind.DIAMONDS: ("Coins", "violet")}

    def who(u):
        return {"name": u.display_name or u.email, "email": u.email, "url": _link("user", u.pk)}

    purchases = []
    for o in (Order.objects.filter(status=Order.Status.PAID).select_related("user", "product", "ad")
              .order_by("-paid_at")[:LIST_SIZE]):
        label, tone = kind_label.get(o.product.kind, ("Other", "grey"))
        purchases.append({
            "url": _link("order", o.pk), "kind": label, "tone": tone, "what": o.product_name,
            "addons": ", ".join(a.get("name", "") for a in (o.addons or [])), "user": who(o.user),
            "ad": o.ad.title if o.ad_id else "", "ad_url": _link("ad", o.ad_id) if o.ad_id else "",
            "amount": f"{o.amount / 100:,.2f} {o.currency}".replace(",", " "), "at": o.paid_at,
        })

    vip_orders = {}
    for o in (Order.objects.filter(status=Order.Status.PAID, product__kind=Product.Kind.MEMBERSHIP)
              .order_by("user_id", "-paid_at")):
        vip_orders.setdefault(o.user_id, o)
    vip_members = []
    for u in User.objects.filter(is_advertiser=False, vip_until__gt=now).order_by("-vip_until")[:LIST_SIZE]:
        o = vip_orders.get(u.pk)
        vip_members.append({**who(u), "until": u.vip_until, "plan": o.product_name if o else "Given by admin",
                            "since": o.paid_at if o else None, "country": country(u.phone_country)})

    reviews = [
        {"url": _link("adreview", r.pk), "ad": r.ad.title, "ad_url": _link("ad", r.ad_id), "user": who(r.user),
         "rating": r.rating, "stars": "★" * r.rating + "☆" * (5 - r.rating), "body": r.body, "at": r.created_at,
         "hidden": r.is_hidden}
        for r in AdReview.objects.select_related("ad", "user").order_by("-created_at")[:8]
    ]

    reports = []
    for r in Report.objects.filter(status=Report.Status.OPEN).select_related("ad").order_by("-created_at")[:8]:
        reports.append({"url": _link("report", r.pk), "type": "Ad", "about": r.ad.title, "reason": r.get_reason_display(),
                        "text": r.message if hasattr(r, "message") else "", "at": r.created_at})
    for r in UserReport.objects.filter(status=UserReport.Status.OPEN).select_related("reported").order_by("-created_at")[:8]:
        reports.append({"url": _link("userreport", r.pk), "type": "User", "about": r.reported.display_name or r.reported.email,
                        "reason": r.get_reason_display(), "text": r.message, "at": r.created_at})
    reports.sort(key=lambda x: x["at"], reverse=True)

    contacts = [
        {"url": _link("contactmessage", m.pk), "name": m.name, "email": m.email, "topic": m.get_topic_display(),
         "text": m.message, "at": m.created_at, "handled": m.handled, "replies": m.n_replies}
        for m in ContactMessage.objects.annotate(n_replies=Count("replies")).order_by("handled", "-created_at")[:6]
    ]

    week_start = timezone.localdate(now) - timedelta(days=6)
    top = (AdDailyView.objects.filter(day__gte=week_start).values("ad").annotate(views=Sum("count"))
           .order_by("-views")[:8])
    ads = {a.pk: a for a in Ad.objects.filter(pk__in=[t["ad"] for t in top]).select_related("owner")}
    top_ads = [{"url": _link("ad", t["ad"]), "title": ads[t["ad"]].title, "owner": ads[t["ad"]].owner.display_name or ads[t["ad"]].owner.email,
                "views": t["views"], "tier": ads[t["ad"]].effective_tier} for t in top if t["ad"] in ads]

    ending = [
        {"url": _link("ad", a.pk), "title": a.title, "owner": who(a.owner), "tier": a.get_tier_display(), "until": a.tier_until}
        for a in (Ad.objects.filter(tier_until__gt=now, tier_until__lte=now + timedelta(days=7)).exclude(tier="free")
                  .select_related("owner").order_by("tier_until")[:8])
    ]

    week = now - timedelta(days=7)
    posters = User.objects.filter(ads__created_at__gte=week).distinct().count()
    return {
        "purchases": purchases, "vip_members": vip_members, "reviews": reviews, "open_reports": reports[:8],
        "contacts": contacts, "top_ads": top_ads, "ending": ending,
        "week": {"new_ads": Ad.objects.filter(created_at__gte=week).count(), "posters": posters,
                 "reviews": AdReview.objects.filter(created_at__gte=week).count(),
                 "vip_bought": Order.objects.filter(status=Order.Status.PAID, paid_at__gte=week,
                                                    product__kind=Product.Kind.MEMBERSHIP).count(),
                 "plans_bought": Order.objects.filter(status=Order.Status.PAID, paid_at__gte=week,
                                                      product__kind=Product.Kind.PLAN).count()},
    }
