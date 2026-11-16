"""Fill the database with clearly fictional demo data.

Pictures are generated abstract gradients (no photos of people), so the demo
never contains real persons. Run: uv run python manage.py seed_demo
"""

import random
from datetime import timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from core import taxonomy
from core.models import (
    Accessibility,
    Ad,
    AdPicture,
    Audience,
    Category,
    Gender,
    Heart,
    Region,
    Tag,
    Tier,
    Topic,
    User,
)

NAMES = [
    "Aurora", "Bella", "Cleo", "Dahlia", "Elena", "Freja", "Gaia", "Hana", "Iris",
    "Juno", "Kira", "Luna", "Maya", "Nova", "Opal", "Pia", "Rosa", "Sofia",
    "Thea", "Vega", "Willow", "Yara", "Zoe", "Alex", "Noah", "Leo", "Mila & Theo",
]
# Same areas and towns as migration 0046_move_ads_to_nepal.
CITIES = {
    "kathmandu": ["Thamel", "Baneshwor", "Boudha", "Lazimpat", "Maharajgunj", "Kalanki"],
    "lalitpur": ["Patan", "Jawalakhel", "Pulchowk", "Sanepa", "Kupondole"],
    "bhaktapur": ["Bhaktapur", "Suryabinayak", "Thimi"],
    "pokhara": ["Lakeside", "Pokhara", "Damside"],
    "chitwan": ["Bharatpur", "Sauraha", "Narayangarh"],
    "biratnagar": ["Biratnagar", "Itahari"],
    "butwal": ["Butwal", "Bhairahawa"],
    "dharan": ["Dharan", "Dhankuta"],
    "nepalgunj": ["Nepalgunj", "Kohalpur"],
    "janakpur": ["Janakpur", "Dhalkebar"],
}
TAGS = ["Massage", "Relaxation", "Hot stone", "Couples", "Wellness", "Body to body",
        "Weekend", "Outcall", "Incall", "English speaking", "Discreet"]
PALETTES = [
    ("#7c3aed", "#ec4899"), ("#0ea5e9", "#a855f7"), ("#f43f5e", "#f59e0b"),
    ("#14b8a6", "#6366f1"), ("#be185d", "#7c2d12"), ("#4f46e5", "#06b6d4"),
]
LOREM = (
    "This is a demo listing created by the seed script. All names, numbers and "
    "pictures are fictional. Replace this text with the advertiser's own "
    "description of services, availability and booking rules."
)


def gradient_picture(label: str, seed: int) -> ContentFile:
    rnd = random.Random(seed)
    a, b = rnd.choice(PALETTES)
    w, h = 600, 800
    img = Image.new("RGB", (w, h), a)
    top = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    bot = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    for _ in range(6):
        r = rnd.randint(40, 160)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255), width=2)
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:
        font = ImageFont.load_default()
    draw.text((w / 2, h / 2), label, fill="white", anchor="mm", font=font)
    draw.text((w / 2, h - 40), "DEMO", fill=(255, 255, 255), anchor="mm", font=font.font_variant(size=24) if hasattr(font, "font_variant") else font)
    out = BytesIO()
    img.save(out, "WEBP", quality=80)
    return ContentFile(out.getvalue(), name="demo.webp")


