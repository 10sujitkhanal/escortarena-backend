"""Payments: QuickPay (https://learn.quickpay.net/tech-talk/api/services/) plus a fake
gateway for local development.

Flow
1. Advertiser picks a product for an ad -> we create an Order and a QuickPay payment
   with a payment link (auto-capture, since the product is delivered immediately).
2. Browser goes to QuickPay's hosted payment window. Card data never touches us.
3. QuickPay POSTs the payment JSON to our callback URL, signed with HMAC-SHA256 of the
   raw body using the account's *private key* (header QuickPay-Checksum-Sha256).
4. When the payment is accepted, the order is marked paid and applied to the ad once.
5. The return page also asks us to re-check the payment via the API, so local
   development works even when QuickPay cannot reach the callback URL.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Ad, Order, Product, SiteSettings, Tier

log = logging.getLogger(__name__)


class PaymentError(Exception):
    pass


# ---------------------------------------------------------------------------
# Gateways
# ---------------------------------------------------------------------------


class QuickPayGateway:
    name = "quickpay"

    def __init__(self, api_key: str, private_key: str, base_url: str):
        self.api_key = api_key
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        token = base64.b64encode(f":{self.api_key}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
        req.add_header("Accept-Version", "v10")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            log.warning("QuickPay %s %s failed: %s %s", method, path, exc.code, detail)
            raise PaymentError(f"QuickPay returned {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PaymentError("Could not reach QuickPay") from exc

    def create_payment_link(self, order: Order, continue_url: str, cancel_url: str, callback_url: str) -> tuple[str, str]:
        payment = self._request("POST", "/payments", {"order_id": order.order_id, "currency": order.currency})
        link = self._request(
            "PUT",
            f"/payments/{payment['id']}/link",
            {
                "amount": order.amount,
                "continue_url": continue_url,
                "cancel_url": cancel_url,
                "callback_url": callback_url,
                "auto_capture": True,
                "language": settings.QUICKPAY_LANGUAGE,
            },
        )
        return str(payment["id"]), link["url"]

    def fetch(self, payment_id: str) -> dict:
        return self._request("GET", f"/payments/{payment_id}")

    def refund(self, payment_id: str, amount: int) -> dict:
        return self._request("POST", f"/payments/{payment_id}/refund?synchronized", {"amount": amount})

    def verify_callback(self, raw_body: bytes, checksum: str) -> bool:
        expected = hmac.new(self.private_key.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, checksum or "")


class FakeGateway:
    """Local-only gateway: the 'payment window' is a page in our own frontend."""

    name = "fake"

    def create_payment_link(self, order, continue_url, cancel_url, callback_url):
        return f"fake-{order.order_id}", f"{settings.PUBLIC_SITE_URL}/payments/fake/{order.order_id}"

    def fetch(self, payment_id: str) -> dict:
        order = Order.objects.get(gateway_payment_id=payment_id)
        return order.last_gateway_state or {"accepted": False, "state": "initial"}

    def refund(self, payment_id: str, amount: int) -> dict:
        return {"accepted": True}

    def verify_callback(self, raw_body, checksum):
        return False  # the fake gateway never sends callbacks


def get_gateway():
    if settings.QUICKPAY_API_KEY and settings.QUICKPAY_PRIVATE_KEY:
        return QuickPayGateway(settings.QUICKPAY_API_KEY, settings.QUICKPAY_PRIVATE_KEY, settings.QUICKPAY_BASE_URL)
    if settings.PAYMENTS_ALLOW_FAKE:
        return FakeGateway()
    raise PaymentError("Payments are not configured.")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def addon_snapshot(addons) -> list[dict]:
    """What the order stores about each add-on, so later price edits in the admin don't change history."""
    return [{"code": a.code, "name": a.name, "effect": a.effect, "days": a.days, "price": a.price} for a in addons]


def check_free_window(ad: Ad, product: Product):
    if product.tier == Tier.FREE and ad.expires_at and ad.expires_at - timezone.now() > FREE_RENEW_WINDOW:
        raise PaymentError("You can renew for free when your ad has less than 7 days left.")


