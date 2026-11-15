"""Coins (virtual currency; stored in the `diamonds` fields) and offers.

1 coin = SiteSettings.coin_value cents (default 1.00). Card purchases of plans give 5 % back in coins. Coins buy add-ons
(Standard/Premium/VIP days, bump) but can never be cashed out or transferred.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import PAID_TIERS, TIER_RANK, Ad, DiamondTransaction, Offer, Product, User

BUMP_COST = 20  # coins to move an ad back to the top of the lists
CASHBACK_PERCENT = 5


def active_offers():
    """Running offers that are not sold out (public list, home page)."""
    now = timezone.now()
    qs = Offer.objects.filter(Q(no_end_date=True) | Q(ends_at__gt=now), is_active=True, starts_at__lte=now)
    qs = qs.annotate(paid_uses=Count("orders", filter=Q(orders__status="paid")))
    return qs.filter(Q(max_uses=0) | Q(paid_uses__lt=F("max_uses")))


def used_offer_ids(user):
    """Offers this user already paid with (for 'once per advertiser' offers)."""
    from .models import Order
    if not user or not getattr(user, "is_authenticated", False):
        return set()
    return set(Order.objects.filter(user=user, status=Order.Status.PAID, offer__isnull=False).values_list("offer_id", flat=True))


def offers_for(user):
    """Offers this user can use right now."""
    qs = active_offers()
    used = used_offer_ids(user)
    return qs.exclude(Q(once_per_user=True) & Q(id__in=used)) if used else qs


def offer_status(offer: Offer, user=None, used: set | None = None) -> str:
    now = timezone.now()
    if not offer.no_end_date and offer.ends_at <= now:
        return "ended"
    if offer.once_per_user and offer.id in (used if used is not None else used_offer_ids(user)):
        return "used"
    if offer.max_uses:
        n = getattr(offer, "paid_uses", None)
        if n is None:
            n = offer.orders.filter(status="paid").count()
        if n >= offer.max_uses:
            return "sold_out"
    return "active"


def offer_applies(offer: Offer, product: Product) -> bool:
    """Does this offer cover this product (coin packages / plans / one plan tier)?"""
    if product.price == 0 or product.kind == Product.Kind.MEMBERSHIP:
        return False
    kind = "diamonds" if product.kind == Product.Kind.DIAMONDS else "plans"
    if offer.applies_to not in ("all", kind):
        return False
    return not (kind == "plans" and offer.tier and offer.tier != product.tier)


def resolve_offer(user, ref) -> Offer | None:
    """Offer chosen by the user ('Use offer' link or typed code) – id or code. Raises if it can't be used."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    q = Q(code__iexact=ref) if not ref.isdigit() else Q(pk=int(ref)) | Q(code__iexact=ref)
    offer = Offer.objects.filter(q, is_active=True).first()
    if offer is None:
        raise ValidationError({"offer": "There is no offer with that code."})
    status = offer_status(offer, user)
    if offer.starts_at > timezone.now():
        status = "not_started"
    reasons = {"ended": "This offer has ended.", "used": "You have already used this offer.",
               "sold_out": "This offer is sold out.", "not_started": "This offer has not started yet."}
    if status in reasons:
        raise ValidationError({"offer": reasons[status]})
    return offer


def price_for(product: Product, user=None, offer: Offer | None = None) -> tuple[int, Offer | None]:
    """Price for this product. Offers are only applied when the user chose one ('Use offer' or a code)
    and it covers the product – otherwise the normal price."""
    if not offer or not offer_applies(offer, product):
        return product.price, None
    # Round to whole currency units (100 cents).
    return int(round(product.price * (100 - offer.discount_percent) / 100 / 100) * 100), offer


@transaction.atomic
def add_diamonds(user: User, amount: int, reason: str, order=None) -> int:
    User.objects.filter(pk=user.pk).update(diamonds=F("diamonds") + amount)
    DiamondTransaction.objects.create(user=user, amount=amount, reason=reason, order=order)
    user.refresh_from_db(fields=["diamonds"])
    return user.diamonds


@transaction.atomic
def spend_diamonds(user: User, amount: int, reason: str) -> int:
    if amount <= 0:
        return user.diamonds
    # Conditional update: never goes below zero, even with parallel requests.
    if not User.objects.filter(pk=user.pk, diamonds__gte=amount).update(diamonds=F("diamonds") - amount):
        user.refresh_from_db(fields=["diamonds"])
        raise ValidationError({"detail": f"You need {amount} coins but have {user.diamonds}."})
    DiamondTransaction.objects.create(user=user, amount=-amount, reason=reason)
    user.refresh_from_db(fields=["diamonds"])
    return user.diamonds


