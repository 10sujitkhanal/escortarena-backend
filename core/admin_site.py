"""The admin: a dashboard as start page, a sales overview, a notification bell and a short sidebar with icons.

Every model stays registered (links and old bookmarks keep working); the sidebar only lists the ones in SECTIONS.
Enabled in settings.INSTALLED_APPS via core.admin_site.VeloraAdminConfig. Look and feel: templates/admin/base_site.html
and templates/admin/nav_sidebar.html.
"""

from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.http import url_has_allowed_host_and_scheme

# (section, icon, [(model class name, icon)]). Anything not listed is hidden from the menu.
SECTIONS = [
    ("Moderation", "shield", [("IdVerification", "id"), ("Ad", "ad"), ("Report", "flag"), ("UserReport", "flag"),
                              ("Comment", "comment"), ("AdReview", "star"), ("ContactMessage", "mail")]),
    ("People", "users", [("User", "user"), ("LoginEvent", "login"), ("LegacyAccount", "move")]),
    ("Chat", "chat", [("Conversation", "chat")]),
    ("Sales", "money", [("Order", "receipt"), ("Plan", "layers"), ("Product", "tag"), ("AddOnGroup", "rocket"),
                        ("Offer", "gift"), ("DiamondTransaction", "coin")]),
    ("Content", "book", [("Topic", "book"), ("FaqItem", "help"), ("Tag", "tag")]),
    ("Site setup", "gear", [("SiteSettings", "gear"), ("Country", "globe"), ("AdRegion", "map"), ("AdPlace", "pin"),
                            ("AdCategory", "grid"), ("ChoiceOption", "list")]),
]


class VeloraAdminSite(AdminSite):
    site_header = "EscortArea"
    site_title = "EscortArea admin"
    index_title = "Dashboard"
    index_template = "admin/dashboard.html"

    def get_urls(self):
        return [
            path("sales/", self.admin_view(self.sales_view), name="sales"),
            path("notifications/seen/", self.admin_view(self.notifications_seen), name="notifications_seen"),
            *super().get_urls(),
        ]

    def each_context(self, request):
        ctx = super().each_context(request)
        if request.user.is_active and request.user.is_staff:
            from . import admin_feed
            ctx.update(admin_feed.for_header(request))
            ctx["nav_sections"] = self._nav(request, ctx.get("available_apps") or [])
        return ctx

    def index(self, request, extra_context=None):
        from . import admin_feed, dashboard
        data = dashboard.build()
        seen = admin_feed.seen_at(request)
        feed = admin_feed.events(limit=25)
        for e in feed:
            e["new"] = seen is None or e["at"] > seen
        data.update(feed=feed, chat=admin_feed.chat_stats(), sales_totals=dashboard.sales_overview()["totals"])
        return super().index(request, {**data, **(extra_context or {})})

    def sales_view(self, request):
        from . import dashboard
        ctx = {**self.each_context(request), **dashboard.sales_overview(), "title": "Sales overview"}
        return TemplateResponse(request, "admin/sales_overview.html", ctx)

    def notifications_seen(self, request):
        """POST: mark every notification as read, then go back."""
        from . import admin_feed
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index"))
        admin_feed.mark_seen(request)
        back = request.POST.get("next") or reverse("admin:index")
        if not url_has_allowed_host_and_scheme(back, {request.get_host()}, require_https=request.is_secure()):
            back = reverse("admin:index")
        return HttpResponseRedirect(back)

    def get_app_list(self, request, app_label=None):
        apps = super().get_app_list(request, app_label)
        if app_label:  # an app's own page (/admin/core/): keep Django's list
            return apps
        models = {m["object_name"]: m for app in apps for m in app["models"]}
        counts = self._badges()
        out = []
        for title, _icon, names in SECTIONS:
            items = []
            for name, _ in names:
                if name in models:
                    m = dict(models[name])
                    if counts.get(name):
                        m["name"] = f"{m['name']} ({counts[name]})"
                    items.append(m)
            if items:
                out.append({"name": title, "app_label": title.lower().replace(" ", "-"), "app_url": reverse("admin:index"),
                            "has_module_perms": True, "models": items})
        return out

    def _nav(self, request, available_apps) -> list[dict]:
        """Sidebar sections with icons and 'waiting' badges (templates/admin/nav_sidebar.html)."""
        models = {m["object_name"]: m for app in available_apps for m in app["models"]}
        counts = self._badges()
        here = request.path
        home, sales = reverse("admin:index"), reverse("admin:sales")
        sections = [{"title": "", "items": [
            {"name": "Dashboard", "url": home, "icon": "home", "active": here == home},
            {"name": "Sales overview", "url": sales, "icon": "chart", "active": here == sales},
        ]}]
        for title, icon, names in SECTIONS:
            items = []
            for name, item_icon in names:
                m = models.get(name)
                if not m or not m.get("admin_url"):
                    continue
                items.append({"name": m["name"].split(" (")[0], "url": m["admin_url"], "icon": item_icon,
                              "badge": counts.get(name, 0), "active": here.startswith(m["admin_url"])})
            if items:
                sections.append({"title": title, "icon": icon, "items": items})
        return sections

    @staticmethod
    def _badges() -> dict:
        """Waiting items shown next to the menu entry, e.g. 'ID verifications 3'."""
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
