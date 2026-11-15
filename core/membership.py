"""Visitor membership: Free or VIP.

VIP visitors can message advertisers, comment, write reviews and watch ad videos. VIP is bought for a number of
days (Admin → Products, kind "Visitor VIP membership") by card or with coins. Days add up when bought again.
"""

import math
import struct
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import DiamondTransaction, Order, Product, User

# Shown on the VIP page (Free vs VIP).
PERKS = [
    {"key": "browse", "label": "Browse all ads, pictures and the map", "free": True},
    {"key": "favorites", "label": "Save favourites and like pictures", "free": True},
    {"key": "message", "label": "Send messages to advertisers", "free": False},
    {"key": "comment", "label": "Comment on ads", "free": False},
    {"key": "review", "label": "Write reviews with star ratings", "free": False},
    {"key": "video", "label": "Watch advertisers' videos", "free": False},
    {"key": "badge", "label": "VIP badge next to your name in comments", "free": False},
]

MESSAGES = {
    "message": "Become VIP to send messages to advertisers.",
    "comment": "Become VIP to comment on ads.",
    "review": "Become VIP to write reviews.",
    "video": "Become VIP to watch videos.",
}


def is_vip(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.is_vip)


def require_vip(user, what: str) -> None:
    """Stop free visitors. The answer carries `vip_required` so the site can show the VIP offer."""
    if not is_vip(user):
        raise PermissionDenied({"detail": MESSAGES.get(what, "This is for VIP members."), "vip_required": True})


def products():
    return Product.objects.filter(kind=Product.Kind.MEMBERSHIP, is_active=True).order_by("days")


def coin_price(product: Product) -> int:
    from .economy import coin_value
    return math.ceil(product.price / max(1, coin_value()))


def extend(user: User, days: int) -> None:
    """Add days to the user's VIP (from now, or from the current end if still VIP)."""
    now = timezone.now()
    user.refresh_from_db(fields=["vip_until"])
    start = user.vip_until if user.vip_until and user.vip_until > now else now
    user.vip_until = start + timedelta(days=days)
    user.save(update_fields=["vip_until"])


def apply_order(order: Order) -> None:
    """A paid membership order: give the VIP days once."""
    if order.applied_at:
        return
    extend(order.user, order.days)
    order.applied_at = timezone.now()
    order.save(update_fields=["applied_at", "updated_at"])
    from .notifications import K, notify
    user = User.objects.get(pk=order.user_id)
    until = timezone.localtime(user.vip_until).strftime("%d %b %Y")
    notify(user, K.VIP_STARTED, text=f"until {until}", link="/vip", key=f"vip:{order.order_id}")


@transaction.atomic
def buy_with_coins(user: User, product: Product) -> Order:
    cost = coin_price(product)
    locked = User.objects.select_for_update().get(pk=user.pk)
    if locked.diamonds < cost:
        raise ValidationError({"detail": f"You need {cost} coins – you have {locked.diamonds}."})
    locked.diamonds -= cost
    locked.save(update_fields=["diamonds"])
    now = timezone.now()
    order = Order.objects.create(
        user=locked, ad=None, product=product, product_name=product.name, tier=product.tier, days=product.days,
        diamonds=0, amount=0, list_amount=product.price, currency=product.currency, gateway="coins",
        status=Order.Status.PAID, paid_at=now,
    )
    DiamondTransaction.objects.create(user=locked, amount=-cost, reason=f"Bought {product.name}", order=order)
    apply_order(order)
    return order


def status(user) -> dict:
    vip = is_vip(user)
    return {"is_vip": vip, "vip_until": user.vip_until if vip and not user.is_staff else None}


# ---------------------------------------------------------------------------
# Video length (MP4 / MOV) – read from the file itself, no extra programs needed.
# ---------------------------------------------------------------------------


class InvalidVideo(Exception):
    pass


def _boxes(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        header = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, pos + size
        pos += size


def mp4_duration(f) -> float:
    """Length in seconds of an MP4/MOV file (from the movie header 'mvhd')."""
    f.seek(0, 2)
    end = f.tell()
    first = None
    for kind, body, box_end in _boxes(f, 0, end):
        first = first or kind
        if kind != b"moov":
            continue
        for sub, sbody, _ in _boxes(f, body, box_end):
            if sub != b"mvhd":
                continue
            f.seek(sbody)
            version = f.read(1)[0]
            f.read(3)
            if version == 1:
                f.read(16)
                timescale, duration = struct.unpack(">IQ", f.read(12))
            else:
                f.read(8)
                timescale, duration = struct.unpack(">II", f.read(8))
            f.seek(0)
            if not timescale:
                break
            return duration / timescale
    f.seek(0)
    if first != b"ftyp":
        raise InvalidVideo("This is not an MP4 or MOV video.")
    raise InvalidVideo("Could not read the length of this video.")
