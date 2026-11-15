"""Original demo content for Guide → Articles, Blogs and Tags. Safe to re-run.

Cover images are generated gradients with the title (no photos of people).
Run: uv run python manage.py seed_guides
"""

import random
from datetime import timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFont

from core.models import Ad, Tag, Topic

ARTICLES = [
    ("first-massage-visit", "Your first massage visit – what to expect",
     "A calm guide to booking, arriving and enjoying your first session.",
     "Booking your first massage can feel like a big step. Here is how it usually works.\n\n"
     "## Before you book\nRead the ad carefully: what is offered, the price, and whether it is *incall* or *outcall*. "
     "Write a short, polite message with the time you would like.\n\n"
     "## On the day\nArrive on time, freshly showered. Turn your phone to silent. Tell the masseuse about injuries or allergies.\n\n"
     "## During and after\nSay if the pressure is too hard. Afterwards, drink some water and take it easy."),
    ("spot-a-scam", "How to spot a fake ad or a scam",
     "Five warning signs – and what to do if you see them.",
     "Most advertisers are genuine, but scammers try their luck everywhere. Watch out for:\n\n"
     "1. **Requests for prepayment** with gift cards, vouchers or crypto.\n"
     "2. Prices that are far below everyone else's.\n"
     "3. Pressure to move the conversation to another app straight away.\n"
     "4. Pictures that look like they come from a magazine.\n"
     "5. Stories about fees, taxes or a 'security deposit'.\n\n"
     "If something feels wrong, stop and press **Report** on the ad. We review every report."),
    ("etiquette", "Good etiquette for clients",
     "Respect, hygiene and communication make every meeting better.",
     "Good manners go a long way.\n\n"
     "- Be clear and polite when you write.\n- Respect the boundaries in the ad – don't negotiate them.\n"
     "- Cancel in good time if your plans change.\n- Pay the agreed price without discussion.\n\n"
     "Remember that advertisers are people running a business. Treat them the way you want to be treated."),
    ("types-of-massage", "Massage styles explained",
     "From Swedish to Thai and tantric – find the style that suits you.",
     "There are many styles of massage. A short overview:\n\n"
     "- **Swedish massage** – long, flowing strokes. Perfect for relaxing.\n"
     "- **Deep tissue** – firm pressure for tight muscles.\n"
     "- **Thai massage** – stretching and pressure, usually fully clothed.\n"
     "- **Hot stone** – warm stones melt away tension.\n"
     "- **Tantric massage** – slow, mindful touch with focus on breathing.\n\n"
     "Look up any word you don't know in our [dictionary](/dictionary)."),
    ("safety-for-advertisers", "Safety tips for advertisers",
     "Practical advice to keep yourself safe and in control.",
     "Your safety comes first.\n\n"
     "- Tell a friend where you are and when you expect to be finished.\n"
     "- Screen new clients with a short call before you share an address.\n"
     "- Trust your instincts – you can always say no.\n- Keep your money and phone within reach.\n\n"
     "If you are ever pressured or threatened, contact the police on 100. Our moderators can also help remove an ad quickly."),
    ("verified-ads", "Why we verify every advertiser",
     "How age verification protects both clients and advertisers.",
     "Every advertiser on the site must prove they are at least 18 before an ad goes live. "
     "Our moderators check ID and review every new ad.\n\n"
     "Verification keeps minors off the site, makes fake ads rarer and gives clients more confidence. "
     "Look for the ✔ mark on an ad."),
]

BLOGS = [
    ("kathmandu-guide", "Kathmandu – temples, tea and time to unwind",
     "Old squares, rooftop cafés and calm treatments in the capital.",
     "Kathmandu rewards a slow pace. Start early at Boudhanath or Swayambhunath, then spend the afternoon in "
     "Thamel's quieter lanes and book a relaxing massage.\n\n"
     "Many advertisers in Kathmandu offer both incall and hotel visits – use the **Kathmandu** area filter to see who is near you."),
    ("pokhara-guide", "Pokhara – lakeside calm below the Himalaya",
     "Phewa Lake, mountain views and unhurried wellness.",
     "Pokhara is where Nepal slows down. Take a boat out on Phewa Lake, watch the sunrise over the Annapurnas from "
     "Sarangkot and end the day with a massage at Lakeside.\n\n"
     "After a trek, a deep tissue or hot stone massage helps tired legs recover."),
    ("lalitpur-guide", "Lalitpur – courtyards, crafts and quiet evenings",
     "Patan's old town is perfect for a relaxed day out.",
     "Patan Durbar Square, the Golden Temple and small craft workshops make Lalitpur easy to enjoy on foot.\n\n"
     "Jawalakhel, Pulchowk and Sanepa have several advertisers – filter by **Lalitpur** to find them."),
    ("chitwan-guide", "Chitwan – jungle days and gentle evenings",
     "Wildlife by day, rest and recovery by night.",
     "Chitwan National Park is known for rhinos, elephants and river safaris, with Sauraha and Bharatpur as good bases.\n\n"
     "After a long day outdoors, an aromatherapy or relaxation massage is the perfect way to unwind."),
    ("bhaktapur-guide", "Bhaktapur – a walk through living history",
     "Pottery squares, juju dhau and a slower rhythm.",
     "Bhaktapur's brick squares and temples feel like a step back in time. Try the famous juju dhau (king curd) "
     "and watch the potters at work.\n\n"
     "It is a short ride from Kathmandu, so many advertisers in the valley offer visits here too."),
    ("winter-wellness", "Winter wellness – beat the cold months",
     "Warmth and touch help when mornings are cold and foggy.",
     "Winter mornings in the Kathmandu valley are cold and often foggy. Regular massage, warm baths and time in the "
     "midday sun can lift your mood. Hot stone and aromatherapy massages are especially popular in the colder months."),
]

