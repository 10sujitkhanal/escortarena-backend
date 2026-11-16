"""Header menu: show "Trans" next to Women and Men (Admin → Options → Gender → show in menu)."""

from django.db import migrations


def show_trans(apps, schema_editor):
    Option = apps.get_model("core", "ChoiceOption")
    Option.objects.filter(group="gender", value="trans").update(show_in_menu=True, menu_label="Trans", is_active=True)


def hide_trans(apps, schema_editor):
    Option = apps.get_model("core", "ChoiceOption")
    Option.objects.filter(group="gender", value="trans").update(show_in_menu=False)


class Migration(migrations.Migration):
    dependencies = [("core", "0061_support_email_help")]
    operations = [migrations.RunPython(show_trans, hide_trans)]
