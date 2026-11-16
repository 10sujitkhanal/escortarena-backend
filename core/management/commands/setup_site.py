"""Make a fresh install ready to use – one command after `migrate`.

    manage.py migrate
    manage.py setup_site --admin-email you@example.com          # asks for the password
    manage.py setup_site --admin-email you@example.com --demo   # + fictional ads (testing only)

What it does (safe to run again – it only adds what is missing and never overwrites admin edits):
  1. Site settings (name, colours, country Nepal, e-mail …) with the defaults
  2. Info pages: About, FAQ, Contact, Safety, Terms, Privacy
  3. FAQ questions
  4. All countries, regions and cities (GeoNames); Nepal's areas and cities switched on
  5. Dictionary, Articles, Blogs and Tags (starter guide content)
  6. Checks that plans, prices, coin packages, extras, categories and options exist (created by the migrations)
  7. An admin login (optional)
  No ads and no users are created – except with --demo (fictional ads, for testing only).
"""

import getpass
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import (
    AdCategory,
    AddOn,
    AdPlace,
    AdRegion,
    ChoiceOption,
    Country,
    FaqItem,
    Plan,
    Product,
    SiteSettings,
    Tag,
    Topic,
    User,
)
from core.site_content import FAQ, PAGE_TITLES, PAGES


class Command(BaseCommand):
    help = "Seed everything a fresh site needs (settings, pages, FAQ, countries/areas/cities, admin). Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--admin-email", help="Create this admin login if it doesn't exist yet")
        parser.add_argument("--admin-password", help="Password for the admin (or set DJANGO_SUPERUSER_PASSWORD; asked if missing)")
        parser.add_argument("--country", default="NP", help="Country whose areas and cities are switched on (default NP)")
        parser.add_argument("--skip-world", action="store_true", help="Don't load all countries/regions/cities (faster)")
        parser.add_argument("--no-guides", action="store_true", help="Leave out the starter Dictionary, Articles, Blogs and Tags")
        parser.add_argument("--demo", action="store_true", help="Also add fictional demo users and ads (testing only)")

    def ok(self, text):
        self.stdout.write(self.style.SUCCESS("  ✓ ") + text)

    def handle(self, *args, **o):
        self.stdout.write(self.style.MIGRATE_HEADING("Setting up the site …"))

        with transaction.atomic():
            # 1. Site settings – load() creates the record with the model defaults.
            s = SiteSettings.load()
            self.ok(f"Site settings: {s.site_name}, country {s.country or '–'}, e-mail {s.contact_email}")

            # 2. Info pages – created only if missing, so edits in the admin are kept.
            made = 0
            for order, (slug, title, menu) in enumerate(PAGE_TITLES):
                summary, body = PAGES[slug]
                _, created = Topic.objects.get_or_create(
                    kind=Topic.Kind.PAGE, slug=slug,
                    defaults={"title": title, "summary": summary, "body": body, "show_in_menu": menu,
                              "menu_order": order, "is_published": True},
                )
                made += created
            self.ok(f"Info pages: {made} added, {Topic.objects.filter(kind=Topic.Kind.PAGE).count()} in total")

            # 3. FAQ – only when there are no questions yet.
            if not FaqItem.objects.exists():
                FaqItem.objects.bulk_create(
                    [FaqItem(category=c, question=q, answer=a, order=i) for i, (c, q, a) in enumerate(FAQ)]
                )
            self.ok(f"FAQ: {FaqItem.objects.count()} questions")

        # 4. Countries, regions and cities.
        code = o["country"].upper()
        if o["skip_world"]:
            self.stdout.write("  – Countries/regions/cities skipped (--skip-world)")
        else:
            self.stdout.write("  … loading countries, regions and cities (takes a few seconds)")
            call_command("seed_world", activate=[code], stdout=open(os.devnull, "w"))
            Country.objects.filter(code=code).update(is_active=True)
            self.ok(
                f"Places: {Country.objects.count()} countries, {AdRegion.objects.count()} regions, "
                f"{AdPlace.objects.count()} cities – {code}: "
                f"{AdRegion.objects.filter(country__code=code, is_active=True).count()} areas and "
                f"{AdPlace.objects.filter(region__country__code=code, is_active=True).count()} cities switched on"
            )

        # 5. Lookups and prices come from the migrations – just check they are there.
        checks = {
            "plans": Plan.objects.count(),
            "plan prices": Product.objects.filter(kind=Product.Kind.PLAN).count(),
            "coin packages": Product.objects.filter(kind=Product.Kind.DIAMONDS).count(),
            "extras": AddOn.objects.count(),
            "categories": AdCategory.objects.count(), "options": ChoiceOption.objects.count(),
        }
        missing = [k for k, n in checks.items() if not n]
        if missing:
            raise CommandError(f"Missing {', '.join(missing)} – run `manage.py migrate` first.")
        self.ok(", ".join(f"{n} {k}" for k, n in checks.items()))

        # Optional content.
        if not o["no_guides"]:
            if not Topic.objects.filter(kind=Topic.Kind.DICTIONARY).exists():
                call_command("seed_dictionary", stdout=open(os.devnull, "w"))
            if not Topic.objects.filter(kind__in=[Topic.Kind.ARTICLE, Topic.Kind.BLOG]).exists():
                call_command("seed_guides", stdout=open(os.devnull, "w"))
            self.ok(
                f"Guides: {Topic.objects.filter(kind=Topic.Kind.DICTIONARY).count()} dictionary entries, "
                f"{Topic.objects.filter(kind=Topic.Kind.ARTICLE).count()} articles, "
                f"{Topic.objects.filter(kind=Topic.Kind.BLOG).count()} blogs, {Tag.objects.count()} tags"
            )
        if o["demo"]:
            call_command("seed_demo", stdout=open(os.devnull, "w"))
            self.ok("Demo users and ads added (log in as advertiser1@demo.velora.test – see seed_demo)")

        # 6. Admin login.
        self.admin(o.get("admin_email"), o.get("admin_password"))

        self.stdout.write(self.style.SUCCESS("\nDone – the site is ready. Start the server and open /admin to adjust Site settings."))

    def admin(self, email, password):
        if not email:
            if User.objects.filter(is_superuser=True).exists():
                self.ok("Admin login already exists")
            else:
                self.stdout.write(self.style.WARNING("  ! No admin login yet – run again with --admin-email you@example.com"))
            return
        email = User.objects.normalize_email(email.strip())
        user = User.objects.filter(email__iexact=email).first()
        if user and user.is_superuser:
            self.ok(f"Admin {email} already exists")
            return
        password = password or os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        if not password:
            password = getpass.getpass(f"Password for {email}: ")
            if password != getpass.getpass("Again: "):
                raise CommandError("The passwords don't match.")
        if len(password) < 8:
            raise CommandError("Use a password of at least 8 characters.")
        if user:
            user.is_staff = user.is_superuser = True
            user.set_password(password)
            user.save()
        else:
            User.objects.create_superuser(email=email, password=password)
        self.ok(f"Admin login {email} ready – sign in at /admin")
