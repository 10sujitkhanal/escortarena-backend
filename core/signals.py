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