def start_checkout(user, ad: Ad | None, product: Product, offer=None, addons=()) -> Order:
    from .economy import price_for

    if product.kind in (Product.Kind.DIAMONDS, Product.Kind.MEMBERSHIP):
        ad = None  # coins and visitor VIP belong to the account, not to an ad
    else:
        if ad is None or ad.owner_id != user.id:
            raise PaymentError("You can only pay for your own ads.")
        if ad.status in {Ad.Status.REJECTED}:
            raise PaymentError("This ad was rejected. Edit and resubmit it before buying extras.")
    if product.kind in (Product.Kind.DIAMONDS, Product.Kind.MEMBERSHIP):
        addons = ()
    elif ad is not None:
        check_free_window(ad, product)
    extras = addon_snapshot(addons)
    amount, offer = price_for(product, user, offer)
    extra_total = sum(a["price"] for a in extras)
    if amount + extra_total == 0:
        return _free_checkout(user, ad, product)
    gateway = get_gateway()
    order = Order.objects.create(
        user=user, ad=ad, product=product, product_name=product.name, tier=product.tier,
        days=product.days, diamonds=product.diamonds, amount=amount + extra_total, list_amount=product.price + extra_total,
        offer=offer, currency=product.currency, gateway=gateway.name, addons=extras,
    )
    site = settings.PUBLIC_SITE_URL
    payment_id, url = gateway.create_payment_link(
        order,
        continue_url=f"{site}/payments/return?order={order.order_id}",
        cancel_url=f"{site}/payments/return?order={order.order_id}&cancelled=1",
        callback_url=f"{settings.PUBLIC_API_URL}/api/payments/quickpay/callback",
    )
    order.gateway_payment_id = payment_id
    order.payment_url = url
    order.status = Order.Status.PENDING
    order.save(update_fields=["gateway_payment_id", "payment_url", "status", "updated_at"])
    return order


def _card_brand(state: dict) -> str:
    meta = state.get("metadata") or {}
    return (meta.get("brand") or "")[:30]


FREE_RENEW_WINDOW = timedelta(days=7)


def _free_checkout(user, ad: Ad | None, product: Product) -> Order:
    """Price-0 products (the Free plan): no payment window – the order is completed at once."""
    now = timezone.now()
    if ad is None:
        raise PaymentError("Free products are only for ads.")
    check_free_window(ad, product)
    order = Order.objects.create(
        user=user, ad=ad, product=product, product_name=product.name, tier=product.tier, days=product.days,
        diamonds=0, amount=0, list_amount=0, currency=product.currency, gateway="free",
        status=Order.Status.PAID, paid_at=now,
    )
    order.payment_url = f"{settings.PUBLIC_SITE_URL}/payments/return?order={order.order_id}"
    order.save(update_fields=["payment_url", "updated_at"])
    _apply_to_ad(order)
    return order


@transaction.atomic
def apply_gateway_state(order_pk: int, state: dict) -> Order:
    """Update an order from a gateway payment object. Safe to call repeatedly."""
    order = Order.objects.select_for_update(of=("self",)).select_related("ad").get(pk=order_pk)  # lock only the order (Postgres)
    order.last_gateway_state = state

    if state.get("order_id") and state["order_id"] != order.order_id:
        raise PaymentError("Order id mismatch")
    if state.get("currency") and state["currency"] != order.currency:
        raise PaymentError("Currency mismatch")

    if state.get("accepted") and order.status != Order.Status.PAID:
        captured = _captured_amount(state)
        if captured is not None and captured < order.amount:
            log.warning("Order %s accepted but captured %s < %s", order.order_id, captured, order.amount)
            order.save(update_fields=["last_gateway_state", "updated_at"])
            return order
        order.status = Order.Status.PAID
        order.paid_at = timezone.now()
        order.card_brand = _card_brand(state)
    elif state.get("state") == "rejected" and order.status == Order.Status.PENDING:
        order.status = Order.Status.FAILED

    order.save()
    if order.status == Order.Status.PAID and not order.applied_at:
        if order.product.kind == Product.Kind.MEMBERSHIP:
            from .membership import apply_order

            apply_order(order)
        elif order.diamonds:
            from .economy import add_diamonds

            add_diamonds(order.user, order.diamonds, f"Bought {order.product_name}", order=order)
            order.applied_at = timezone.now()
            order.save(update_fields=["applied_at", "updated_at"])
        else:
            _apply_to_ad(order)
            if order.applied_at:  # once per order, even if the gateway reports "paid" again
                from .economy import give_cashback

                give_cashback(order)
    return order


