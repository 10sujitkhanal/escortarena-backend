"""Activating an ad: choose a plan and pay by card, with coins, or use the Free plan.

Rules
- Free: lasts SiteSettings.free_ad_days (7) and an advertiser can have only one Free ad running or in review.
- Paid plans run for the period bought. Bought before approval, the days start when the ad goes live.
- Coins: price / Site settings → Coin value (rounded up); no cashback on coin purchases.
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import economy, payments
from .adstatus import missing_fields
from .models import Ad, Order, Product, Tier


def running_free_ad(user, exclude=None):
    """The advertiser's Free ad that is live or waiting for review (other than `exclude`)."""
    now = timezone.now()
    qs = Ad.objects.filter(owner=user, free_listing=True).filter(
        Q(status=Ad.Status.PENDING) | Q(status=Ad.Status.ACTIVE, expires_at__gt=now)
    )
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.order_by("-updated_at").first()


def check_ready(ad):
    missing = missing_fields(ad)
    if missing:
        raise ValidationError({"detail": f"Fill in {', '.join(missing)} before activating the ad."})
    if ad.status == Ad.Status.PENDING:
        raise ValidationError({"detail": "The ad is already waiting for review."})


def activate(user, ad: Ad, product: Product, method: str, offer=None, addons=()) -> dict:
    if ad.owner_id != user.pk:
        raise ValidationError({"detail": "This is not your ad."})
    if product.kind != Product.Kind.PLAN:
        raise ValidationError({"product": "Choose a plan."})
    check_ready(ad)
    if product.tier == Tier.FREE:
        other = running_free_ad(user, exclude=ad)
        if other:
            raise ValidationError({"detail": f"You already have a Free ad (\u201c{other.title}\u201d). "
                                             "Only one Free ad at a time – choose a paid plan, or wait until it expires."})
    if product.tier != Tier.FREE and method not in ("card", "coins"):
        raise ValidationError({"method": "Choose card or coins."})
    try:
        if method == "coins" and (product.tier != Tier.FREE or addons):
            return {"status": "done", "order_id": pay_with_coins(user, ad, product, offer, addons).order_id}
        order = payments.start_checkout(user, ad, product, offer, addons)  # Free without add-ons: completed at once
    except payments.PaymentError as exc:
        raise ValidationError({"detail": str(exc)})
    if order.status == Order.Status.PAID:
        return {"status": "done", "order_id": order.order_id}
    return {"status": "redirect", "order_id": order.order_id, "payment_url": order.payment_url}


@transaction.atomic
def pay_with_coins(user, ad: Ad, product: Product, offer=None, addons=()) -> Order:
    payments.check_free_window(ad, product)
    extras = payments.addon_snapshot(addons)
    amount, offer = economy.price_for(product, user, offer)
    amount += sum(a["price"] for a in extras)
    coins = economy.coins_for_price(amount)
    label = " + ".join([product.name, *(a["name"] for a in extras)])
    economy.spend_diamonds(user, coins, f"{label} for {ad.title}"[:120])
    now = timezone.now()
    order = Order.objects.create(
        user=user, ad=ad, product=product, product_name=product.name, tier=product.tier, days=product.days,
        diamonds=0, amount=amount, list_amount=product.price + sum(a["price"] for a in extras), offer=offer,
        currency=product.currency, gateway="coins", status=Order.Status.PAID, paid_at=now, addons=extras,
    )
    payments._apply_to_ad(order)
    return order
