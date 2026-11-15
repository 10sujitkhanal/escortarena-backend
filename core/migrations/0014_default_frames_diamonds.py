from django.db import migrations

FRAMES = [  # code, name, description, diamonds, days, from, to
    ("simple", "Simple", "A border in the site colour. Free for everyone.", 0, 3650, None, None),
    ("lace", "Lace", "An elegant white lace border.", 60, 365, None, None),
    ("polaroid", "Polaroid", "A classic instant-photo frame.", 60, 365, None, None),
    ("gold", "Gold", "A gold border with a light sweep.", 120, 365, None, None),
    ("glitter", "Glitter", "Twinkling stars around the edges.", 150, 365, None, None),
    ("gems", "Gems", "Sparkling stones in the corners.", 200, 365, None, None),
    ("winter", "Winter", "Snow on the top edge – for the cold months.", 80, 120, None, None),
]
PACKAGES = [(100, 49), (250, 99), (600, 199), (1500, 449)]  # diamonds, kr


def create(apps, schema_editor):
    Frame = apps.get_model("core", "Frame")
    Product = apps.get_model("core", "Product")
    for order, (code, name, desc, price, days, a, b) in enumerate(FRAMES):
        Frame.objects.update_or_create(code=code, defaults={
            "name": name, "description": desc, "price_diamonds": price, "days": days,
            "available_from": a, "available_to": b, "sort_order": order})
    for order, (diamonds, kr) in enumerate(PACKAGES):
        Product.objects.update_or_create(code=f"diamonds-{diamonds}", defaults={
            "kind": "diamonds", "diamonds": diamonds, "name": f"{diamonds} diamonds", "tier": "basic", "days": 30,
            "price": kr * 100, "currency": "DKK", "is_active": True, "sort_order": 100 + order})


def remove(apps, schema_editor):
    apps.get_model("core", "Frame").objects.all().delete()
    apps.get_model("core", "Product").objects.filter(kind="diamonds").delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0013_ad_frame_until")]
    operations = [migrations.RunPython(create, remove)]