class Command(BaseCommand):
    help = "Create demo users, ads, tags and content pages."

    def add_arguments(self, parser):
        parser.add_argument("--ads", type=int, default=36)
        parser.add_argument("--reset", action="store_true", help="Delete existing demo data first")

    @transaction.atomic
    def handle(self, *args, ads, reset, **opts):
        if reset:
            User.objects.filter(email__endswith="@demo.velora.test").delete()
            Topic.objects.all().delete()

        admin, created = User.objects.get_or_create(
            email="admin@demo.velora.test",
            defaults={"display_name": "Admin", "is_staff": True, "is_superuser": True, "age_verified": True},
        )
        if created:
            admin.set_password("admin12345")
            admin.save()

        visitor, created = User.objects.get_or_create(
            email="visitor@demo.velora.test",
            defaults={"display_name": "Visitor", "adult_confirmed_at": timezone.now()},
        )
        if created:
            visitor.set_password("visitor12345")
            visitor.save()

        tags = [Tag.objects.get_or_create(name=t)[0] for t in TAGS]
        rnd = random.Random(42)
        now = timezone.now()

        audience_values = [o["value"] for o in taxonomy.lists()["options"]["audience"]] or list(Audience.values)
        for i in range(ads):
            name = NAMES[i % len(NAMES)]
            email = f"advertiser{i + 1}@demo.velora.test"
            owner, created = User.objects.get_or_create(
                email=email,
                defaults={"display_name": name, "is_advertiser": True,
                          "adult_confirmed_at": now},
            )
            if created:
                owner.set_password("advertiser12345")
                owner.save()
            if owner.ads.exists():
                continue
            region = rnd.choice([r["value"] for r in taxonomy.lists()["regions"]] or Region.values)
            category = rnd.choice([Category.MASSAGE, Category.ESCORT, Category.TANTRIC, Category.CLINIC, Category.MASSAGE])
            gender = Gender.COUPLE if "&" in name else (Gender.MALE if name in {"Alex", "Noah", "Leo"} else Gender.FEMALE)
            published = now - timedelta(hours=rnd.randint(1, 24 * 20))
            ad = Ad.objects.create(
                owner=owner,
                title=f"{name} – {Category(category).label}",
                headline=rnd.choice([
                    "Relaxing sessions in a calm, private setting",
                    "Experienced, friendly and discreet",
                    "New in town – weekday and weekend times",
                    "Professional wellness treatments",
                ]),
                description=LOREM,
                category=category,
                gender=gender,
                audiences=rnd.sample(audience_values, k=rnd.randint(1, len(audience_values))),
                region=region,
                city=rnd.choice(CITIES.get(region) or [""]),
                age=rnd.randint(21, 45),
                languages=rnd.choice(["Nepali, English", "English", "English, Hindi", "Nepali"]),
                phone=f"+45 00 00 {i:02d} {rnd.randint(10, 99)}",
                accessibility=rnd.choice(Accessibility.values),
                tier=rnd.choices(Tier.values, weights=[5, 3, 2, 1])[0],
                status=Ad.Status.ACTIVE,
                published_at=published,
                bumped_at=published,
                expires_at=now + timedelta(days=30),
                view_count=rnd.randint(20, 4000),
            )
            if ad.tier != Tier.FREE:
                ad.tier_until = now + timedelta(days=rnd.randint(3, 30))
                ad.save(update_fields=["tier_until"])
            ad.tags.set(rnd.sample(tags, k=3))
            for p in range(rnd.randint(2, 4)):
                pic = AdPicture(ad=ad, width=600, height=800, position=p)
                pic.image.save("demo.webp", gradient_picture(name, i * 10 + p), save=True)

        # A few hearts from the visitor
        for ad in Ad.objects.order_by("?")[:8]:
            if not Heart.objects.filter(user=visitor, ad=ad).exists():
                Heart.objects.create(user=visitor, ad=ad)
        for ad in Ad.objects.all():
            ad.heart_count = ad.hearts.count() + rnd.randint(0, 60)
            ad.save(update_fields=["heart_count"])

        pages = [
            ("page", "about", "About us", True, "We are a listings platform for adult wellness and companionship services. Every advertiser is verified as 18+ before their ad goes live."),
            ("page", "terms", "Terms of use", True, "## Who can use the site\n\nYou must be 18 or older.\n\n## Advertisers\n\nAdvertisers must be at least 18, advertise only themselves, and must not be working under pressure from anyone. Ads are reviewed before publishing.\n\n*Replace this text with terms written by your lawyer.*"),
            ("page", "privacy", "Privacy policy", True, "We store the minimum data needed to run the service. Picture metadata (including location) is removed on upload. You can delete your account at any time.\n\n*Replace this text with a GDPR-compliant policy.*"),
            ("page", "safety", "Safety & reporting", True, "If you believe someone is under 18 or being forced, use **Report** on the ad. Urgent reports are reviewed first. In an emergency, call the police on 100."),
            ("page", "faq", "FAQ", True, "### How do I create an ad?\nSign up as an advertiser, verify your age, then create your ad.\n\n### How long is an ad live?\nAds stay live for 30 days and can be renewed."),
            ("page", "contact", "Contact us", True, "Questions about your ad, payments or your account? Write to us and we reply within one working day.\n\nTo report an ad, use **Report** on the ad itself – urgent reports are reviewed first."),
            ("article", "first-visit-guide", "A guide to your first massage visit", False, "Arrive on time, communicate clearly, respect boundaries. *(Demo article.)*"),
            ("blog", "welcome", "Welcome to the new site", False, "We launched a faster, safer site. *(Demo blog post.)*"),
        ]
        from core.site_content import PAGES
        for order, (kind, slug, title, menu, body) in enumerate(pages):
            summary = body.split("\n")[0][:200]
            if kind == "page" and slug in PAGES:
                summary, body = PAGES[slug]  # the full default text
            # Only add missing pages – never overwrite text edited in the admin.
            Topic.objects.get_or_create(
                kind=kind, slug=slug,
                defaults={"title": title, "body": body, "show_in_menu": menu, "menu_order": order, "summary": summary},
            )

        from django.core.management import call_command
        call_command("seed_dictionary", stdout=self.stdout)
        call_command("seed_guides", stdout=self.stdout)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {Ad.objects.count()} ads, {Tag.objects.count()} tags, {Topic.objects.count()} pages.\n"
            "Logins (demo only): admin@demo.velora.test / admin12345, "
            "visitor@demo.velora.test / visitor12345, advertiser1@demo.velora.test / advertiser12345"
        ))
