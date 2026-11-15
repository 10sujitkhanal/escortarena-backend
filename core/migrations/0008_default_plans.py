from django.db import migrations

DAYS = [1, 7, 30, 90, 180, 365]
DAY_LABEL = {1: "1 day", 7: "1 week", 30: "1 month", 90: "3 months", 180: "6 months", 365: "1 year"}

# Placeholder prices in DKK (incl. VAT). Change them in Admin → Plans.
PLANS = [
    ("basic", "Basic", "Get your ad online.", False,
     "Ad visible in all lists and searches\nUp to 12 pictures\nHearts and statistics",
     [19, 79, 199, 499, 899, 1499]),
    ("gold", "Gold", "Stand out with a gold frame and higher placement.", True,
     "Everything in Basic\nGold frame and Featured badge\nShown above Basic ads\nBumped to the top when you buy",
     [39, 149, 399, 999, 1799, 2999]),
    ("platinum", "Platinum", "Maximum visibility on the front page.", False,
     "Everything in Gold\nFront-page slider\nTop of every list\nHighlighted card",
     [69, 249, 699, 1799, 3199, 5499]),
]

OLD_CODES = ["renew-30", "gold-30", "platinum-7", "platinum-30"]


def create(apps, schema_editor):
    Plan = apps.get_model("core", "Plan")
    Product = apps.get_model("core", "Product")
    for order, (tier, name, desc, highlight, features, prices) in enumerate(PLANS):
        plan, _ = Plan.objects.update_or_create(
            tier=tier,
            defaults={"name": name, "description": desc, "features": features,
                      "is_highlighted": highlight, "sort_order": order},
        )
        for days, kr in zip(DAYS, prices):
            Product.objects.update_or_create(
                code=f"{tier}-{days}d",
                defaults={"plan": plan, "name": f"{name} – {DAY_LABEL[days]}", "tier": tier, "days": days,
                          "price": kr * 100, "currency": "DKK", "is_active": True, "sort_order": days,
                          "description": desc},
            )
    # Old one-off products stay for order history but can no longer be bought.
    Product.objects.filter(code__in=OLD_CODES).update(is_active=False)


def remove(apps, schema_editor):
    apps.get_model("core", "Product").objects.filter(code__in=OLD_CODES).update(is_active=True)
    apps.get_model("core", "Plan").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0007_site_settings_plans")]
    operations = [migrations.RunPython(create, remove)]
