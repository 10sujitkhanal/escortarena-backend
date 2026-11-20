"""The admin: a dashboard as start page, a sales overview, a notification bell and a short sidebar with icons.

Every model stays registered (links and old bookmarks keep working); the sidebar only lists the ones in SECTIONS.
Enabled in settings.INSTALLED_APPS via core.admin_site.VeloraAdminConfig. Look and feel: templates/admin/base_site.html
and templates/admin/nav_sidebar.html.
"""

from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig
from django.http import HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.http import url_has_allowed_host_and_scheme

# (section, icon, [(model class name, icon)]). Anything not listed is hidden from the menu.
SECTIONS = [
    ("Moderation", "shield", [("IdVerification", "id"), ("Ad", "ad"), ("Report", "flag"), ("UserReport", "flag"),
                              ("Comment", "comment"), ("AdReview", "star"), ("ContactMessage", "mail")]),
    ("People", "users", [("User", "user"), ("LoginEvent", "login"), ("LegacyAccount", "move")]),
    ("Chat", "chat", [("Conversation", "chat")]),
    # "@sales" = Sales dashboard (charts, any period); "@plans" = Plans page (one tab per price list). Not models.
    ("Sales", "money", [("@sales", "chart"), ("Order", "receipt"), ("Offer", "gift"), ("@plans", "layers"),
                        ("DiamondTransaction", "coin")]),
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
            path("sales-dashboard/", self.admin_view(self.sales_dashboard_view), name="sales_dashboard"),
            path("sales-dashboard/export.csv", self.admin_view(self.sales_export), name="sales_export"),
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

    PLAN_TABS = [("ads", "Ads plan", "layers"), ("vip", "VIP plan", "gift"), ("coins", "Coin packages", "coin"),
                 ("addons", "Add-ons", "rocket")]

    def sales_view(self, request):
        """Plans: one tab per price list – ad plans, visitor VIP, coin packages, add-ons (First in search, …)."""
        from . import dashboard
        data = dashboard.sales_overview()
        tab = request.GET.get("tab", "ads")
        if tab not in {key for key, _, _ in self.PLAN_TABS}:
            tab = "ads"
        sizes = {"ads": sum(len(p["prices"]) for p in data["plans"]), "vip": len(data["memberships"]),
                 "coins": len(data["coins"]), "addons": sum(len(g["options"]) for g in data["addons"])}
        tabs = [{"key": key, "label": label, "icon": icon, "count": sizes[key], "active": key == tab,
                 "url": f"?tab={key}"} for key, label, icon in self.PLAN_TABS]
        ctx = {**self.each_context(request), **data, "title": "Plans", "tab": tab, "plan_tabs": tabs}
        return TemplateResponse(request, "admin/sales_overview.html", ctx)

    def sales_dashboard_view(self, request):
        from . import sales_dashboard
        ctx = {**self.each_context(request), **sales_dashboard.build(request.GET), "title": "Sales dashboard"}
        return TemplateResponse(request, "admin/sales_dashboard.html", ctx)

    def sales_export(self, request):
        from . import sales_dashboard
        return sales_dashboard.export_csv(request.GET)

    def notifications_seen(self, request):
        """POST: mark every notification as read, then go back."""
        from . import admin_feed
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index"))
        admin_feed.mark_seen(request)
        if request.headers.get("X-Requested-With") == "fetch":  # the bell, opened in the header
            return HttpResponse(status=204)
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
        home, plans = reverse("admin:index"), reverse("admin:sales")
        # Price lists edited from the Plans page count as "Plans" in the menu too.
        plan_pages = [plans] + [reverse(f"admin:core_{n}_changelist") for n in ("plan", "product", "addongroup")]
        sections = [{"title": "", "items": [{"name": "Dashboard", "url": home, "icon": "home", "active": here == home}]}]
        for title, icon, names in SECTIONS:
            items = []
            for name, item_icon in names:
                if name == "@sales":
                    url = reverse("admin:sales_dashboard")
                    items.append({"name": "Sales dashboard", "url": url, "icon": item_icon, "badge": 0,
                                  "active": here.startswith(url)})
                    continue
                if name == "@plans":
                    items.append({"name": "Plans", "url": plans, "icon": item_icon, "badge": 0,
                                  "active": any(here.startswith(u) for u in plan_pages)})
                    continue
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
