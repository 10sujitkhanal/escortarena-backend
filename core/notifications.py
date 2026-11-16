"""Notifications: the header bell (likes, favourites, comments, ad review, expiry, payments, ID check, offers).

Events create rows through `notify()`; each new or updated row is pushed live to the
user's open tabs over the messages WebSocket as {"type": "notification", ...}.

- Likes, picture likes, favourites and comments on the same ad are grouped while unread
  ("Anna and 2 others liked your ad").
- Time-based reminders (expires soon, expired, plan ends) come from `sweep()`. It runs at
  most every few minutes when a signed-in browser asks for its count, and can also be
  run from cron: `python manage.py send_reminders`.
- Who saved an ad to favourites stays private: the owner only sees "Someone".
"""

from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Ad,
    Comment,
    Favorite,
    Heart,
    IdVerification,
    Offer,
    Order,
    PictureHeart,
    Tier,
    User,
)
from .models import (
    Notification as N,
)

K = N.Kind
GROUPED = {K.AD_LIKED, K.PICTURE_LIKED, K.AD_FAVORITED, K.AD_COMMENT}
PAGE = 30
SWEEP_EVERY = 300  # seconds
EXPIRY_WARNINGS = (3, 1)  # days before expiry
PLAN_WARNING_DAYS = 2
VIP_WARNING_DAYS = 3


# ---------------------------------------------------------------------------
# Text shown to the user
# ---------------------------------------------------------------------------


def _who(n: N) -> str:
    name = n.actor_name or "Someone"
    if n.count <= 1:
        return name
    others = n.count - 1
    return f"{name} and {others} other{'s' if others > 1 else ''}"


def _days(until) -> str:
    if not until:
        return "soon"
    hours = (until - timezone.now()).total_seconds() / 3600
    if hours <= 24:
        return "within 24 hours"
    return f"in {round(hours / 24)} days"


def message(n: N) -> str:
    title = f"“{n.ad.title}”" if n.ad_id and n.ad else "your ad"
    return {
        K.AD_LIKED: f"{_who(n)} liked your ad {title}.",
        K.PICTURE_LIKED: f"{_who(n)} liked {'a picture' if n.count <= 1 else 'pictures'} in your ad {title}.",
        K.AD_FAVORITED: (f"Someone saved your ad {title} to their favourites." if n.count <= 1
                         else f"{n.count} people saved your ad {title} to their favourites."),
        K.AD_COMMENT: f"{_who(n)} commented on your ad {title}.",
        K.AD_APPROVED: f"Your ad {title} was approved and is now live.",
        K.AD_REJECTED: f"Your ad {title} was not approved.",
        K.AD_PAUSED: f"Your ad {title} was paused by our moderators.",
        K.AD_EXPIRING: f"Your ad {title} expires {_days(n.ad.expires_at if n.ad_id and n.ad else None)}. Extend it to stay visible.",
        K.AD_EXPIRED: f"Your ad {title} has expired and is no longer shown.",
        K.PLAN_ENDING: f"The paid plan on {title} ends {_days(n.ad.tier_until if n.ad_id and n.ad else None)}.",
        K.PAYMENT: "Payment received – thank you!",
        K.ID_APPROVED: "Your ID is verified. Your ads can now be approved.",
        K.ID_REJECTED: "We could not verify your ID.",
        K.REPORT_REVIEWED: "Thank you – a moderator has reviewed your report.",
        K.OFFER: "New offer for you",
        K.FAVORITE_GONE: f"An ad in your favourites, {title}, has expired.",
        K.ID_RECEIVED: "We received your ID documents. A moderator checks them, usually within 24 hours.",
        K.VIP_STARTED: f"You are VIP{(' – ' + n.text) if n.text else ''}.",
        K.VIP_ENDING: f"Your VIP membership ends {n.text or 'soon'}. Renew it to keep messaging advertisers.",
    }.get(n.kind, n.get_kind_display())


def serialize(n: N) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "message": message(n),
        "text": n.text,
        "link": n.link,
        "count": n.count,
        "read": n.read_at is not None,
        "created_at": n.updated_at,
    }


def unread_count(user) -> int:
    return N.objects.filter(user=user, read_at__isnull=True).count()


