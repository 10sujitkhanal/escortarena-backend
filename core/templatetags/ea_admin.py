"""Small template helpers for the admin (templates/admin/*)."""

from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def ago(value) -> str:
    """'now', '5m', '3h', '2d', '6w' – short time since `value`."""
    if not value:
        return ""
    secs = max(0, int((timezone.now() - value).total_seconds()))
    for size, unit in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{secs // size}{unit}"
    return "now"
