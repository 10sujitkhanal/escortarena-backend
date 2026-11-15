"""Account e-mails: confirmation links are signed (no DB table) and expire after 3 days."""

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail

from .models import SiteSettings, User

SALT = "velora.email-confirm"
MAX_AGE = 3 * 24 * 3600


class InvalidToken(Exception):
    pass


def make_token(user: User) -> str:
    # Bound to the current address: changing e-mail again invalidates older links.
    return signing.dumps({"u": user.pk, "e": user.email}, salt=SALT)


def read_token(token: str) -> User:
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.SignatureExpired as exc:
        raise InvalidToken("This link has expired. Send a new one from Settings.") from exc
    except signing.BadSignature as exc:
        raise InvalidToken("This link is not valid.") from exc
    user = User.objects.filter(pk=data.get("u"), email=data.get("e"), is_active=True).first()
    if not user:
        raise InvalidToken("This link is no longer valid – the e-mail address has changed.")
    return user


def site_email() -> str:
    """The site's e-mail address from Admin → Site settings (falls back to the CONTACT_EMAIL setting)."""
    return SiteSettings.load().contact_email or settings.DEFAULT_FROM_EMAIL


def sender(user: User) -> tuple[str, str]:
    """(From header, subject prefix) honouring the user's discretion setting."""
    site = SiteSettings.load()
    address = site_email()
    if user.sender_name == User.SenderName.NEUTRAL:
        return f"Notification <{address}>", "Notification"
    return f"{site.site_name} <{address}>", site.site_name


def send_confirmation(user: User) -> None:
    from_header, prefix = sender(user)
    link = f"{settings.PUBLIC_SITE_URL}/confirm-email?token={make_token(user)}"
    send_mail(
        subject=f"{prefix}: please confirm your e-mail",
        message=(
            "Hi,\n\nPlease confirm your e-mail address by opening this link:\n\n"
            f"{link}\n\nThe link works for 3 days. If you didn't ask for this, you can ignore this e-mail."
        ),
        from_email=from_header,
        recipient_list=[user.email],
        fail_silently=False,
    )


REPORT_SALT = "velora.report-confirm"


def send_report_confirmation(report) -> None:
    site = SiteSettings.load()
    token = signing.dumps({"r": report.pk}, salt=REPORT_SALT)
    send_mail(
        subject=f"{site.site_name}: confirm your report",
        message=(
            f"Hi {report.reporter_name},\n\nYou reported the ad \"{report.ad.title}\".\n"
            "Your report is saved as a draft. Confirm it to send it to our moderators:\n\n"
            f"{settings.PUBLIC_SITE_URL}/confirm-report?token={token}\n\nThe link works for 3 days."
        ),
        from_email=f"{site.site_name} <{site_email()}>",
        recipient_list=[report.contact_email],
        fail_silently=True,
    )


def read_report_token(token: str):
    from .models import Report

    try:
        data = signing.loads(token, salt=REPORT_SALT, max_age=MAX_AGE)
    except signing.BadSignature as exc:
        raise InvalidToken("This link is not valid or has expired.") from exc
    report = Report.objects.filter(pk=data.get("r")).first()
    if not report:
        raise InvalidToken("This report no longer exists.")
    return report
