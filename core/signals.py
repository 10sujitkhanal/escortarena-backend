"""Ask the Next.js frontend to drop cached site settings / plans after they change."""

import json
import logging
import sys
import threading
import urllib.request

from django.conf import settings
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    AdCategory,
    AdPlace,
    AdRegion,
    ChoiceOption,
    Country,
    Plan,
    Product,
    SiteSettings,
)

log = logging.getLogger(__name__)
TESTING = "test" in sys.argv


def revalidate(tag: str):
    if TESTING or not settings.NEXT_REVALIDATE_URL:
        return

    def send():
        req = urllib.request.Request(
            settings.NEXT_REVALIDATE_URL,
            data=json.dumps({"tag": tag}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Revalidate-Secret": settings.REVALIDATE_SECRET},
        )
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as exc:  # frontend down: its cache expires on its own within minutes
            log.info("Frontend revalidation for %s skipped: %s", tag, exc)

    # Not a daemon thread: a short-lived process (shell, management command) waits for it (max 5 s).
    threading.Thread(target=send, daemon=False).start()


@receiver(post_save, sender=SiteSettings)
def site_settings_changed(**kwargs):
    revalidate("site-settings")


@receiver([post_save, post_delete], sender=Plan)
@receiver([post_save, post_delete], sender=Product)
def plans_changed(**kwargs):
    revalidate("plans")


@receiver([post_save, post_delete], sender=Country)
@receiver([post_save, post_delete], sender=AdRegion)
@receiver([post_save, post_delete], sender=AdPlace)
@receiver([post_save, post_delete], sender=AdCategory)
@receiver([post_save, post_delete], sender=ChoiceOption)
def lookups_changed(**kwargs):
    revalidate("lookups")


# ---------------------------------------------------------------------------
# Login history (Admin → dashboard and each user's page)
# ---------------------------------------------------------------------------

from django.contrib.auth.signals import user_logged_in  # noqa: E402


def describe_device(ua: str) -> str:
    """'Chrome · Android' from a user-agent string (short and coarse on purpose)."""
    ua = ua or ""
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"),
                                           ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("CriOS", "Chrome"),
                                           ("Safari/", "Safari")) if key in ua), "")
    system = next((name for key, name in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                          ("Windows", "Windows"), ("Mac OS X", "Mac"), ("Linux", "Linux")) if key in ua), "")
    return " · ".join(x for x in (browser, system) if x)[:60]


def login_method(path: str) -> str:
    for part, method in (("/auth/signup", "signup"), ("/auth/claim", "claim"), ("/auth/login", "password"),
                         ("/admin/", "admin"), ("reset", "reset")):
        if part in (path or ""):
            return method
    return "other"


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs):
    from .models import LoginEvent

    import ipaddress

    meta = getattr(request, "META", {}) if request is not None else {}
    sent = getattr(request, "data", None) or {}  # DRF request: what the website sent with the sign-in
    sent = sent if hasattr(sent, "get") else {}
    ip = str(sent.get("client_ip") or "").strip()[:45]
    try:
        ip = str(ipaddress.ip_address(ip)) if ip else None
    except ValueError:
        ip = None
    country = str(sent.get("client_country") or meta.get("HTTP_CF_IPCOUNTRY") or "").strip().upper()
    try:
        LoginEvent.objects.create(
            user=user,
            method=login_method(getattr(request, "path", "")),
            ip=ip,
            country=country if len(country) == 2 and country.isalpha() and country != "XX" else "",
            device=describe_device(meta.get("HTTP_USER_AGENT", "")),
        )
    except Exception:  # noqa: BLE001 – a login must never fail because of the history
        log.exception("Could not record a login")