def bump_ad(user: User, ad: Ad) -> Ad:
    if not ad.is_live:
        raise ValidationError({"detail": "Only live ads can be bumped."})
    spend_diamonds(user, BUMP_COST, f"Bump: {ad.title}")
    ad.bumped_at = timezone.now()
    ad.save(update_fields=["bumped_at", "updated_at"])
    return ad


# ---------------------------------------------------------------------------
# Cashback and refunds
# ---------------------------------------------------------------------------


def coin_value() -> int:
    """Cents per coin (Site settings → Coin value)."""
    from .models import SiteSettings
    return max(1, SiteSettings.load().coin_value)


def coins_for_price(amount_minor: int) -> int:
    """Coins needed to pay a price (in cents), rounded up."""
    v = coin_value()
    return -(-amount_minor // v)


def coins_for_amount(amount_minor: int) -> int:
    """Coins earned on a card payment (amount in cents): 5 %, whole coins."""
    return amount_minor * CASHBACK_PERCENT // 100 // coin_value()


def give_cashback(order) -> int:
    coins = coins_for_amount(order.amount)
    if coins > 0:
        add_diamonds(order.user, coins, f"{CASHBACK_PERCENT}% back on {order.product_name}", order=order)
    return coins


@transaction.atomic
def reverse_order_coins(order) -> int:
    """On refund, take back the coins the order gave (package or cashback). The balance may go negative."""
    given = sum(t.amount for t in DiamondTransaction.objects.filter(order=order))
    if given > 0:
        User.objects.filter(pk=order.user_id).update(diamonds=F("diamonds") - given)
        DiamondTransaction.objects.create(user_id=order.user_id, amount=-given, reason=f"Refund of {order.product_name}", order=order)
    return given


# ---------------------------------------------------------------------------
# Spending coins on add-ons for one ad
# ---------------------------------------------------------------------------


def addons_for(ad: Ad) -> list[dict]:
    """What can be bought with coins for this ad. Paid days must end before the ad itself ends."""
    now = timezone.now()
    if not ad.is_live or not ad.expires_at:
        return []
    items = [{"code": "bump", "kind": "bump", "label": "Bump to the top", "tier": "", "days": 0, "coins": BUMP_COST,
              "help": "Moves the ad back to the top of the lists now."}]
    for p in Product.objects.filter(kind=Product.Kind.PLAN, is_active=True, plan__is_active=True, tier__in=PAID_TIERS).order_by("days"):
        start = ad.tier_until if (ad.tier == p.tier and ad.tier_until and ad.tier_until > now) else now
        downgrade = TIER_RANK[p.tier] < TIER_RANK[ad.effective_tier]  # would throw away paid days of a higher plan
        items.append({
            "code": p.code, "kind": "tier", "tier": p.tier, "days": p.days,
            "label": f"{p.get_tier_display()} – {p.get_days_display()}", "coins": coins_for_price(price_for(p, ad.owner)[0]),
            "help": {"standard": "Shown above Free ads, with a Standard badge.",
                     "premium": "Shown higher up and more often, with a Premium badge.",
                     "vip": "Top placement in every list, the front-page slider and a VIP badge."}[p.tier],
            "ends_at": start + timedelta(days=p.days),
            "fits": start + timedelta(days=p.days) <= ad.expires_at and not downgrade,
            "reason": (f"Your ad has {ad.effective_tier.title()} until {timezone.localtime(ad.tier_until):%d %b}" if downgrade
                       else "Ends after your ad – extend the ad first"),
        })
    return items


@transaction.atomic
def spend_on_ad(user: User, ad: Ad, codes: list[str]) -> dict:
    if ad.owner_id != user.pk:
        raise ValidationError({"detail": "This is not your ad."})
    ad = Ad.objects.select_for_update().get(pk=ad.pk)
    by_code = {i["code"]: i for i in addons_for(ad)}
    chosen = [by_code.get(c) for c in dict.fromkeys(codes)]
    if not chosen or None in chosen:
        raise ValidationError({"detail": "Choose at least one add-on that is available for this ad."})
    tiers = [i for i in chosen if i["kind"] == "tier"]
    if len(tiers) > 1:
        raise ValidationError({"detail": "Choose only one plan option."})
    if tiers and not tiers[0]["fits"]:
        raise ValidationError({"detail": "Extend your ad first – paid days with coins can't run past the ad's end date."})
    total = sum(i["coins"] for i in chosen)
    balance = spend_diamonds(user, total, f"{' + '.join(i['label'] for i in chosen)} on {ad.title}"[:120])
    now = timezone.now()
    fields = ["updated_at"]
    if tiers:
        ad.tier, ad.tier_until = tiers[0]["tier"], tiers[0]["ends_at"]
        fields += ["tier", "tier_until"]
    if tiers or any(i["kind"] == "bump" for i in chosen):
        ad.bumped_at = now
        fields.append("bumped_at")
    ad.save(update_fields=fields)
    return {"spent": total, "balance": balance}
