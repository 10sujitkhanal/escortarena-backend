"""Admin notifications: one activity feed built from what already happens on the site.

Nothing extra is stored. Each admin's "seen" time lives in their session; everything newer counts as new and shows on
the bell in the header and in the dashboard feed. Chat appears only as "new conversation" – never message text.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

SEEN_KEY = "admin_seen_at"
PER_KIND = 12  # newest items fetched per kind before merging
WINDOW_DAYS = 14  # how far back the feed looks

# kind -> (label, colour key, icon name used in the templates)
KINDS = {
    "user": ("New visitor", "violet", "user"),
    "advertiser": ("New advertiser", "pink", "star"),
    "id_first": ("ID verification – first request", "pink", "id"),
    "id_again": ("ID verification – sent again after rejection", "amber", "id"),
    "id_recheck": ("ID verification – new check (was approved)", "blue", "id"),
    "ad": ("New ad", "green", "ad"),
    "ad_review": ("Ad waiting for approval", "amber", "ad"),
    "order": ("Payment received", "green", "money"),
    "report": ("Ad reported", "red", "flag"),
    "user_report": ("User reported", "red", "flag"),
    "contact": ("Contact message", "blue", "mail"),
    "chat": ("New conversation", "violet", "chat"),
    "claim": ("Moved from escortside.dk", "green", "user"),
}


def _url(name: str, *args, query: str = "") -> str:
    return reverse(f"admin:core_{name}", args=args) + (f"?{query}" if query else "")


def verification_history(v) -> dict:
    """How this ID request relates to the user's earlier ones."""
    from .models import IdVerification

    earlier = IdVerification.objects.filter(user_id=v.user_id, created_at__lt=v.created_at)
    rejected = earlier.filter(status=IdVerification.Status.REJECTED).count()
    approved = earlier.filter(status=IdVerification.Status.APPROVED).exists()
    if approved:
        return {"key": "id_recheck", "label": "New check – was approved before", "rejected": rejected, "tone": "blue"}
    if rejected:
        times = "once" if rejected == 1 else f"{rejected} times"
        return {"key": "id_again", "label": f"Sent again – rejected {times} before", "rejected": rejected, "tone": "amber"}
    return {"key": "id_first", "label": "First request", "rejected": 0, "tone": "pink"}


