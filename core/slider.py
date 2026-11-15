"""Front-page slider slots: only Site settings → Slider slots ads can be in the slider at the same time.

A slot is taken by a live ad whose "Front page ad" add-on is running, and by a paid order whose
"Front page ad" waits for the ad to be approved (so slots can't be oversold while ads are in review).
"""

from django.utils import timezone

from .models import Ad, AddOn, Order, SiteSettings


def _waiting_ad_ids():
    """Ads with a paid, not yet started Front page ad (in review)."""
    ids = set()
    for order in Order.objects.filter(status=Order.Status.PAID, addons_applied=False).exclude(addons=[]).only("ad_id", "addons"):
        if order.ad_id and any(a.get("effect") == AddOn.Effect.SLIDER for a in order.addons):
            ids.add(order.ad_id)
    return ids


def slots(for_ad=None) -> dict:
    """{total, used, free, next_free_at, has_slot}. `has_slot`: this ad already holds a slot (it may extend)."""
    now = timezone.now()
    total = SiteSettings.load().slider_slots
    running = Ad.objects.live().filter(slider_until__gt=now)
    holders = set(running.values_list("id", flat=True)) | _waiting_ad_ids()
    used = len(holders)
    next_free = running.order_by("slider_until").values_list("slider_until", flat=True).first() if used >= total else None
    return {
        "total": total,
        "used": min(used, total),
        "free": max(0, total - used),
        "next_free_at": next_free,
        "has_slot": bool(for_ad is not None and for_ad.pk in holders),
    }


def check_free(ad, addons):
    """Raise if a Front page ad is chosen but every slot is taken (an ad that already holds a slot may extend)."""
    from rest_framework.exceptions import ValidationError

    if not any(a.effect == AddOn.Effect.SLIDER for a in addons):
        return
    info = slots(ad)
    if not info["has_slot"] and info["free"] == 0:
        when = f" The next one frees up on {timezone.localtime(info['next_free_at']):%d %b}." if info["next_free_at"] else ""
        raise ValidationError({"addons": f"All {info['total']} front-page slider slots are taken.{when}"})
