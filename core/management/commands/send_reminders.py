"""Send 'ad expires soon', 'ad expired' and 'plan ends' notifications. Run from cron, e.g. every 15 minutes."""

from django.core.management.base import BaseCommand

from core.notifications import sweep


class Command(BaseCommand):
    help = "Send ad expiry and plan reminders (each one only once)."

    def handle(self, *args, **options):
        self.stdout.write(f"Sent {sweep(force=True)} reminder(s).")
