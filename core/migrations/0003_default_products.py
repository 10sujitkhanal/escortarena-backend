from django.db import migrations

# Placeholder prices in øre (DKK x 100), incl. VAT. Change them in the admin.
PRODUCTS = [
    ("renew-30", "Extend 30 days", "Keep your ad live for 30 more days.", "basic", 30, 9900, 0),
    ("gold-30", "Gold – 30 days", "Gold frame and higher placement in lists.", "gold", 30, 19900, 1),
    ("platinum-7", "Platinum – 7 days", "Top placement and front-page spot for a week.", "platinum", 7, 12900, 2),
    ("platinum-30", "Platinum – 30 days", "Top placement and front-page spot for a month.", "platinum", 30, 39900, 3),
]


def create(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    for code, name, desc, tier, days, price, order in PRODUCTS:
        Product.objects.update_or_create(
            code=code,
            defaults={"name": name, "description": desc, "tier": tier, "days": days,
                      "price": price, "currency": "DKK", "sort_order": order},
        )


def remove(apps, schema_editor):
    apps.get_model("core", "Product").objects.filter(code__in=[p[0] for p in PRODUCTS]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0002_payments")]
    operations = [migrations.RunPython(create, remove)]
