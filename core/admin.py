from django import forms
from django.db import models
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.shortcuts import redirect
from django.utils.html import format_html

from . import payments
from .models import (
    Ad,
    AdCategory,
    AddOn,
    AddOnGroup,
    AdPicture,
    AdPlace,
    AdRegion,
    AdReview,
    AdVideo,
    ChoiceOption,
    Comment,
    ContactMessage,
    ContactReply,
    Conversation,
    Country,
    DiamondTransaction,
    FaqItem,
    Heart,
    IdVerification,
    LegacyAccount,
    LoginEvent,
    Message,
    Offer,
    Order,
    Plan,
    Product,
    Report,
    SiteSettings,
    Tag,
    Topic,
    User,
    UserReport,
)


# ---------------------------------------------------------------------------
# Photos in the lists: a small picture per row (click = full size). One subquery per list, not one query per row.
# ---------------------------------------------------------------------------

def _media_url(name: str) -> str:
    from django.core.files.storage import default_storage
    return default_storage.url(name) if name else ""


def thumb(name: str, *, round_: bool = False, size: int = 46, fallback: str = "", badge: str = ""):
    """<img> for a stored file name (or a grey placeholder with `fallback` text)."""
    radius = "50%" if round_ else "10px"
    if not name:
        return format_html(
            '<span style="display:inline-grid;place-items:center;width:{0}px;height:{0}px;border-radius:{1};'
            'background:var(--darkened-bg);color:var(--body-quiet-color);font-weight:800;font-size:.8rem">{2}</span>',
            size, radius, (fallback or "–")[:2].upper())
    url = _media_url(name)
    extra = format_html('<span style="position:absolute;right:-4px;bottom:-4px;background:#e94fc2;color:#fff;font-size:.65rem;'
                        'font-weight:800;border-radius:999px;padding:1px 5px">{}</span>', badge) if badge else ""
    return format_html(
        '<a href="{0}" target="_blank" rel="noopener" style="position:relative;display:inline-block" title="Open full size">'
        '<img src="{0}" alt="" loading="lazy" style="width:{1}px;height:{1}px;object-fit:cover;border-radius:{2};display:block">{3}</a>',
        url, size, radius, extra)


def _ad_cover(ref: str):
    from django.db.models import OuterRef, Subquery
    return Subquery(AdPicture.objects.filter(ad=OuterRef(ref)).order_by("position", "id").values("image")[:1])


def _user_photo(ref: str):
    """Profile picture, else the photo of the person's newest ad."""
    from django.db.models import OuterRef, Subquery
    from django.db.models.functions import Coalesce, NullIf
    text = models.CharField()
    profile = Subquery(User.objects.filter(pk=OuterRef(ref)).values("profile_picture")[:1], output_field=text)
    ad_photo = Subquery(AdPicture.objects.filter(ad__owner=OuterRef(ref)).order_by("-ad__created_at", "position", "id")
                        .values("image")[:1], output_field=text)
    return Coalesce(NullIf(profile, models.Value("")), ad_photo, output_field=text)


class PhotoColumnMixin:
    """Adds a "Photo" column. Set photo_ad (path to the ad, "pk" for ads) or photo_user (path to the user)."""

    photo_ad: str | None = None
    photo_user: str | None = None
    photo_round = False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if self.photo_ad:
            qs = qs.annotate(_photo=_ad_cover(self.photo_ad))
        elif self.photo_user:
            qs = qs.annotate(_photo=_user_photo(self.photo_user))
        return qs

    def _photo_fallback(self, obj) -> str:
        return ""

    def get_list_display_links(self, request, list_display):
        """The photo opens the picture, so the next column (title, e-mail, …) opens the row."""
        links = super().get_list_display_links(request, list_display)
        if links and list(links)[:1] == ["photo"]:
            rest = [c for c in list_display if c not in ("photo", "screenshots", "action_checkbox")]
            return rest[:1]
        return links

    @admin.display(description="Photo")
    def photo(self, obj):
        return thumb(getattr(obj, "_photo", "") or "", round_=bool(self.photo_user), fallback=self._photo_fallback(obj))


class LoginEventInline(admin.TabularInline):
    """Every sign-in of this user, newest first (no IP addresses are kept)."""

    model = LoginEvent
    extra = 0
    max_num = 0
    can_delete = False
    fields = ["at", "method", "device", "ip", "country"]
    readonly_fields = fields
    ordering = ["-at"]
    verbose_name_plural = "Login history"