# ---------------------------------------------------------------------------
# Creating notifications
# ---------------------------------------------------------------------------


def _push(n: N) -> None:
    from .realtime import _send

    uid, payload_id = n.user_id, n.pk

    def send():
        row = N.objects.select_related("ad").filter(pk=payload_id).first()
        if row:
            _send(uid, {"type": "notification", "notification": serialize(row), "unread_notifications": unread_count(row.user)})

    transaction.on_commit(send)


def notify(user, kind, *, ad=None, actor=None, text="", link="", key="") -> N | None:
    """Create (or, for grouped kinds, bump) a notification and push it live."""
    if user is None or (actor is not None and actor.pk == user.pk):
        return None  # nobody is notified about their own actions
    actor_name = (actor.display_name or "").strip()[:60] if actor is not None else ""
    now = timezone.now()
    if key and N.objects.filter(user=user, key=key).exists():
        return None  # reminder already sent
    if kind in GROUPED and ad is not None:
        existing = N.objects.filter(user=user, kind=kind, ad=ad, read_at__isnull=True).first()
        if existing:
            N.objects.filter(pk=existing.pk).update(count=F("count") + 1, actor_name=actor_name or existing.actor_name, updated_at=now)
            existing.refresh_from_db()
            _push(existing)
            return existing
    n = N.objects.create(user=user, kind=kind, ad=ad, actor_name=actor_name, text=text[:300], link=link, key=key, updated_at=now)
    _push(n)
    email_for(n)
    return n


# E-mails -----------------------------------------------------------------------
# kind → (subject, button label, which user setting turns it off – None = always sent).
# Likes, favourites and comments stay in-app only (too many e-mails).
EMAILS = {
    K.AD_APPROVED: ("your ad is live", "View my ad", None),
    K.AD_REJECTED: ("your ad was not approved", "Edit my ad", None),
    K.AD_PAUSED: ("your ad was paused", "Open my ad", None),
    K.AD_EXPIRING: ("your ad expires soon", "Extend my ad", "notify_expiry"),
    K.AD_EXPIRED: ("your ad has expired", "Extend my ad", "notify_expiry"),
    K.PLAN_ENDING: ("your plan ends soon", "Renew the plan", "notify_expiry"),
    K.PAYMENT: ("payment received – thank you", "View my payments", None),
    K.ID_RECEIVED: ("we received your ID documents", "Open my settings", None),
    K.ID_APPROVED: ("you are verified", "Go to My ads", None),
    K.ID_REJECTED: ("ID verification not approved", "Send new pictures", None),
    K.OFFER: ("a new offer for you", "See the offer", "notify_offers"),
    K.VIP_STARTED: ("welcome to VIP", "Start messaging", None),
    K.VIP_ENDING: ("your VIP membership ends soon", "Renew VIP", None),
}


def email_for(n: N) -> None:
    rule = EMAILS.get(n.kind)
    if not rule or not n.user.is_active or not n.user.email:
        return
    subject, button, pref = rule
    if pref and not getattr(n.user, pref, True):
        return
    from . import emails
    lines = [message(n)]
    if n.kind in (K.AD_REJECTED, K.ID_REJECTED) and n.text:
        lines.append(f"Reason: {n.text}")
    elif n.kind == K.PAYMENT and n.text:
        lines.append(f"Paid: {n.text}")
    elif n.kind == K.OFFER and n.text:
        lines.append(n.text)
    if n.kind == K.ID_REJECTED:
        lines.append("You can send new pictures from your settings. Your account stays an advertiser account.")
    elif n.kind == K.ID_APPROVED:
        lines.append("Your profile and ads now show the Verified badge.")
    elif n.kind == K.AD_REJECTED:
        lines.append("Fix the points above and send the ad for review again.")
    note = "You can turn these e-mails off in Settings → Notifications." if pref else ""
    emails.send(to=n.user.email, user=n.user, subject=subject, heading=subject[:1].upper() + subject[1:],
                lines=lines, button=(button, n.link or "/notifications"), note=note)


def ad_link(ad) -> str:
    return f"/ads/{ad.pk}"


def my_ad_link(ad) -> str:
    return f"/my-ads/{ad.pk}"


