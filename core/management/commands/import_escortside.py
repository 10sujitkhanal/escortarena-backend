"""Move users and ads from escortside.dk (see core/legacy.py).

    python manage.py import_escortside --wipe                           # delete all ads + users (not admins), import
    python manage.py import_escortside                                  # import only (safe to run again)
    python manage.py import_escortside --send --test-to you@example.com  # test: 1 invite e-mail, only to you
    python manage.py import_escortside --send                           # real: invite every owner not invited yet

Files: data/escortside/users/ (data.csv + one picture folder per user, "123_Name/images/…") and
data/escortside/ads/ (data.csv + "ad45_Name/images/…"), or --users / --ads with other folders.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import legacy
from core.management.commands._csv import read_csv
from core.models import LegacyAccount

DATA = Path(settings.BASE_DIR) / "data" / "escortside"


def csv_and_folder(path: str) -> tuple[Path, Path]:
    p = Path(path)
    return (p / "data.csv", p) if p.is_dir() else (p, p.parent)


class Command(BaseCommand):
    help = "Import escortside.dk users and ads (Free plan, with pictures) and optionally e-mail the claim links."

    def add_arguments(self, parser):
        parser.add_argument("--users", default=str(DATA / "users"), help="Folder with data.csv + picture folders")
        parser.add_argument("--ads", default=str(DATA / "ads"), help="Folder with data.csv + picture folders")
        parser.add_argument("--wipe", action="store_true",
                            help="First DELETE every ad and every user except staff/superusers")
        parser.add_argument("--yes", action="store_true", help="With --wipe: don't ask for confirmation")
        parser.add_argument("--draft", action="store_true", help="Keep all imported ads as drafts (not live)")
        parser.add_argument("--skip-inactive", action="store_true", help="Leave out accounts that were inactive")
        parser.add_argument("--send", action="store_true", help="E-mail the claim link to owners not invited yet")
        parser.add_argument("--test-to", metavar="EMAIL",
                            help="With --send: send the e-mails only to this address (owners are not marked invited)")
        parser.add_argument("--limit", type=int, default=None,
                            help="With --send: at most this many e-mails (default 1 with --test-to, else all)")
        parser.add_argument("--resend", action="store_true", help="With --send: also owners who were already invited")

    def handle(self, *args, **opts):
        users_csv, users_dir = csv_and_folder(opts["users"])
        ads_csv, ads_dir = csv_and_folder(opts["ads"])
        users, ads = read_csv(users_csv), read_csv(ads_csv)

        if opts["wipe"]:
            if not opts["yes"] and input("Delete ALL ads and ALL users except admins? Type yes: ").strip() != "yes":
                raise CommandError("Stopped – nothing deleted.")
            gone = legacy.wipe_ads_and_users()
            self.stdout.write(self.style.WARNING(f"Deleted {gone['ads']} ads and {gone['users']} users."))

        self.stdout.write(f"Importing {len(users)} users (pictures from {users_dir}) …")
        stats = legacy.import_rows(users, ads, include_inactive=not opts["skip_inactive"], user_media=users_dir,
                                   ad_media=ads_dir, go_live=not opts["draft"])
        self.stdout.write(self.style.SUCCESS(
            f"Users: {stats['users_created']} new, {stats['users_updated']} updated, "
            f"{stats['users_merged']} merged into an account with the same e-mail · ads created: "
            f"{stats['ads_created']} ({stats['ads_live']} live, the rest drafts) · "
            f"{stats['pictures']} pictures, {stats['videos']} videos"))
        for item in stats["added"]:
            self.stdout.write(f"  added {item}")
        for name, why in stats["skipped"]:
            self.stdout.write(f"  skipped {name}: {why}")
        for p in stats["problems"]:
            self.stdout.write(self.style.WARNING(f"  {p}"))

        if not opts["send"]:
            self.stdout.write("No e-mails sent (add --send).")
            return
        test_to = opts["test_to"]
        limit = opts["limit"] if opts["limit"] is not None else (1 if test_to else None)
        qs = LegacyAccount.objects.filter(claimed_at__isnull=True).select_related("user").order_by("-legacy_id")
        if not opts["resend"] and not test_to:
            qs = qs.filter(invited_at__isnull=True)
        seen, sent = set(), 0
        for acc in qs:
            if limit is not None and sent >= limit:
                break
            if acc.user_id in seen:  # one e-mail per account, even if two old profiles were merged
                continue
            seen.add(acc.user_id)
            try:
                legacy.send_invite(acc, to=test_to)
            except Exception as exc:  # noqa: BLE001 – report and go on with the next owner
                self.stderr.write(f"  could not e-mail {acc.legacy_username}: {exc}")
                continue
            sent += 1
        where = f"to {test_to} (test – owners not marked invited)" if test_to else "to the owners"
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} invite e-mail(s) {where}."))
