"""The admin: a dashboard as start page and a short sidebar grouped by task.

Every model stays registered (links and old bookmarks keep working); the sidebar and the start page only list the
ones in SECTIONS. Enabled in settings.INSTALLED_APPS via core.admin_site.VeloraAdminConfig.
"""

from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig
from django.urls import reverse

# (section, [model class names in the order shown]). Anything not listed is hidden from the menu.
SECTIONS = [
    ("Moderation", ["IdVerification", "Ad", "Report", "UserReport", "Comment", "AdReview", "ContactMessage"]),
    ("People", ["User", "LoginEvent", "LegacyAccount"]),
    ("Sales", ["Order", "Plan", "Product", "AddOnGroup", "Offer", "DiamondTransaction"]),
    ("Content", ["Topic", "FaqItem", "Tag"]),
    ("Site setup", ["SiteSettings", "Country", "AdRegion", "AdPlace", "AdCategory", "ChoiceOption"]),
]


class VeloraAdminSite(AdminSite):
    site_header = "EscortArea admin"
    site_title = "EscortArea admin"
    index_title = "Dashboard"
    index_template = "admin/dashboard.html"

    def index(self, request, extra_context=None):
        from . import dashboard
        return super().index(request, {**dashboard.build(), **(extra_context or {})})

    def get_app_list(self, request, app_label=None):
        apps = super().get_app_list(request, app_label)
        if app_label:  # an app's own page (/admin/core/): keep Django's list
            return apps
        models = {m["object_name"]: m for app in apps for m in app["models"]}
        counts = self._badges()
        out = []
        for title, names in SECTIONS:
            items = []
            for name in names:
                if name in models:
                    m = dict(models[name])
                    if counts.get(name):
                        m["name"] = f"{m['name']} ({counts[name]})"
                    items.append(m)
            if items:
                out.append({"name": title, "app_label": title.lower().replace(" ", "-"), "app_url": reverse("admin:index"),
                            "has_module_perms": True, "models": items})
        return out

    @staticmethod
    def _badges() -> dict:
        """Waiting items shown next to the menu entry, e.g. 'ID verifications (3)'."""
        from .models import Ad, ContactMessage, IdVerification, Report, SiteSettings, UserReport
        try:
            out = {
                "IdVerification": IdVerification.objects.filter(status="pending").count(),
                "Report": Report.objects.filter(status="open").count(),
                "UserReport": UserReport.objects.filter(status="open").count(),
                "ContactMessage": ContactMessage.objects.filter(handled=False).count(),
            }
            if SiteSettings.load().review_ads:
                out["Ad"] = Ad.objects.filter(status="pending").count()
            return out
        except Exception:  # noqa: BLE001 – e.g. before the first migrate
            return {}


class VeloraAdminConfig(AdminConfig):
    default_site = "core.admin_site.VeloraAdminSite"
