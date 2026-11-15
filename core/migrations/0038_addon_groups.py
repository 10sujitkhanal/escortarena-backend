"""Add-ons become groups ("First in search", "Front page ad", …) with one row per period and price.

Orders keep their own copy of what was bought, so the old single add-on rows can be replaced.
"""

import django.db.models.deletion
from django.db import migrations, models

EFFECTS = [
    ("top", "First in search (top of the lists + coloured card)"),
    ("slider", "Front page ad (front-page slider + coloured card)"),
    ("highlight", "Coloured card only"),
    ("bump", "Bump to the top (one time)"),
]

GROUPS = [
    # name, badge, description, effect, sort, [(days, price in cents)]
    ("First in search", "Featured", 'Shown at the top of the search results with the "Featured" mark and a coloured card.',
     "top", 1, [(7, 4900), (14, 9800), (30, 17900), (60, 34900)]),
    ("Front page ad", "Featured", 'Shown at the top of the front page with the "Featured" mark and a coloured card.',
     "slider", 2, [(7, 7000), (14, 14000), (30, 27900), (60, 54900)]),
    ("Coloured card", "", "A coloured glow around the card so it stands out in every list.",
     "highlight", 3, [(7, 2900), (14, 4900), (30, 7900), (60, 13900)]),
    ("Bump to the top", "", "Moves the ad back to the top of the lists right away.", "bump", 4, [(0, 2900)]),
]


def clear(apps, schema_editor):
    apps.get_model("core", "AddOn").objects.all().delete()


def seed(apps, schema_editor):
    Group, AddOn, Site = (apps.get_model("core", m) for m in ("AddOnGroup", "AddOn", "SiteSettings"))
    site = Site.objects.filter(pk=1).first()
    currency = site.currency if site else "USD"
    for name, badge, desc, effect, sort, options in GROUPS:
        group = Group.objects.create(name=name, badge=badge, description=desc, effect=effect, sort_order=sort)
        for days, price in options:
            AddOn.objects.create(group=group, code=f"{effect}-{days}d", days=days, price=price, currency=currency, effect=effect,
                                 name=name if days == 0 else f"{name} – {days} days")


class Migration(migrations.Migration):

    dependencies = [("core", "0037_addons")]

    operations = [
        migrations.RunPython(clear, migrations.RunPython.noop),
        migrations.CreateModel(
            name="AddOnGroup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=60)),
                ("badge", models.CharField(blank=True, default="Featured", help_text="Small label next to the name (optional)", max_length=20)),
                ("description", models.CharField(blank=True, max_length=200)),
                ("effect", models.CharField(choices=EFFECTS, max_length=12)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.PositiveSmallIntegerField(default=0)),
            ],
            options={"verbose_name": "add-on", "ordering": ["sort_order", "id"]},
        ),
        migrations.RemoveField("addon", "description"),
        migrations.RemoveField("addon", "icon"),
        migrations.RemoveField("addon", "sort_order"),
        migrations.AddField("addon", "group", models.ForeignKey(
            default=None, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="options", to="core.addongroup")),
        migrations.AlterField("addon", "group", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="options", to="core.addongroup")),
        migrations.AlterField("addon", "code", models.SlugField(blank=True, max_length=40, unique=True)),
        migrations.AlterField("addon", "days", models.PositiveSmallIntegerField(default=7, help_text="How long it lasts. Bump: one time (0).")),
        migrations.AlterField("addon", "name", models.CharField(editable=False, max_length=80)),
        migrations.AlterField("addon", "effect", models.CharField(choices=EFFECTS, editable=False, max_length=12)),
        migrations.AlterField("addon", "currency", models.CharField(default="USD", editable=False, max_length=3)),
        migrations.AlterModelOptions("addon", {"ordering": ["group__sort_order", "days", "price"],
                                               "verbose_name": "period and price", "verbose_name_plural": "periods and prices"}),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
