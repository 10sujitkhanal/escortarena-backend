"""Original starter glossary for the Guide → Dictionary. Safe to re-run (updates by slug).

Run: uv run python manage.py seed_dictionary
"""

from django.core.management.base import BaseCommand

from core.models import Topic

# (slug, title, aliases, summary, body)
ENTRIES = [
    ("24-7-availability", "24/7", "Around the clock", "Available at any time of day.",
     "Used in ads where the advertiser takes bookings day and night. Always confirm the time directly before you travel."),
    ("aromatherapy", "Aromatherapy", "Essential oils", "Massage with scented plant oils.",
     "A massage where essential oils such as lavender or eucalyptus are added to the massage oil to help you relax. Mention any allergies before the session starts."),
    ("body-to-body", "Body-to-body massage", "B2B, Body massage", "Massage given using the whole body.",
     "A massage style where the masseuse uses more than the hands. What is included varies between advertisers, so read the ad and ask before booking."),
    ("boundaries", "Boundaries", "Limits", "What each person is and isn't comfortable with.",
     "Both the client and the advertiser have boundaries. Respect them, agree on them before you meet, and remember that anyone can change their mind at any time."),
    ("consent", "Consent", "Agreement", "A clear, freely given yes.",
     "Consent must be clear, voluntary and can be withdrawn at any time. If something is unclear, stop and ask. Nobody may ever be pressured."),
    ("couples-massage", "Couples massage", "Massage for two, Duo massage", "Two people receiving a massage together.",
     "A session for two people at the same time, either side by side or one after the other. Ads that welcome couples are marked **Welcomes: Couples**."),
    ("deep-tissue", "Deep tissue massage", "Sports massage", "Firm pressure for tense muscles.",
     "A firm massage that works on deeper muscle layers. Good for tension and stiffness – tell the masseuse if the pressure is too hard."),
    ("discretion", "Discretion", "Discreet", "Your privacy is respected.",
     "Discreet advertisers don't share information about their clients, use neutral addresses and keep messages private."),
    ("escort", "Escort", "Companion, Companionship", "A person who offers paid company.",
     "An escort offers their time and company, for example for dinner, events or private meetings. Everything beyond that is agreed between adults."),
    ("gfe", "GFE", "Girlfriend experience", "A relaxed, personal, date-like meeting.",
     "Short for *girlfriend experience*: a meeting with a warm, personal and relaxed atmosphere rather than a purely practical one."),
    ("hot-stone", "Hot stone massage", "Hot stones", "Massage with warm, smooth stones.",
     "Heated stones are placed on the body and used to massage. The warmth relaxes the muscles. Not recommended if you are pregnant or have circulation problems."),
    ("incall", "Incall", "In-call", "You visit the advertiser.",
     "The meeting takes place at the advertiser's location – their clinic, studio or apartment. The address is usually shared after booking."),
    ("lingam-massage", "Lingam massage", "Lingam", "A tantric massage style for men.",
     "A slow, mindful massage from the tantric tradition focused on relaxation and energy. Always agree on content and boundaries in advance."),
    ("massage", "Massage", "Classic massage", "Hands-on treatment of muscles and skin.",
     "The umbrella term for treatments that knead, stroke and press the body to relax muscles and mind. See also Swedish, Thai and deep tissue massage."),
    ("nuru", "Nuru massage", "Nuru", "Body massage with a special slippery gel.",
     "A Japanese style of body-to-body massage using an odourless gel made from seaweed. Ask the advertiser what is included."),
    ("outcall", "Outcall", "Out-call, Home visit, Hotel visit", "The advertiser comes to you.",
     "The advertiser travels to your home or hotel. Travel costs are often added to the price – check the ad."),
    ("overnight", "Overnight", "Overnight stay", "A booking that lasts the whole night.",
     "A long booking that usually includes dinner, conversation and sleep. Agree on the time, price and expectations in advance."),
    ("prepayment-scam", "Prepayment scam", "Gift card scam, Advance payment", "A fraud where you are asked to pay in advance.",
     "Never pay in advance with gift cards, vouchers or transfers to strangers. Real advertisers on this site are verified. Use **Report** on the ad if someone asks for prepayment."),
    ("safe-word", "Safe word", "Safeword", "An agreed word that stops everything immediately.",
     "A word agreed in advance that means *stop now*, no questions asked. Used especially in role play and BDSM."),
    ("bdsm", "BDSM", "Bondage, Domination, Kink", "Consensual play with power and control.",
     "An umbrella term for consensual activities involving restraint, dominance and submission. Requires clear agreements, a safe word and mutual respect."),
    ("swedish-massage", "Swedish massage", "Classic Swedish", "Long, flowing strokes to relax the body.",
     "The most common relaxation massage, using long strokes, kneading and gentle tapping. A good choice for your first massage."),
    ("tantric-massage", "Tantric massage", "Tantra, Tantra massage", "Slow, mindful whole-body massage.",
     "Rooted in tantric philosophy, this massage focuses on breathing, presence and energy through slow, gentle touch."),
    ("thai-massage", "Thai massage", "Thai", "Stretching and pressure, usually fully clothed.",
     "A traditional massage with stretching and pressure points, often done on a mat. You usually wear loose, comfortable clothes."),
    ("verified-advertiser", "Verified advertiser", "Verified, Age-verified", "The advertiser's age and identity are checked.",
     "Every advertiser on this site must prove they are at least 18 before their ad goes live. Look for the ✔ mark on the ad."),
    ("webcam", "Webcam", "Cam, Camshow, Video call", "A meeting over live video.",
     "An online session over video. Never send money to people who contact you outside the site, and never share personal details."),
    ("yoni-massage", "Yoni massage", "Yoni", "A tantric massage style for women.",
     "A gentle, respectful massage from the tantric tradition with focus on relaxation and trust. Content and boundaries are always agreed first."),
]


class Command(BaseCommand):
    help = "Create or update the starter dictionary entries."

    def handle(self, *args, **opts):
        for order, (slug, title, aliases, summary, body) in enumerate(ENTRIES):
            Topic.objects.update_or_create(
                kind=Topic.Kind.DICTIONARY,
                slug=slug,
                defaults={"title": title, "aliases": aliases, "summary": summary, "body": body,
                          "is_published": True, "menu_order": order},
            )
        self.stdout.write(self.style.SUCCESS(f"Dictionary has {Topic.objects.filter(kind='dictionary').count()} entries."))