def events(limit: int = 30, since=None) -> list[dict]:
    """Newest first. Each: {kind, label, tone, icon, title, detail, at, url}."""
    from .models import (
        Ad,
        ContactMessage,
        Conversation,
        IdVerification,
        LegacyAccount,
        Order,
        Report,
        SiteSettings,
        User,
        UserReport,
    )

    start = since or (timezone.now() - timedelta(days=WINDOW_DAYS))
    out: list[dict] = []

    def add(kind, title, detail, at, url):
        label, tone, icon = KINDS[kind]
        out.append({"kind": kind, "label": label, "tone": tone, "icon": icon, "title": title, "detail": detail,
                    "at": at, "url": url})

    for u in User.objects.filter(is_staff=False, date_joined__gte=start).order_by("-date_joined")[:PER_KIND]:
        add("advertiser" if u.is_advertiser else "user", u.display_name or u.email, u.email, u.date_joined,
            _url("user_change", u.pk))

    for v in IdVerification.objects.filter(created_at__gte=start).select_related("user").order_by("-created_at")[:PER_KIND]:
        h = verification_history(v)
        status = "" if v.status == IdVerification.Status.PENDING else f" · {v.get_status_display().lower()}"
        add(h["key"], v.full_name or v.user.display_name or v.user.email, f"{v.get_document_type_display()}{status}",
            v.created_at, _url("idverification_change", v.pk))

    for ad in Ad.objects.filter(created_at__gte=start).select_related("owner").order_by("-created_at")[:PER_KIND]:
        add("ad", ad.title, f"by {ad.owner.display_name or ad.owner.email} · {ad.get_status_display()}", ad.created_at,
            _url("ad_change", ad.pk))
    if SiteSettings.load().review_ads:
        for ad in Ad.objects.filter(status=Ad.Status.PENDING, updated_at__gte=start).select_related("owner")[:PER_KIND]:
            add("ad_review", ad.title, f"by {ad.owner.display_name or ad.owner.email}", ad.updated_at,
                _url("ad_change", ad.pk))

    for o in Order.objects.filter(status=Order.Status.PAID, paid_at__gte=start).select_related("user").order_by("-paid_at")[:PER_KIND]:
        add("order", f"{o.product_name} – {o.amount / 100:,.2f} {o.currency}", o.user.display_name or o.user.email,
            o.paid_at, _url("order_change", o.pk))

    for r in Report.objects.filter(created_at__gte=start).exclude(status=Report.Status.DRAFT).select_related("ad").order_by("-created_at")[:PER_KIND]:
        add("report", r.ad.title if r.ad_id else "Ad", r.get_reason_display(), r.created_at, _url("report_change", r.pk))
    for r in UserReport.objects.filter(created_at__gte=start).order_by("-created_at")[:PER_KIND]:
        add("user_report", r.get_reason_display(), (r.message or "")[:80], r.created_at, _url("userreport_change", r.pk))

    for m in ContactMessage.objects.filter(created_at__gte=start).order_by("-created_at")[:PER_KIND]:
        add("contact", f"{m.name} – {m.get_topic_display()}", m.email, m.created_at, _url("contactmessage_change", m.pk))

    for c in Conversation.objects.filter(created_at__gte=start).select_related("visitor", "advertiser").order_by("-created_at")[:PER_KIND]:
        add("chat", c.ad_title or "Conversation",
            f"{c.visitor.display_name or c.visitor.email} → {c.advertiser.display_name or c.advertiser.email}",
            c.created_at, _url("conversation_change", c.pk))

    for acc in LegacyAccount.objects.filter(claimed_at__gte=start).select_related("user").order_by("-claimed_at")[:PER_KIND]:
        add("claim", acc.user.display_name or acc.legacy_username, f"reward: {acc.reward_tier or 'free'}", acc.claimed_at,
            _url("user_change", acc.user_id))

    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:limit]


def seen_at(request):
    from django.utils.dateparse import parse_datetime
    raw = request.session.get(SEEN_KEY)
    return parse_datetime(raw) if raw else None


def mark_seen(request):
    request.session[SEEN_KEY] = timezone.now().isoformat()


def for_header(request) -> dict:
    """Bell in the header: newest items and how many are new since this admin last looked."""
    try:
        items = events(limit=12)
    except Exception:  # noqa: BLE001 – the admin must keep working (e.g. during migrations)
        return {"notif_items": [], "notif_new": 0}
    seen = seen_at(request)
    for e in items:
        e["new"] = seen is None or e["at"] > seen
    return {"notif_items": items, "notif_new": sum(e["new"] for e in items)}


def chat_stats(now=None) -> dict:
    """Chat activity without reading anyone's messages."""
    from .models import Conversation, Message

    now = now or timezone.now()
    day, week = now - timedelta(days=1), now - timedelta(days=7)
    latest = (Conversation.objects.select_related("visitor", "advertiser")
              .annotate(n=Count("messages", filter=Q(messages__deleted_at__isnull=True)))
              .order_by("-last_message_at")[:8])
    return {
        "conversations": Conversation.objects.count(),
        "active_24h": Conversation.objects.filter(last_message_at__gte=day).count(),
        "messages_24h": Message.objects.filter(created_at__gte=day).count(),
        "messages_7d": Message.objects.filter(created_at__gte=week).count(),
        "blocked": Conversation.objects.filter(blocked_by__isnull=False).count(),
        "latest": [
            {"url": _url("conversation_change", c.pk), "ad": c.ad_title,
             "visitor": c.visitor.display_name or c.visitor.email, "advertiser": c.advertiser.display_name or c.advertiser.email,
             "messages": c.n, "last": c.last_message_at, "blocked": bool(c.blocked_by_id)}
            for c in latest
        ],
    }