# Likes, favourites, comments ---------------------------------------------------


@receiver(post_save, sender=Heart)
def _heart(sender, instance, created, **kw):
    if created:
        notify(instance.ad.owner, K.AD_LIKED, ad=instance.ad, actor=instance.user, link=ad_link(instance.ad))


@receiver(post_save, sender=PictureHeart)
def _picture_heart(sender, instance, created, **kw):
    if created:
        ad = instance.picture.ad
        notify(ad.owner, K.PICTURE_LIKED, ad=ad, actor=instance.user, link=ad_link(ad))


@receiver(post_save, sender=Favorite)
def _favorite(sender, instance, created, **kw):
    if created and instance.user_id != instance.ad.owner_id:
        notify(instance.ad.owner, K.AD_FAVORITED, ad=instance.ad, link=my_ad_link(instance.ad))  # anonymous on purpose


@receiver(post_save, sender=Comment)
def _comment(sender, instance, created, **kw):
    if created:
        notify(instance.ad.owner, K.AD_COMMENT, ad=instance.ad, actor=instance.user, text=instance.body[:140],
               link=f"{ad_link(instance.ad)}#comments")


# Status changes (remember the old value before saving) ---------------------------


def _remember(sender, instance, **kw):
    instance._old_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first() if instance.pk else None


for model in (Ad, Order, IdVerification):
    pre_save.connect(_remember, sender=model, dispatch_uid=f"notif-old-{model.__name__}")


@receiver(post_save, sender=Ad)
def _ad_status(sender, instance, created, **kw):
    old, new = getattr(instance, "_old_status", None), instance.status
    if created or old == new:
        return
    if new == Ad.Status.ACTIVE and old == Ad.Status.PENDING:
        notify(instance.owner, K.AD_APPROVED, ad=instance, link=ad_link(instance))
    elif new == Ad.Status.REJECTED:
        notify(instance.owner, K.AD_REJECTED, ad=instance, text=instance.rejection_reason, link=my_ad_link(instance))


@receiver(post_save, sender=Order)
def _order(sender, instance, created, **kw):
    if instance.status == Order.Status.PAID and getattr(instance, "_old_status", None) != Order.Status.PAID:
        amount = f"{instance.amount / 100:,.2f} {instance.currency}"
        what = instance.product_name + (f" for “{instance.ad.title}”" if instance.ad_id and instance.ad else "")
        kind = instance.product.kind if instance.product_id else ""
        link = "/vip" if kind == "membership" else "/payments" if kind == "diamonds" else (f"/my-ads/{instance.ad_id}" if instance.ad_id else "/payments")
        if instance.amount:  # paid with money (coins-only purchases have their own messages)
            notify(instance.user, K.PAYMENT, text=f"{what} · {amount}", link=link, key=f"paid:{instance.order_id}")


@receiver(post_save, sender=IdVerification)
def _id_check(sender, instance, created, **kw):
    if getattr(instance, "_old_status", None) == instance.status:
        return
    if instance.status == IdVerification.Status.APPROVED:
        notify(instance.user, K.ID_APPROVED, link="/my-ads")
    elif instance.status == IdVerification.Status.REJECTED:
        notify(instance.user, K.ID_REJECTED, text=instance.reject_reason, link="/account")


@receiver(post_save, sender=Offer)
def _offer(sender, instance, created, **kw):
    if not created or not instance.is_active:
        return
    pct = f"−{instance.discount_percent}%"
    owners = User.objects.filter(ads__isnull=False).distinct()
    for user in owners.iterator():
        notify(user, K.OFFER, text=f"{instance.title} ({pct})", link="/offers", key=f"offer:{instance.pk}")


def moderator_paused(ad) -> None:
    """Admin actions update in bulk (no signals), so they call this."""
    notify(ad.owner, K.AD_PAUSED, ad=ad, link=my_ad_link(ad))


def report_reviewed(report) -> None:
    if report.reporter_id:
        notify(report.reporter, K.REPORT_REVIEWED, key=f"report:{report.pk}")


# Time-based reminders ------------------------------------------------------------


