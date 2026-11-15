from django.db import migrations


def reset(apps, schema_editor):
    """'Verified' must now come from an approved ID check, not a manual tick."""
    User = apps.get_model("core", "User")
    IdVerification = apps.get_model("core", "IdVerification")
    approved = IdVerification.objects.filter(status="approved").values("user_id")
    User.objects.filter(age_verified=True).exclude(pk__in=approved).update(age_verified=False)


class Migration(migrations.Migration):
    dependencies = [("core", "0017_id_verification")]
    operations = [migrations.RunPython(reset, migrations.RunPython.noop)]