def _captured_amount(state: dict) -> int | None:
    ops = state.get("operations")
    if not ops:
        return None
    return sum(op.get("amount", 0) for op in ops if op.get("type") == "capture" and op.get("qp_status_code") == "20000")


def _apply_to_ad(order: Order):
    ad = order.ad
    if ad is None:
        return
    now = timezone.now()
    ad.free_listing = order.tier == Tier.FREE
    if ad.status != Ad.Status.ACTIVE:
        # Not approved yet: keep the plan until approval, so the paid days start when the ad goes live.
        ad.pending_tier, ad.pending_days = order.tier, order.days
        if not SiteSettings.load().review_ads:
            ad.approve()  # no review: live at once, the plan and add-ons start now
        else:
            if ad.status in {Ad.Status.DRAFT, Ad.Status.PAUSED, Ad.Status.REJECTED}:
                ad.status = Ad.Status.PENDING  # buying = activating: it goes to the moderators
            ad.save(update_fields=["pending_tier", "pending_days", "free_listing", "status", "updated_at"])
        order.applied_at = now
        order.save(update_fields=["applied_at", "updated_at"])
        return
    if order.tier != Tier.FREE:
        # Extend the same tier; switching tier starts the new one now.
        start = ad.tier_until if (ad.tier == order.tier and ad.tier_until and ad.tier_until > now) else now
        ad.tier = order.tier
        ad.tier_until = start + timedelta(days=order.days)
        ad.expires_at = max(ad.expires_at or now, ad.tier_until)
        if ad.status == Ad.Status.ACTIVE:
            ad.bumped_at = now
        ad.grant_plan_extras()
    else:
        start = ad.expires_at if ad.expires_at and ad.expires_at > now else now
        ad.expires_at = start + timedelta(days=order.days)
    ad.save(update_fields=["tier", "tier_until", "expires_at", "bumped_at", "free_listing", "top_until", "slider_until", "updated_at"])
    order.applied_at = now
    order.save(update_fields=["applied_at", "updated_at"])
    order.apply_addons()  # live ads only; otherwise Ad.approve() does it


def refresh_order(order: Order) -> Order:
    """Ask the gateway for the current payment state (used by the return page)."""
    if order.status != Order.Status.PENDING or not order.gateway_payment_id:
        return order
    gateway = get_gateway()
    if gateway.name != order.gateway:
        return order
    try:
        state = gateway.fetch(order.gateway_payment_id)
    except PaymentError:
        return order
    return apply_gateway_state(order.pk, state)


def refund_order(order: Order) -> Order:
    """Refund the full amount. The ad keeps its tier; staff can change it in the admin."""
    if order.status != Order.Status.PAID:
        raise PaymentError("Only paid orders can be refunded.")
    gateway = get_gateway()
    if gateway.name != order.gateway:
        raise PaymentError(f"Order was paid with {order.gateway}, which is not the active gateway.")
    result = gateway.refund(order.gateway_payment_id, order.amount)
    if not result.get("accepted", True):
        raise PaymentError("The gateway did not accept the refund.")
    order.status = Order.Status.REFUNDED
    order.save(update_fields=["status", "updated_at"])
    from .economy import reverse_order_coins

    reverse_order_coins(order)
    return order