@admin.register(User)
class UserAdmin(PhotoColumnMixin, BaseUserAdmin):
    photo_user = "pk"
    ordering = ["-date_joined"]
    list_display = ["photo", "email", "display_name", "kind", "country", "ad_count", "age_verified", "date_joined", "last_login",
                    "login_count"]
    list_filter = ["is_advertiser", "age_verified", "is_staff", "is_active", "phone_country"]
    search_fields = ["email", "display_name", "phone"]
    list_per_page = 50
    date_hierarchy = "date_joined"
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("display_name", "phone_country", "phone", "phone_verified")}),
        ("Visitor membership", {"fields": ("vip_until",), "description": "Free, or VIP until this time (messages, comments, reviews and videos)."}),
        ("Advertiser", {"fields": ("is_advertiser", "age_verified", "adult_confirmed_at")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("date_joined", "last_login")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)
    # "Verified" is set only by approving an ID verification (see ID verifications).
    readonly_fields = ["age_verified", "last_login"]
    inlines = [LoginEventInline]

    def get_queryset(self, request):
        from django.db.models import Count
        return super().get_queryset(request).annotate(n_ads=Count("ads", distinct=True), n_logins=Count("logins", distinct=True))

    @admin.display(description="Type", ordering="is_advertiser")
    def kind(self, obj):
        if obj.is_staff:
            return "Staff"
        return "Advertiser" if obj.is_advertiser else ("VIP visitor" if obj.is_vip else "Visitor")

    @admin.display(description="Country", ordering="phone_country")
    def country(self, obj):
        return obj.phone_country

    @admin.display(description="Ads", ordering="n_ads")
    def ad_count(self, obj):
        return obj.n_ads

    @admin.display(description="Logins", ordering="n_logins")
    def login_count(self, obj):
        return obj.n_logins


@admin.register(LoginEvent)
class LoginEventAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_user = "user"
    """Read-only list of every sign-in."""

    list_display = ["photo", "at", "user", "is_advertiser", "method", "device", "ip", "country"]
    list_filter = ["method", "user__is_advertiser", "country"]
    search_fields = ["user__email", "user__display_name", "ip"]
    date_hierarchy = "at"
    list_select_related = ["user"]
    list_per_page = 100

    @admin.display(description="Advertiser", boolean=True, ordering="user__is_advertiser")
    def is_advertiser(self, obj):
        return obj.user.is_advertiser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class AdPictureInline(admin.TabularInline):
    model = AdPicture
    extra = 0
    fields = ["preview", "image", "position", "heart_count"]
    readonly_fields = ["preview", "heart_count"]

    @admin.display(description="Preview")
    def preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="height:80px;border-radius:6px">', obj.image.url)
        return "-"


class AdVideoInline(admin.TabularInline):
    model = AdVideo
    extra = 0
    fields = ["player", "duration", "size", "position"]
    readonly_fields = ["player", "duration", "size"]

    @admin.display(description="Video")
    def player(self, obj):
        if obj.file:
            return format_html('<video src="{}" controls preload="metadata" style="height:120px;border-radius:6px"></video>', obj.file.url)
        return "-"


class AdReviewStateFilter(admin.SimpleListFilter):
    title = "review"
    parameter_name = "review"

    def lookups(self, request, model_admin):
        return [("admin", "Reviewed by an admin"), ("auto", "Went live without review"), ("waiting", "Waiting for review")]

    def queryset(self, request, queryset):
        if self.value() == "admin":
            return queryset.filter(reviewed_by__isnull=False)
        if self.value() == "auto":
            return queryset.filter(reviewed_by__isnull=True, published_at__isnull=False)
        if self.value() == "waiting":
            return queryset.filter(status=Ad.Status.PENDING)
        return queryset


class ReviewerFilter(admin.SimpleListFilter):
    """Only the admins who actually reviewed ads."""

    title = "reviewed by"
    parameter_name = "reviewer"

    def lookups(self, request, model_admin):
        ids = Ad.objects.filter(reviewed_by__isnull=False).values_list("reviewed_by", flat=True).distinct()
        return [(u.pk, u.display_name or u.email) for u in User.objects.filter(pk__in=ids)]

    def queryset(self, request, queryset):
        return queryset.filter(reviewed_by_id=self.value()) if self.value() else queryset


@admin.register(Ad)
class AdAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_ad = "pk"
    list_display = ["photo", "title", "owner", "category", "region", "status", "review", "tier", "tier_until", "owner_verified",
                    "open_reports", "published_at", "expires_at"]
    list_filter = ["status", AdReviewStateFilter, ReviewerFilter, "tier", "category", "region", "gender"]
    search_fields = ["title", "description", "owner__email", "phone"]
    readonly_fields = ["view_count", "heart_count", "created_at", "updated_at", "reviewed_by", "reviewed_at"]
    filter_horizontal = ["tags"]
    inlines = [AdPictureInline, AdVideoInline]
    actions = ["approve", "reject", "pause"]
    list_select_related = ["owner", "reviewed_by"]

    # Tabs above the list (templates/admin/core/ad/change_list.html).
    TABS = [("all", "All"), ("pending", "Waiting for review"), ("active", "Live"), ("rejected", "Rejected"),
            ("paused", "Paused"), ("draft", "Drafts")]

    def changelist_view(self, request, extra_context=None):
        from django.db.models import Count
        current = request.GET.get("status__exact", "all")
        counts = dict(Ad.objects.values_list("status").annotate(n=Count("id")))
        counts["all"] = sum(counts.values())
        tabs = [{"key": k, "label": label, "count": counts.get(k, 0), "active": k == current,
                 "url": "?" if k == "all" else f"?status__exact={k}"} for k, label in self.TABS]
        return super().changelist_view(request, {**(extra_context or {}), "status_tabs": tabs})

    @staticmethod
    def mark_reviewed(ads, user):
        from django.utils import timezone
        Ad.objects.filter(pk__in=[a.pk for a in ads]).update(reviewed_by=user, reviewed_at=timezone.now())

    def save_model(self, request, obj, form, change):
        """Changing the status by hand on the ad's page counts as a review too."""
        if change and "status" in form.changed_data and obj.status in (Ad.Status.ACTIVE, Ad.Status.REJECTED, Ad.Status.PAUSED):
            from django.utils import timezone
            obj.reviewed_by, obj.reviewed_at = request.user, timezone.now()
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        from django.db.models import Count, OuterRef, Subquery
        n = AdPicture.objects.filter(ad=OuterRef("pk")).values("ad").annotate(c=Count("id")).values("c")
        return super().get_queryset(request).annotate(_photos=Subquery(n))

    @admin.display(description="Photo")
    def photo(self, obj):
        n = obj._photos or 0
        return thumb(obj._photo or "", size=52, fallback="no", badge=str(n) if n > 1 else "")

    @admin.display(description="Review", ordering="reviewed_at")
    def review(self, obj):
        if obj.status == Ad.Status.PENDING:
            return format_html('<span style="color:#d48806;font-weight:700">Waiting</span>')
        if obj.reviewed_by_id:
            who = obj.reviewed_by.display_name or obj.reviewed_by.email
            return format_html('<b>{}</b><br><small>{}</small>', who, obj.reviewed_at.strftime("%d %b %Y %H:%M") if obj.reviewed_at else "")
        return format_html('<span style="opacity:.6">{}</span>', "Automatic" if obj.published_at else "–")

    @admin.display(boolean=True, description="Owner 18+ verified")
    def owner_verified(self, obj):
        return obj.owner.age_verified

    @admin.display(description="Open reports")
    def open_reports(self, obj):
        return obj.reports.filter(status=Report.Status.OPEN).count()

    @admin.action(description="Approve and publish")
    def approve(self, request, queryset):
        approved, skipped = [], 0
        for ad in queryset.select_related("owner"):
            if not ad.owner.age_verified:
                skipped += 1
                continue
            ad.approve()
            approved.append(ad)
        self.mark_reviewed(approved, request.user)
        self.message_user(request, f"Approved {len(approved)} ad(s).")
        if skipped:
            self.message_user(
                request,
                f"Skipped {skipped} ad(s): the owner is not age-verified yet.",
                level=messages.WARNING,
            )

    @admin.action(description="Reject")
    def reject(self, request, queryset):
        ads = []
        for ad in queryset.select_related("owner"):  # one by one so the owner is notified
            ad.status, ad.rejection_reason = Ad.Status.REJECTED, "Does not meet our guidelines."
            ad.save(update_fields=["status", "rejection_reason", "updated_at"])
            ads.append(ad)
        self.mark_reviewed(ads, request.user)
        self.message_user(request, f"Rejected {len(ads)} ad(s).")

    @admin.action(description="Pause (hide from site)")
    def pause(self, request, queryset):
        from .notifications import moderator_paused

        ads = list(queryset.exclude(status=Ad.Status.PAUSED).select_related("owner"))
        n = queryset.update(status=Ad.Status.PAUSED)
        self.mark_reviewed(ads, request.user)
        for ad in ads:
            moderator_paused(ad)
        self.message_user(request, f"Paused {n} ad(s).")


class ReportImageInline(admin.TabularInline):
    model = __import__("core.models", fromlist=["ReportImage"]).ReportImage
    extra = 0
    readonly_fields = ["preview"]

    @admin.display(description="Preview")
    def preview(self, obj):
        return format_html('<a href="{0}" target="_blank"><img src="{0}" style="height:80px"></a>', obj.image.url) if obj.image else "-"


class StatusTabsMixin:
    """Status tabs with counts above a list (templates/admin/_status_tabs.html); the list opens on DEFAULT_TAB."""

    TAB_FIELD = "status"
    TABS: list = []  # [(value, label)]; "all" = no filter
    DEFAULT_TAB = "open"

    def changelist_view(self, request, extra_context=None):
        from django.db.models import Count
        param = f"{self.TAB_FIELD}__exact"
        if not request.GET:
            return redirect(f"{request.path}?{param}={self.DEFAULT_TAB}")
        current = request.GET.get(param, "all")
        if "all" in request.GET:
            query = request.GET.copy()
            query.pop("all")
            request.GET = query
            current = "all"
        counts = dict(self.model.objects.values_list(self.TAB_FIELD).annotate(n=Count("id")))
        counts["all"] = sum(counts.values())
        tabs = [{"key": k, "label": label, "count": counts.get(k, 0), "active": k == current,
                 "url": "?all=1" if k == "all" else f"?{param}={k}"} for k, label in self.TABS]
        return super().changelist_view(request, {**(extra_context or {}), "status_tabs": tabs})


@admin.register(Report)
class ReportAdmin(PhotoColumnMixin, StatusTabsMixin, admin.ModelAdmin):
    photo_ad = "ad"
    TABS = [("open", "Open"), ("resolved", "Resolved"), ("dismissed", "Dismissed"), ("draft", "Not confirmed yet"), ("all", "All")]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("images")

    @admin.display(description="Screenshots")
    def screenshots(self, obj):
        from django.utils.html import format_html_join
        imgs = list(obj.images.all())[:4]
        if not imgs:
            return "–"
        return format_html_join("", '<span style="display:inline-block;margin-right:4px">{}</span>',
                                ((thumb(i.image.name, size=38),) for i in imgs))
    inlines = [ReportImageInline]
    list_display = ["photo", "screenshots", "created_at", "urgent", "reason", "ad", "status", "reporter_name", "contact_email"]
    list_filter = ["status", "reason"]
    search_fields = ["message", "ad__title", "contact_email"]
    actions = ["resolve_and_pause_ad", "dismiss"]

    @admin.display(boolean=True, description="Urgent")
    def urgent(self, obj):
        return obj.is_urgent

    @admin.action(description="Resolve and pause the reported ad")
    def resolve_and_pause_ad(self, request, queryset):
        from .notifications import moderator_paused, report_reviewed

        for report in queryset.select_related("ad__owner", "reporter"):
            if report.ad.status != Ad.Status.PAUSED:
                report.ad.status = Ad.Status.PAUSED
                report.ad.save(update_fields=["status"])
                moderator_paused(report.ad)
            report_reviewed(report)
        n = queryset.update(status=Report.Status.RESOLVED)
        self.message_user(request, f"Resolved {n} report(s) and paused the ads.")

    @admin.action(description="Dismiss")
    def dismiss(self, request, queryset):
        from .notifications import report_reviewed

        for report in queryset.select_related("reporter"):
            report_reviewed(report)
        n = queryset.update(status=Report.Status.DISMISSED)
        self.message_user(request, f"Dismissed {n} report(s).")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "color"]
    prepopulated_fields = {"slug": ["name"]}
    search_fields = ["name"]


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display_links = ["title"]
    list_display = ["cover", "title", "kind", "slug", "aliases", "is_published", "show_in_menu", "menu_order", "published_at", "has_pdf"]
    list_filter = ["kind", "is_published", "show_in_menu"]
    prepopulated_fields = {"slug": ["title"]}
    search_fields = ["title", "body"]

    @admin.display(boolean=True, description="PDF")
    def has_pdf(self, obj):
        return bool(obj.document)

    @admin.display(description="Cover")
    def cover(self, obj):
        return thumb(obj.image.name if obj.image else "", size=52, fallback="–")


