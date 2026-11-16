"""Send e-mail through the aaPanel (BT Panel) Mail Server "send mail" HTTP API.

Use it with EMAIL_BACKEND=core.mail_backends.BTPanelEmailBackend and these settings in backend/.env:

  BTPANEL_MAIL_URL=https://<server-ip>:<panel-port>/mail_sys/send_mail_http.json
  BTPANEL_MAIL_FROM=info@yourdomain.com          # a mailbox created in aaPanel → Mail Server
  BTPANEL_MAIL_PASSWORD=<that mailbox's password>
  BTPANEL_MAIL_VERIFY_SSL=false                   # the panel uses a self-signed certificate

aaPanel sends every e-mail from BTPANEL_MAIL_FROM; the display name ("KingOfBabes") and Reply-To of the
message are not supported by this API, so replies go to that mailbox.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger(__name__)


class BTPanelMailError(Exception):
    pass


class BTPanelEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.url = settings.BTPANEL_MAIL_URL
        self.mail_from = settings.BTPANEL_MAIL_FROM
        self.password = settings.BTPANEL_MAIL_PASSWORD
        self.context = None if settings.BTPANEL_MAIL_VERIFY_SSL else ssl._create_unverified_context()

    def _post(self, data: dict) -> dict:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=settings.EMAIL_TIMEOUT, context=self.context) as res:
            raw = res.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise BTPanelMailError(f"Unexpected answer from the mail server: {raw[:200]}") from None

    def send_messages(self, email_messages) -> int:
        if not email_messages:
            return 0
        if not (self.url and self.mail_from and self.password):
            if self.fail_silently:
                return 0
            raise BTPanelMailError("Set BTPANEL_MAIL_URL, BTPANEL_MAIL_FROM and BTPANEL_MAIL_PASSWORD in backend/.env.")
        sent = 0
        for message in email_messages:
            html = next((c for c, mime in getattr(message, "alternatives", []) if mime == "text/html"), None)
            recipients = message.recipients()
            if not recipients:
                continue
            data = {
                "mail_from": self.mail_from,
                "password": self.password,
                "mail_to": ",".join(recipients),
                "subject": message.subject,
                "content": html or message.body,
                "subtype": "html" if html else "plain",
            }
            try:
                answer = self._post(data)
                if not answer.get("status"):
                    raise BTPanelMailError(str(answer.get("msg") or answer))
                sent += 1
            except Exception as exc:
                log.warning("aaPanel mail to %s failed: %s", ",".join(recipients), exc)
                if not self.fail_silently:
                    raise
        return sent