TAGS = [
    "Pokhara", "Biratnagar", "Aromatherapy", "Body to body", "Chitwan", "Clinic", "Kathmandu", "Couples",
    "Deep tissue", "Discreet", "Duo", "English speaking", "Butwal", "Evening", "Lalitpur", "Dharan",
    "German speaking", "GFE", "Hot stone", "Hotel visit", "Incall", "Nepalgunj", "Lingam", "Mature",
    "New in town", "Nuru", "Bhaktapur", "Outcall", "Overnight", "Parking", "Relaxation", "Janakpur",
    "Shower available", "Spanish speaking", "Student", "Swedish massage", "Tantra", "Thai massage",
    "Hetauda", "Weekend", "Wellness", "Wheelchair friendly", "Yoni", "24/7",
]

PALETTES = [("#7c3aed", "#ec4899"), ("#0ea5e9", "#22c55e"), ("#f43f5e", "#f59e0b"),
            ("#14b8a6", "#6366f1"), ("#be185d", "#7c2d12"), ("#4f46e5", "#06b6d4")]


FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",            # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",         # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",                  # Fedora/Alpine
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def cover(title: str, seed: int) -> ContentFile:
    rnd = random.Random(seed)
    a, b = rnd.choice(PALETTES)
    w, h = 1200, 675
    img = Image.new("RGB", (w, h))
    c1 = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    c2 = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    d = ImageDraw.Draw(img)
    for x in range(w):
        t = x / w
        d.line([(x, 0), (x, h)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    for _ in range(7):
        r = rnd.randint(60, 260)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255), width=3)
    font = _font(54)
    if font is None:  # built-in bitmap font lacks some characters
        title = title.replace("–", "-").replace("’", "'")
        try:
            font = ImageFont.load_default(size=54)
        except TypeError:
            font = ImageFont.load_default()
    words, lines, line = title.split(), [], ""
    for word in words:
        if len(line + " " + word) > 28:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    lines.append(line)
    for i, ln in enumerate(lines[:3]):
        d.text((w / 2, h / 2 + (i - (len(lines[:3]) - 1) / 2) * 66), ln, fill="white", anchor="mm", font=font)
    out = BytesIO()
    img.save(out, "WEBP", quality=80)
    return ContentFile(out.getvalue(), name="cover.webp")


class Command(BaseCommand):
    help = "Create or update demo articles, blog posts and tags."

    def add_arguments(self, parser):
        parser.add_argument("--covers", action="store_true", help="Regenerate cover images")

    def handle(self, *args, covers=False, **opts):
        now = timezone.now()
        rnd = random.Random(7)
        for kind, items in (("article", ARTICLES), ("blog", BLOGS)):
            for n, (slug, title, summary, body) in enumerate(items):
                topic, _ = Topic.objects.update_or_create(
                    kind=kind, slug=slug,
                    defaults={"title": title, "summary": summary, "body": body, "is_published": True,
                              "published_at": now - timedelta(days=n * 9 + rnd.randint(0, 5))},
                )
                if covers and topic.image:
                    topic.image.delete(save=False)
                if covers or not topic.image:
                    topic.image.save(f"{slug}.webp", cover(title, hash(slug) % 1000), save=True)
        # Drop the old one-line placeholders from the first demo.
        Topic.objects.filter(kind="article", slug="first-visit-guide").delete()
        Topic.objects.filter(kind="blog", slug="welcome").delete()

        tags = [Tag.objects.get_or_create(name=name)[0] for name in TAGS]
        for tag in Tag.objects.filter(color=""):
            tag.save()  # assigns a colour
        for ad in Ad.objects.all():
            ad.tags.add(*rnd.sample(tags, k=4))

        self.stdout.write(self.style.SUCCESS(
            f"{Topic.objects.filter(kind='article').count()} articles, "
            f"{Topic.objects.filter(kind='blog').count()} blog posts, {Tag.objects.count()} tags."
        ))