admin.site.register(Heart)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """Single record: the list page opens the form directly."""

    fieldsets = (
        ("Brand", {"fields": ("site_name", "tagline", "logo", "favicon", "accent_color", "accent_color_2")}),
        ("Search engines", {"fields": ("meta_title", "meta_description")}),
        ("Contact & social", {"fields": ("contact_email", "support_hours", "facebook_url", "instagram_url", "x_url")}),
        ("About page & footer", {"fields": ("about_intro", "copyright_name", "country")}),
        ("Ads", {"fields": ("review_ads", "free_ad_days", "slider_slots", "show_view_counts")}),
        ("Money", {"fields": ("currency", "coin_value"),
                   "description": "Changing the currency changes all plan and coin-package prices to that currency (the numbers stay)."}),
    )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect("admin:core_sitesettings_change", SiteSettings.load().pk)


class PriceInline(admin.TabularInline):
    model = Product
    fields = ["days", "price", "currency", "is_active", "code"]
    readonly_fields = ["code"]
    extra = 0
    ordering = ["days"]
    verbose_name = "price"
    verbose_name_plural = "Prices per duration (price in øre/cents: 19900 = 199.00)"


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ["name", "tier", "price_overview", "is_highlighted", "is_active", "sort_order"]
    list_editable = ["is_highlighted", "is_active", "sort_order"]
    inlines = [PriceInline]

    @admin.display(description="Prices")
    def price_overview(self, obj):
        return " · ".join(
            f"{p.get_days_display()}: {p.price / 100:.0f} {p.currency}" for p in obj.prices.filter(is_active=True)
        )


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "plan", "days", "price_display", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["plan", "is_active", "days"]

    @admin.display(description="Price")
    def price_display(self, obj):
        return f"{obj.price / 100:.2f} {obj.currency}"


