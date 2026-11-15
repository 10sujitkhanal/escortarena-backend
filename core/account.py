"""Account e-mails: confirmation links are signed (no DB table) and expire after 3 days."""

from django.conf import settings
from django.core import signing

from . import emails
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
    name = "Notification" if user.sender_name == User.SenderName.NEUTRAL else emails.brand()
    return f"{name} <{settings.DEFAULT_FROM_EMAIL}>", name


# ---------------------------------------------------------------------------
# Account e-mails
# ---------------------------------------------------------------------------


def send_confirmation(user: User, *, welcome: bool = False) -> None:
    link = f"/confirm-email?token={make_token(user)}"
    lines = (["Thanks for joining – your account is ready."] if welcome else []) + [
        "Please confirm your e-mail address so we can reach you about your account, messages and payments.",
    ]
    emails.send(to=user.email, user=user, subject="Welcome – please confirm your e-mail" if welcome else "please confirm your e-mail",
                heading="Welcome!" if welcome else "Confirm your e-mail", lines=lines, button=("Confirm my e-mail", link),
                note="The link works for 3 days. If you didn't create this account, you can ignore this e-mail.",
                background=False, fail_silently=False)


def reset_link(user: User) -> str:
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return f"/reset-password?uid={uid}&token={default_token_generator.make_token(user)}"


def read_reset_link(uid: str, token: str) -> User:
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_str
    from django.utils.http import urlsafe_base64_decode
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(uid)), is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        raise InvalidToken("This reset link is not valid. Ask for a new one.") from None
    if not default_token_generator.check_token(user, token):
        raise InvalidToken("This reset link has expired or was already used. Ask for a new one.")
    return user


def send_password_reset(user: User) -> None:
    hours = max(1, settings.PASSWORD_RESET_TIMEOUT // 3600)
    emails.send(to=user.email, user=user, subject="reset your password", heading="Reset your password",
                lines=["Someone (hopefully you) asked to reset the password for this account.",
                       "Click the button to choose a new password."],
                button=("Choose a new password", reset_link(user)),
                note=f"The link works for {hours} hour{'s' if hours > 1 else ''} and only once. "
                     "If you didn't ask for this, ignore this e-mail – your password stays the same.")


def send_password_changed(user: User) -> None:
    emails.send(to=user.email, user=user, subject="your password was changed", heading="Your password was changed",
                lines=["The password for your account was just changed.",
                       "If this was you, there is nothing more to do."],
                button=("Reset my password", "/forgot-password"),
                note="If you did NOT change it, reset your password right away and contact us.")


def send_email_changed(user: User, old_email: str) -> None:
    emails.send(to=old_email, user=user, subject="your e-mail address was changed", heading="Your e-mail address was changed",
                lines=[f"The e-mail address on your account was changed from {old_email} to {user.email}.",
                       "From now on we send account e-mails to the new address."],
                note="If you did NOT make this change, contact us right away by replying to this e-mail.")


LOGIN_COOKIE = "known_browser"


def login_alert(request, response, user: User, *, quiet: bool = False) -> None:
    """E-mail the user when they sign in from a browser we haven't seen for them before."""
    known = request.get_signed_cookie(LOGIN_COOKIE, default="", salt="velora.known-browser")
    if str(user.pk) not in known.split(","):
        if not quiet:  # quiet: just signed up / reset on this browser – remember it, no e-mail
            _send_login_alert(request, user)
        ids = [i for i in known.split(",") if i][-9:] + [str(user.pk)]
        response.set_signed_cookie(LOGIN_COOKIE, ",".join(ids), salt="velora.known-browser",
                                   max_age=365 * 24 * 3600, httponly=True, samesite="Lax", secure=not settings.DEBUG)


def _send_login_alert(request, user: User) -> None:
    from django.utils import timezone
    ua = request.META.get("HTTP_USER_AGENT", "")[:160] or "Unknown browser"
    ip = (request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")) or "unknown"
    when = timezone.localtime().strftime("%d %b %Y, %H:%M")
    emails.send(to=user.email, user=user, subject="new sign-in to your account", heading="New sign-in to your account",
                lines=["Your account was just signed in from a new browser.",
                       f"When: {when}", f"Browser: {ua}", f"IP address: {ip}",
                       "If this was you, there is nothing more to do."],
                button=("Not you? Reset your password", "/forgot-password"),
                note="We send this the first time you sign in on a new browser or device.")


REPORT_SALT = "velora.report-confirm"


def send_report_confirmation(report) -> None:
    token = signing.dumps({"r": report.pk}, salt=REPORT_SALT)
    emails.send(to=report.contact_email, subject="confirm your report", heading="Confirm your report",
                lines=[f"Hi {report.reporter_name},", f"You reported the ad \u201c{report.ad.title}\u201d.",
                       "Your report is saved as a draft. Confirm it to send it to our moderators."],
                button=("Confirm my report", f"/confirm-report?token={token}"), note="The link works for 3 days.")


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