def sweep(force=False) -> int:
    """Send 'expires soon', 'expired' and 'plan ends' reminders once each. Returns how many were sent."""
    if not force and not cache.add("notifications:sweep", 1, SWEEP_EVERY):
        return 0
    now, sent = timezone.now(), 0
    live = Ad.objects.filter(status=Ad.Status.ACTIVE).select_related("owner")

    for days in EXPIRY_WARNINGS:
        window = live.filter(expires_at__gt=now, expires_at__lte=now + timedelta(days=days))
        for ad in window:
            key = f"expiring:{ad.pk}:{ad.expires_at:%Y%m%d%H%M}:{days}"
            # The 1-day reminder replaces the 3-day one if both are due at once.
            if days == 3 and ad.expires_at <= now + timedelta(days=1):
                continue
            sent += bool(notify(ad.owner, K.AD_EXPIRING, ad=ad, link=f"/my-ads/{ad.pk}/activate", key=key))

    for ad in live.filter(expires_at__lte=now, expires_at__gt=now - timedelta(days=30)).prefetch_related("favorited_by__user"):
        stamp = f"{ad.expires_at:%Y%m%d%H%M}"
        sent += bool(notify(ad.owner, K.AD_EXPIRED, ad=ad, link=f"/my-ads/{ad.pk}/activate", key=f"expired:{ad.pk}:{stamp}"))
        for fav in ad.favorited_by.all():
            sent += bool(notify(fav.user, K.FAVORITE_GONE, ad=ad, link="/favorites", key=f"favgone:{ad.pk}:{stamp}"))

    soon = User.objects.filter(is_active=True, vip_until__gt=now, vip_until__lte=now + timedelta(days=VIP_WARNING_DAYS))
    for user in soon:
        when = timezone.localtime(user.vip_until).strftime("on %d %b %Y")
        sent += bool(notify(user, K.VIP_ENDING, text=when, link="/vip", key=f"vipend:{user.pk}:{user.vip_until:%Y%m%d%H%M}"))

    ending = live.exclude(tier=Tier.FREE).filter(tier_until__gt=now, tier_until__lte=now + timedelta(days=PLAN_WARNING_DAYS))
    for ad in ending.filter(expires_at__gt=F("tier_until") + timedelta(hours=1)):  # otherwise "expires soon" covers it
        sent += bool(notify(ad.owner, K.PLAN_ENDING, ad=ad, link=f"/my-ads/{ad.pk}/activate", key=f"planend:{ad.pk}:{ad.tier_until:%Y%m%d%H%M}"))
    return sent


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class NotificationListView(APIView):
    """GET ?filter=unread&before=<id> → {results, unread, has_more}"""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        sweep()
        qs = N.objects.filter(user=request.user).select_related("ad")
        if request.query_params.get("filter") == "unread":
            qs = qs.filter(read_at__isnull=True)
        before = request.query_params.get("before")
        if before and before.isdigit():
            anchor = N.objects.filter(user=request.user, pk=before).first()
            if anchor:
                qs = qs.filter(Q(updated_at__lt=anchor.updated_at) | Q(updated_at=anchor.updated_at, pk__lt=anchor.pk))
        rows = list(qs.order_by("-updated_at", "-id")[: PAGE + 1])
        return Response({
            "results": [serialize(n) for n in rows[:PAGE]],
            "has_more": len(rows) > PAGE,
            "unread": unread_count(request.user),
        })


class NotificationCountView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        sweep()
        return Response({"unread": unread_count(request.user)})


def _changed(user):
    from .realtime import _send

    transaction.on_commit(lambda: _send(user.pk, {"type": "notifications_read", "unread_notifications": unread_count(user)}))


class NotificationReadAllView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        N.objects.filter(user=request.user, read_at__isnull=True).update(read_at=timezone.now())
        _changed(request.user)
        return Response({"unread": 0})


class NotificationDetailView(APIView):
    """POST → mark read · DELETE → remove"""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        n = get_object_or_404(N, pk=pk, user=request.user)
        if request.data.get("read") is False:
            n.read_at = None
        elif n.read_at is None:
            n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
        _changed(request.user)
        return Response({"unread": unread_count(request.user)})

    def delete(self, request, pk):
        get_object_or_404(N, pk=pk, user=request.user).delete()
        _changed(request.user)
        return Response({"unread": unread_count(request.user)})

