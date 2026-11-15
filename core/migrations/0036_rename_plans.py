"""Rename the paid plans: Bronze → Standard, Gold → Premium, Diamond → VIP (values, codes and texts)."""

import re

from django.db import migrations, models

FORWARD = {"bronze": ("standard", "Standard"), "gold": ("premium", "Premium"), "diamond": ("vip", "VIP")}
BACKWARD = {new: (old, old.title()) for old, (new, _) in FORWARD.items()}
TEXT_WORDS = {
    "bronze": "standard", "Bronze": "Standard", "Silver": "Standard",
    "gold": "premium", "Gold": "Premium",
    "diamond": "VIP", "Diamond": "VIP",
}


def _words(mapping):
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, mapping)) + r")\b")
    return lambda text: pattern.sub(lambda m: mapping[m.group(1)], text or "")


def _run(apps, mapping, text):
    Ad, Plan, Product, Order, Offer = (apps.get_model("core", m) for m in ("Ad", "Plan", "Product", "Order", "Offer"))
    for old, (new, _) in mapping.items():
        Ad.objects.filter(tier=old).update(tier=new)
        Ad.objects.filter(pending_tier=old).update(pending_tier=new)
        Order.objects.filter(tier=old).update(tier=new)
        Offer.objects.filter(tier=old).update(tier=new)
    for plan in Plan.objects.all():
        if plan.tier in mapping:
            plan.tier, plan.name = mapping[plan.tier]
        plan.description, plan.features = text(plan.description), text(plan.features)
        plan.save()
    for p in Product.objects.all():
        if p.tier in mapping:
            old = p.tier
            p.tier = mapping[old][0]
            if p.code.startswith(f"{old}-"):
                p.code = p.tier + p.code[len(old):]
        p.name, p.description = text(p.name), text(p.description)
        p.save()


def forward(apps, schema_editor):
    _run(apps, FORWARD, _words(TEXT_WORDS))


def backward(apps, schema_editor):
    _run(apps, BACKWARD, _words({"Standard": "Bronze", "standard": "bronze", "Premium": "Gold", "premium": "gold", "VIP": "Diamond"}))


TIER_CHOICES = [("free", "Free"), ("standard", "Standard"), ("premium", "Premium"), ("vip", "VIP")]


class Migration(migrations.Migration):

    dependencies = [("core", "0035_all_phone_countries")]

    operations = [
        migrations.AlterField("ad", "tier", models.CharField(choices=TIER_CHOICES, default="free", max_length=10)),
        migrations.AlterField("plan", "tier", models.CharField(choices=TIER_CHOICES, max_length=10, unique=True)),
        migrations.AlterField("product", "tier", models.CharField(choices=TIER_CHOICES, default="free", max_length=10)),
        migrations.AlterField("order", "tier", models.CharField(choices=TIER_CHOICES, max_length=10)),
        migrations.AlterField("offer", "tier", models.CharField(blank=True, choices=TIER_CHOICES, help_text="Only this plan (optional)", max_length=10)),
        migrations.RunPython(forward, backward),
    ]