class AddOnOptionInline(admin.TabularInline):
    model = AddOn
    extra = 1
    fields = ["days", "price", "is_active", "code"]
    readonly_fields = ["code"]
    verbose_name = "period and price"
    verbose_name_plural = "Periods and prices (price in øre/cents incl. VAT, e.g. 4900 = 49.00)"


@admin.register(AddOnGroup)
class AddOnGroupAdmin(admin.ModelAdmin):
    """Add-ons on the payment page (First in search, Front page ad, …). Set the periods and prices below each one."""

    list_display = ["name", "badge", "effect", "prices", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    fields = ["name", "badge", "description", "effect", "is_active", "sort_order"]
    inlines = [AddOnOptionInline]

    @admin.display(description="Periods and prices")
    def prices(self, obj):
        return " · ".join(f"{o.period_label}: {o.price / 100:.0f} {o.currency}" for o in obj.options.all())


@admin.register(Order)
class OrderAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_ad = "ad"
    list_display = ["photo", "order_id", "created_at", "user", "ad", "product_name", "amount_display", "status", "gateway", "paid_at"]
    list_filter = ["status", "gateway", "tier"]
    search_fields = ["order_id", "user__email", "ad__title", "gateway_payment_id"]
    readonly_fields = [f.name for f in Order._meta.fields]
    actions = ["refresh_from_gateway", "refund"]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Amount")
    def amount_display(self, obj):
        return f"{obj.amount / 100:.2f} {obj.currency}"

    @admin.action(description="Re-check payment status with the gateway")
    def refresh_from_gateway(self, request, queryset):
        for order in queryset:
            payments.refresh_order(order)
        self.message_user(request, "Checked the selected orders.")

    @admin.action(description="Refund (Stripe: money goes back to the card automatically)")
    def refund(self, request, queryset):
        done = 0
        for order in queryset:
            try:
                payments.refund_order(order)
                done += 1
            except payments.PaymentError as exc:
                self.message_user(request, f"{order.order_id}: {exc}", level=messages.ERROR)
        if done:
            self.message_user(request, f"Refunded {done} order(s) and took back their coins. Stripe orders are refunded to the card "
                              "automatically; adjust the ads' tiers if needed.")


class MessageInline(admin.TabularInline):
    model = Message
    fields = ["created_at", "sender", "body"]
    readonly_fields = fields
    extra = 0
    can_delete = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_ad = "ad"
    """For handling abuse reports. Open conversations only when there is a reason to."""

    list_display = ["photo", "ad_title", "visitor", "advertiser", "last_message_at", "blocked_by"]
    search_fields = ["ad_title", "visitor__email", "advertiser__email"]
    readonly_fields = ["ad", "ad_title", "visitor", "advertiser", "created_at", "last_message_at", "visitor_read_at",
                       "advertiser_read_at", "blocked_by"]
    inlines = [MessageInline]

    def has_add_permission(self, request):
        return False


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ["banner", "title", "discount_percent", "applies_to", "tier", "code", "uses", "starts_at", "ends_at", "is_active", "show_on_home"]
    list_display_links = ["banner", "title"]
    list_filter = ["is_active", "applies_to", "show_on_home"]
    list_editable = ["is_active", "show_on_home"]
    fields = ["title", "text", "image", "banner", "button_label", "discount_percent", "applies_to", "tier", "code",
              "max_uses", "once_per_user", "starts_at", "ends_at", "no_end_date", "is_active", "show_on_home"]
    readonly_fields = ["banner"]

    @admin.display(description="Used")
    def uses(self, obj):
        n = obj.orders.filter(status="paid").count()
        return f"{n} / {obj.max_uses}" if obj.max_uses else n

    @admin.display(description="Photo")
    def banner(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="height:60px;border-radius:6px">', obj.image.url)
        return "-"


@admin.register(DiamondTransaction)
class DiamondTransactionAdmin(admin.ModelAdmin):
    """Coin ledger. To give coins, add a row with a positive amount – the balance updates automatically."""

    list_display = ["created_at", "user", "amount", "reason"]
    search_fields = ["user__email", "reason"]

    def save_model(self, request, obj, form, change):
        if change:
            return  # ledger rows are immutable
        from . import economy
        economy.add_diamonds(obj.user, obj.amount, obj.reason or f"Adjusted by {request.user.email}")

    def has_change_permission(self, request, obj=None):
        return obj is None


@admin.register(Comment)
class CommentAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_ad = "ad"
    list_display = ["photo", "created_at", "ad", "user", "body", "is_hidden"]
    list_filter = ["is_hidden"]
    list_editable = ["is_hidden"]
    search_fields = ["body", "ad__title", "user__email"]


@admin.register(AdReview)
class AdReviewAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_ad = "ad"
    list_display = ["photo", "created_at", "ad", "user", "rating", "body", "is_hidden"]
    list_filter = ["is_hidden", "rating"]
    list_editable = ["is_hidden"]
    search_fields = ["body", "ad__title", "user__email"]


# ---------------------------------------------------------------------------
# ID verification review
# ---------------------------------------------------------------------------


class ResubmittedFilter(admin.SimpleListFilter):
    """ID requests from people who were rejected before (and sent new documents)."""

    title = "history"
    parameter_name = "history"

    def lookups(self, request, model_admin):
        return [("again", "Sent again after rejection"), ("first", "First request")]

    def queryset(self, request, queryset):
        from django.db.models import Exists, OuterRef
        earlier_rejected = IdVerification.objects.filter(user_id=OuterRef("user_id"), created_at__lt=OuterRef("created_at"),
                                                         status=IdVerification.Status.REJECTED)
        earlier_any = IdVerification.objects.filter(user_id=OuterRef("user_id"), created_at__lt=OuterRef("created_at"))
        if self.value() == "again":
            return queryset.filter(Exists(earlier_rejected))
        if self.value() == "first":
            return queryset.exclude(Exists(earlier_any))
        return queryset


@admin.register(IdVerification)
class IdVerificationAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_user = "user"  # the public profile/ad photo – never the ID documents
    list_display = ["photo", "user", "full_name", "document_type", "age", "status", "history", "created_at", "reviewed_at"]
    list_filter = ["status", "document_type", ResubmittedFilter]
    search_fields = ["user__email", "user__display_name", "full_name"]
    list_select_related = ["user"]
    fields = ["user", "document_type", "full_name", "date_of_birth", "age", "history", "earlier_requests", "pictures",
              "status", "reject_reason", "created_at", "reviewed_at", "reviewed_by"]
    readonly_fields = ["user", "document_type", "full_name", "date_of_birth", "age", "history", "earlier_requests", "pictures",
                       "created_at", "reviewed_at", "reviewed_by"]
    actions = ["approve", "reject"]

    # Tabs above the list (templates/admin/core/idverification/change_list.html). The page opens on "Requested".
    TABS = [("pending", "Requested"), ("approved", "Verified"), ("rejected", "Rejected"), ("all", "All")]

    def changelist_view(self, request, extra_context=None):
        from django.db.models import Count
        if not request.GET:  # always start with the people who are waiting
            return redirect(f"{request.path}?status__exact=pending")
        current = request.GET.get("status__exact", "")
        if "all" in request.GET:  # the "All" tab: our own marker, not a filter Django knows
            query = request.GET.copy()
            query.pop("all")
            request.GET = query
            current = "all"
        counts = dict(IdVerification.objects.values_list("status").annotate(n=Count("id")))
        counts["all"] = sum(counts.values())
        tabs = [{"key": key, "label": label, "count": counts.get(key, 0), "active": key == current,
                 "url": "?all=1" if key == "all" else f"?status__exact={key}"} for key, label in self.TABS]
        return super().changelist_view(request, {**(extra_context or {}), "id_tabs": tabs})

    def has_add_permission(self, request):
        return False

    @admin.display(description="History")
    def history(self, obj):
        from .admin_feed import verification_history
        h = verification_history(obj)
        colours = {"pink": "#e94fc2", "amber": "#d48806", "blue": "#3b82f6"}
        return format_html('<span style="font-weight:700;color:{}">{}</span>', colours[h["tone"]], h["label"])

    @admin.display(description="Earlier requests")
    def earlier_requests(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html_join
        rows = IdVerification.objects.filter(user_id=obj.user_id).exclude(pk=obj.pk).order_by("-created_at")
        if not rows:
            return "None – this is the first request."
        return format_html_join(
            "", '<div><a href="{}">{}</a> – {}{}</div>',
            ((reverse("admin:core_idverification_change", args=[r.pk]), r.created_at.strftime("%d %b %Y %H:%M"),
              r.get_status_display(), f" ({r.reject_reason})" if r.reject_reason else "") for r in rows))

    @admin.display(description="Age")
    def age(self, obj):
        from django.utils import timezone
        t, d = timezone.localdate(), obj.date_of_birth
        return t.year - d.year - ((t.month, t.day) < (d.month, d.day))

    @admin.display(description="Documents")
    def pictures(self, obj):
        from django.urls import reverse
        from django.utils.html import format_html_join
        parts = [(reverse("admin:core_idverification_file", args=[obj.pk, name]), label)
                 for name, label in (("document_front", "Front"), ("document_back", "Back"), ("selfie", "Selfie with document"))
                 if getattr(obj, name)]
        if not parts:
            return "Deleted after review" if obj.reviewed_at else "-"
        return format_html_join("", '<a href="{0}" target="_blank" style="display:inline-block;margin:0 12px 12px 0;text-align:center">'
                                '<img src="{0}" style="height:220px;border-radius:8px;display:block"><small>{1}</small></a>', parts)

    def get_urls(self):
        from django.urls import path
        return [path("<int:pk>/file/<str:name>/", self.admin_site.admin_view(self.serve_file), name="core_idverification_file")] + super().get_urls()

    def serve_file(self, request, pk, name):
        from django.http import FileResponse, Http404
        obj = self.get_object(request, str(pk))
        if not obj or not self.has_view_permission(request, obj) or name not in ("document_front", "document_back", "selfie"):
            raise Http404
        f = getattr(obj, name)
        if not f:
            raise Http404
        resp = FileResponse(f.open("rb"), content_type="image/webp")
        resp["Cache-Control"] = "private, no-store"
        return resp

    def save_model(self, request, obj, form, change):
        old = IdVerification.objects.get(pk=obj.pk).status if change else None
        if change and obj.status != old and obj.status != IdVerification.Status.PENDING:
            obj.decide(obj.status == IdVerification.Status.APPROVED, by=request.user, reason=obj.reject_reason)
        else:
            super().save_model(request, obj, form, change)

    @admin.action(description="Approve – documents match, 18+")
    def approve(self, request, queryset):
        n = 0
        for v in queryset.filter(status=IdVerification.Status.PENDING).select_related("user"):
            v.decide(True, by=request.user)
            n += 1
        self.message_user(request, f"Approved {n} verification(s).")

    @admin.action(description="Reject – documents unclear or not matching")
    def reject(self, request, queryset):
        n = 0
        for v in queryset.filter(status=IdVerification.Status.PENDING).select_related("user"):
            v.decide(False, by=request.user, reason="The pictures were unclear or did not match. Please send new ones.")
            n += 1
        self.message_user(request, f"Rejected {n} verification(s).")


# ---------------------------------------------------------------------------
# Countries, regions, categories and option lists (shown everywhere on the site)
# ---------------------------------------------------------------------------


class InUseProtectedAdmin(admin.ModelAdmin):
    """Entries used by ads can't be deleted – untick "is active" to hide them instead."""

    ad_field = ""

    def _in_use(self, obj):
        return obj is not None and Ad.objects.filter(**{self.ad_field: self._value(obj)}).exists()

    def _value(self, obj):
        return obj.slug

    @admin.display(description="Ads")
    def ad_count(self, obj):
        return Ad.objects.filter(**{self.ad_field: self._value(obj)}).count()

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not self._in_use(obj)

    def delete_queryset(self, request, queryset):
        used = [o for o in queryset if self._in_use(o)]
        if used:
            self.message_user(request, f"Not deleted (used by ads – make them inactive instead): {', '.join(map(str, used))}", messages.WARNING)
        queryset.exclude(pk__in=[o.pk for o in used]).delete()


class AdRegionInline(admin.TabularInline):
    model = AdRegion
    extra = 1
    show_change_link = True  # opens the area, where its places are edited
    fields = ["name", "slug", "lat", "lng", "is_active", "sort_order"]
    prepopulated_fields = {"slug": ["name"]}


def _lookups_changed():
    from .management.commands.seed_world import lookups_changed
    lookups_changed()


class AdPlaceInline(admin.TabularInline):
    model = AdPlace
    extra = 3
    fields = ["name", "slug", "is_active", "sort_order"]


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    """Only active countries are offered on the site. Areas and cities come from `manage.py seed_world`
    (inactive at first) – switch a country's on with the actions below."""
    list_display = ["__str__", "code", "continent", "dial_code", "region_count", "active_regions", "is_active", "sort_order"]
    list_filter = ["is_active", "continent", "has_ads"]
    list_editable = ["is_active", "sort_order"]
    search_fields = ["name", "code"]
    inlines = [AdRegionInline]
    actions = ["activate_country", "deactivate_country", "activate_areas", "deactivate_areas"]

    def get_queryset(self, request):
        from django.db.models import Count, Q
        return super().get_queryset(request).annotate(
            n_regions=Count("regions", distinct=True),
            n_active=Count("regions", filter=Q(regions__is_active=True), distinct=True),
        )

    @admin.display(description="Areas", ordering="n_regions")
    def region_count(self, obj):
        return obj.n_regions

    @admin.display(description="Active areas", ordering="n_active")
    def active_regions(self, obj):
        return obj.n_active

    @admin.action(description="Make active (shown on the site)")
    def activate_country(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=True)} countries active.")
        _lookups_changed()

    @admin.action(description="Make inactive (hidden)")
    def deactivate_country(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=False)} countries hidden.")
        _lookups_changed()

    @admin.action(description="Activate all their areas and cities")
    def activate_areas(self, request, queryset):
        from .management.commands.seed_world import activate_country
        done = [activate_country(c.code, True) for c in queryset]
        queryset.update(is_active=True, has_ads=True)
        self.message_user(request, f"Switched on {sum(d[0] for d in done)} areas and {sum(d[1] for d in done)} cities.")

    @admin.action(description="Deactivate all their areas and cities")
    def deactivate_areas(self, request, queryset):
        from .management.commands.seed_world import activate_country
        done = [activate_country(c.code, False) for c in queryset]
        self.message_user(request, f"Switched off {sum(d[0] for d in done)} areas and {sum(d[1] for d in done)} cities.")


@admin.register(AdRegion)
class AdRegionAdmin(InUseProtectedAdmin):
    """Areas with their places (neighbourhoods/towns) – e.g. add Satmuhane under its area."""
    ad_field = "region"
    list_display = ["name", "slug", "country", "ad_count", "place_count", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["country", "is_active"]
    search_fields = ["name", "slug"]
    list_select_related = ["country"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [AdPlaceInline]
    actions = ["activate_with_places", "deactivate_with_places"]

    def get_inlines(self, request, obj):
        # Very big areas list their cities under Admin → Places instead (the page would get too long).
        return [] if obj is not None and obj.places.count() > 150 else [AdPlaceInline]

    @admin.display(description="Places")
    def place_count(self, obj):
        n = obj.places.count()
        url = f"/admin/core/adplace/?region__id__exact={obj.pk}"
        return format_html('<a href="{}">{}</a>', url, n)

    @admin.action(description="Activate with all their cities")
    def activate_with_places(self, request, queryset):
        n = AdPlace.objects.filter(region__in=queryset).update(is_active=True)
        self.message_user(request, f"{queryset.update(is_active=True)} areas and {n} cities switched on.")
        _lookups_changed()

    @admin.action(description="Deactivate with all their cities")
    def deactivate_with_places(self, request, queryset):
        n = AdPlace.objects.filter(region__in=queryset).update(is_active=False)
        self.message_user(request, f"{queryset.update(is_active=False)} areas and {n} cities switched off.")
        _lookups_changed()


@admin.register(AdPlace)
class AdPlaceAdmin(admin.ModelAdmin):
    """Cities / places inside areas. Only active ones (in an active area) are offered to advertisers."""
    list_display = ["name", "region", "country_name", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    list_filter = ["is_active", ("region__country", admin.RelatedOnlyFieldListFilter)]
    search_fields = ["name", "slug", "region__name", "region__country__name"]
    list_select_related = ["region", "region__country"]
    autocomplete_fields = ["region"]
    fields = ["region", "name", "slug", "lat", "lng", "is_active", "sort_order"]
    actions = ["activate", "deactivate"]
    list_per_page = 100

    @admin.display(description="Country", ordering="region__country__name")
    def country_name(self, obj):
        return obj.region.country.name

    @admin.action(description="Activate")
    def activate(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=True)} places switched on.")
        _lookups_changed()

    @admin.action(description="Deactivate")
    def deactivate(self, request, queryset):
        self.message_user(request, f"{queryset.update(is_active=False)} places switched off.")
        _lookups_changed()


@admin.register(AdCategory)
class AdCategoryAdmin(InUseProtectedAdmin):
    ad_field = "category"
    list_display = ["name", "slug", "ad_count", "is_active", "sort_order"]
    list_editable = ["is_active", "sort_order"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(ChoiceOption)
class ChoiceOptionAdmin(InUseProtectedAdmin):
    list_display = ["label", "value", "group", "ad_count", "show_in_menu", "menu_label", "is_active", "sort_order"]
    list_editable = ["show_in_menu", "menu_label", "is_active", "sort_order"]
    list_filter = ["group", "is_active"]
    search_fields = ["label", "value"]

    def _value(self, obj):
        return obj.value

    def ad_count(self, obj):
        if obj.group == ChoiceOption.Group.AUDIENCE:
            return Ad.objects.filter(audiences__icontains=f'"{obj.value}"').count()
        if obj.group == ChoiceOption.Group.SERVICE:
            # Ticked services are stored on the ad by name: services = [{"name": "Anal", "price": ""}, ...]
            return Ad.objects.filter(services__icontains=f'"{obj.label}"').count()
        return Ad.objects.filter(**{obj.group: obj.value}).count()
    ad_count.short_description = "Ads"

    def _in_use(self, obj):
        return obj is not None and self.ad_count(obj) > 0


@admin.register(UserReport)
class UserReportAdmin(PhotoColumnMixin, StatusTabsMixin, admin.ModelAdmin):
    photo_user = "reported"
    TABS = [("open", "Open"), ("resolved", "Resolved"), ("dismissed", "Dismissed"), ("all", "All")]
    list_display = ["photo", "created_at", "urgent", "reason", "reported", "reporter", "status", "chat"]
    list_filter = ["status", "reason"]
    search_fields = ["message", "reported__email", "reporter__email"]
    raw_id_fields = ["reporter", "reported", "conversation"]
    actions = ["resolve", "resolve_and_deactivate", "dismiss"]

    @admin.display(boolean=True, description="Urgent")
    def urgent(self, obj):
        return obj.is_urgent

    @admin.display(description="Chat")
    def chat(self, obj):
        if not obj.conversation_id:
            return "–"
        return format_html('<a href="../conversation/{}/change/">Open chat</a>', obj.conversation_id)

    @admin.action(description="Resolve")
    def resolve(self, request, queryset):
        self.message_user(request, f"Resolved {queryset.update(status=UserReport.Status.RESOLVED)} report(s).")

    @admin.action(description="Resolve and deactivate the reported user")
    def resolve_and_deactivate(self, request, queryset):
        users = User.objects.filter(pk__in=queryset.values("reported_id")).exclude(is_staff=True)
        n_users = users.update(is_active=False)
        n = queryset.update(status=UserReport.Status.RESOLVED)
        self.message_user(request, f"Resolved {n} report(s) and deactivated {n_users} user(s).")

    @admin.action(description="Dismiss")
    def dismiss(self, request, queryset):
        self.message_user(request, f"Dismissed {queryset.update(status=UserReport.Status.DISMISSED)} report(s).")


class ContactReplyInline(admin.TabularInline):
    """What the team answered (newest last). New answers are written in the 'Reply' box above."""

    model = ContactReply
    extra = 0
    max_num = 0
    can_delete = False
    fields = ["sent_at", "sent_by", "body", "emailed"]
    readonly_fields = fields
    verbose_name_plural = "Replies sent"


class ContactMessageForm(forms.ModelForm):
    reply = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 8, "style": "width:min(100%,720px)", "placeholder": "Write your answer – it is e-mailed to the sender when you save."}),
        help_text="Sent by e-mail to the sender (reply-to: the support address) and kept below under 'Replies sent'. "
                  "Saving with a reply also marks the message as handled.")

    class Meta:
        model = ContactMessage
        fields = ["handled"]


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    form = ContactMessageForm
    list_display = ["created_at", "topic", "name", "email", "handled", "replies_sent", "last_reply"]
    list_filter = ["handled", "topic"]
    list_editable = ["handled"]
    search_fields = ["name", "email", "message"]
    readonly_fields = ["topic", "name", "email", "message", "user", "created_at"]
    fields = ["topic", "name", "email", "user", "created_at", "message", "reply", "handled"]
    inlines = [ContactReplyInline]

    TABS = [("unhandled", "Unhandled"), ("handled", "Handled"), ("all", "All")]

    def changelist_view(self, request, extra_context=None):
        if not request.GET:  # start with what still needs an answer
            return redirect(f"{request.path}?handled__exact=0")
        current = {"0": "unhandled", "1": "handled"}.get(request.GET.get("handled__exact", ""), "all")
        if "all" in request.GET:
            query = request.GET.copy()
            query.pop("all")
            request.GET = query
            current = "all"
        open_n = ContactMessage.objects.filter(handled=False).count()
        total = ContactMessage.objects.count()
        counts = {"unhandled": open_n, "handled": total - open_n, "all": total}
        urls = {"unhandled": "?handled__exact=0", "handled": "?handled__exact=1", "all": "?all=1"}
        tabs = [{"key": k, "label": label, "count": counts[k], "active": k == current, "url": urls[k]} for k, label in self.TABS]
        return super().changelist_view(request, {**(extra_context or {}), "status_tabs": tabs})

    def get_queryset(self, request):
        from django.db.models import Count, Max
        return super().get_queryset(request).annotate(n_replies=Count("replies"), last_reply_at=Max("replies__sent_at"))

    @admin.display(description="Replies", ordering="n_replies")
    def replies_sent(self, obj):
        return obj.n_replies

    @admin.display(description="Last reply", ordering="last_reply_at")
    def last_reply(self, obj):
        return obj.last_reply_at.strftime("%d %b %Y %H:%M") if obj.last_reply_at else "–"

    def save_model(self, request, obj, form, change):
        text = (form.cleaned_data.get("reply") or "").strip()
        if text:
            obj.handled = True
        super().save_model(request, obj, form, change)
        if text:
            reply = ContactReply.objects.create(message=obj, body=text, sent_by=request.user)
            try:
                send_contact_reply(obj, text)
                reply.emailed = True
                reply.save(update_fields=["emailed"])
                self.message_user(request, f"Your reply was e-mailed to {obj.email}.")
            except Exception as exc:  # noqa: BLE001 – keep the reply, tell the admin it wasn't sent
                self.message_user(request, f"The reply is saved, but the e-mail could not be sent: {exc}", messages.ERROR)


def send_contact_reply(msg, text: str) -> None:
    from . import emails
    lines = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    quoted = (msg.message or "")[:600]
    emails.send(
        to=msg.email, user=msg.user, background=False, fail_silently=False,
        subject=f"Re: {msg.get_topic_display()}",
        heading=f"Hi {msg.name}",
        lines=[*lines, f"— You wrote: \u201c{quoted}{'…' if len(msg.message or '') > 600 else ''}\u201d"],
        note="You can answer this e-mail directly.",
    )


@admin.register(FaqItem)
class FaqItemAdmin(admin.ModelAdmin):
    list_display = ["question", "category", "order", "is_published"]
    list_editable = ["category", "order", "is_published"]
    list_filter = ["category", "is_published"]
    search_fields = ["question", "answer"]


@admin.register(LegacyAccount)
class LegacyAccountAdmin(PhotoColumnMixin, admin.ModelAdmin):
    photo_user = "user"
    list_display = ("photo", "legacy_username", "user", "invited_at", "claimed_at", "reward_tier", "reward_days")
    list_filter = (("invited_at", admin.EmptyFieldListFilter), ("claimed_at", admin.EmptyFieldListFilter), "reward_tier")
    search_fields = ("legacy_username", "user__email")
    readonly_fields = ("user", "ad", "legacy_id", "legacy_username", "legacy_ad_id", "data", "created_at", "claim_link")
    actions = ["send_invite"]

    @admin.display(description="Claim link")
    def claim_link(self, obj):
        from django.conf import settings

        from . import legacy
        return f"{settings.PUBLIC_SITE_URL}{legacy.claim_link(obj)}" if obj.pk else ""

    @admin.action(description="Send the invite e-mail")
    def send_invite(self, request, queryset):
        from . import legacy
        done = 0
        for acc in queryset.filter(claimed_at__isnull=True).select_related("user"):
            try:
                legacy.send_invite(acc)
                done += 1
            except Exception as exc:  # noqa: BLE001
                self.message_user(request, f"{acc}: {exc}", level=messages.ERROR)
        self.message_user(request, f"Sent {done} invite e-mail(s).")
