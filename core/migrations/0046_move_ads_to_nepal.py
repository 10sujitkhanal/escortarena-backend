"""Move every ad from the Danish areas to Nepal and make Nepal the site's country."""
import random

from django.db import migrations

# New Nepal areas: slug, name, map centre.
NEPAL = [
    ("kathmandu", "Kathmandu", 27.7172, 85.3240),
    ("lalitpur", "Lalitpur", 27.6644, 85.3188),
    ("bhaktapur", "Bhaktapur", 27.6710, 85.4298),
    ("pokhara", "Pokhara", 28.2096, 83.9856),
    ("chitwan", "Chitwan", 27.6766, 84.4322),
    ("biratnagar", "Biratnagar", 26.4525, 87.2718),
    ("butwal", "Butwal", 27.7006, 83.4484),
    ("dharan", "Dharan", 26.8065, 87.2846),
    ("nepalgunj", "Nepalgunj", 28.0500, 81.6167),
    ("janakpur", "Janakpur", 26.7288, 85.9263),
]
# Danish area -> Nepal area.
MOVE = {
    "copenhagen": "kathmandu", "north-zealand": "lalitpur", "rest-of-zealand": "bhaktapur", "aarhus": "pokhara",
    "lolland": "chitwan", "north-jutland": "biratnagar", "mid-west-jutland": "butwal", "funen": "dharan",
    "south-jutland": "nepalgunj", "bornholm": "janakpur",
}
# Neighbourhoods / towns used as the ad's city in each area.
PLACES = {
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


def forward(apps, schema_editor):
    Country = apps.get_model("core", "Country")
    AdRegion = apps.get_model("core", "AdRegion")
    Ad = apps.get_model("core", "Ad")
    SiteSettings = apps.get_model("core", "SiteSettings")

    nepal = Country.objects.filter(code="NP").first()
    if nepal is None:
        nepal = Country.objects.create(code="NP", name="Nepal", dial_code="+977", flag="🇳🇵", continent="AS")
    nepal.has_ads = True
    nepal.is_active = True
    nepal.sort_order = 0
    nepal.save()
    for i, (slug, name, lat, lng) in enumerate(NEPAL):
        AdRegion.objects.update_or_create(
            slug=slug, defaults={"country": nepal, "name": name, "lat": lat, "lng": lng, "is_active": True, "sort_order": i},
        )
    Country.objects.filter(code="DK").update(has_ads=False)

    rnd = random.Random(977)
    for ad in Ad.objects.all():
        regions = [MOVE.get(r, r) for r in (ad.regions or [])]
        region = MOVE.get(ad.region, ad.region)
        if region not in PLACES:  # unknown/empty area → Kathmandu
            region = "kathmandu"
        regions = list(dict.fromkeys([region] + [r for r in regions if r in PLACES]))
        ad.region, ad.regions = region, regions
        ad.city = rnd.choice(PLACES[region])
        ad.save(update_fields=["region", "regions", "city"])

    SiteSettings.objects.update(country="Nepal")


class Migration(migrations.Migration):
    dependencies = [("core", "0045_region_map_position")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
