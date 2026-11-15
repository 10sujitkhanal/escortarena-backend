"""Every e-mail the site sends goes through `send()`: one branded layout (HTML + plain text).

Settings (backend/.env):
  EMAIL_BRAND_NAME    – name shown in the From header and the e-mail header (default: the site name)
  DEFAULT_FROM_EMAIL  – the address e-mails are sent from (must be allowed by your SMTP provider)
  EMAIL_BACKEND / EMAIL_HOST / EMAIL_PORT / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD / EMAIL_USE_TLS – SMTP

Users who chose a discreet sender (Settings → Discretion) get "Notification" as sender and subject, and no
brand name anywhere in the e-mail.
"""

from __future__ import annotations

import logging
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

log = logging.getLogger(__name__)

ACCENT = "#e94fc2"


def brand() -> str:
    if settings.EMAIL_BRAND_NAME:
        return settings.EMAIL_BRAND_NAME
    from .models import SiteSettings
    return SiteSettings.load().site_name


def reply_to() -> str:
    from .models import SiteSettings
    return SiteSettings.load().contact_email or settings.DEFAULT_FROM_EMAIL


def _discreet(user) -> bool:
    from .models import User
    return bool(user is not None and getattr(user, "sender_name", "") == User.SenderName.NEUTRAL)


def link(path: str) -> str:
    return path if path.startswith("http") else f"{settings.PUBLIC_SITE_URL}{path}"


def render(*, heading: str, lines: list[str], button: tuple[str, str] | None, note: str, name: str) -> tuple[str, str]:
    """(plain text, html) for one e-mail."""
    text = "\n\n".join([heading, *lines] + ([f"{button[0]}: {link(button[1])}"] if button else []) + ([note] if note else []))
    text += f"\n\n— {name}"

    paras = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#3b3442">{escape(p)}</p>' for p in lines)
    btn = ""
    if button:
        btn = (f'<p style="margin:22px 0 6px"><a href="{escape(link(button[1]))}" '
               f'style="display:inline-block;background:{ACCENT};color:#fff;text-decoration:none;font-weight:700;'
               f'font-size:15px;padding:12px 22px;border-radius:999px">{escape(button[0])}</a></p>'
               f'<p style="margin:0 0 14px;font-size:12px;color:#8a8291;word-break:break-all">'
               f'Or open this link: {escape(link(button[1]))}</p>')
    foot = f'<p style="margin:18px 0 0;font-size:12px;line-height:1.5;color:#8a8291">{escape(note)}</p>' if note else ""
    html = f"""<!doctype html><html><body style="margin:0;background:#f5f1f6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f1f6;padding:28px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:18px;overflow:hidden">
<tr><td style="background:#1d0f1f;padding:18px 26px;font-size:18px;font-weight:800;color:#fff">
<span style="display:inline-block;width:10px;height:10px;border-radius:3px;background:{ACCENT};margin-right:8px"></span>{escape(name)}</td></tr>
<tr><td style="padding:26px">
<h1 style="margin:0 0 16px;font-size:21px;line-height:1.3;color:#1d0f1f">{escape(heading)}</h1>
{paras}{btn}{foot}
</td></tr></table>
<p style="font-size:11px;color:#8a8291;margin:14px 0 0">{escape(name)} · {escape(settings.PUBLIC_SITE_URL)}</p>
</td></tr></table></body></html>"""
    return text, html


def send(*, to: str, subject: str, heading: str, lines: list[str], button: tuple[str, str] | None = None,
         note: str = "", user=None, reply: str | None = None, background: bool = True, fail_silently: bool = True) -> None:
    """Send one e-mail. `user` (the recipient) decides the discreet sender; `background` sends after commit."""
    discreet = _discreet(user)
    name = "Notification" if discreet else brand()
    text, html = render(heading=heading, lines=lines, button=button, note=note, name=name)
    msg = EmailMultiAlternatives(
        subject=f"{name}: {subject}",
        body=text,
        from_email=f"{name} <{settings.BTPANEL_MAIL_FROM or settings.DEFAULT_FROM_EMAIL}>",
        to=[to],
        reply_to=[reply or reply_to()],
    )
    msg.attach_alternative(html, "text/html")

    def go():
        try:
            msg.send(fail_silently=False)
        except Exception:
            log.exception("Could not send e-mail %r to %s", subject, to)
            if not fail_silently:
                raise

    if background:
        from .messaging import _in_background
        _in_background(go)
    else:
        go()
