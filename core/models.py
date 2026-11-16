import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.files.storage import FileSystemStorage
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

# ---------------------------------------------------------------------------
# Default choices. The live lists are database tables (Country, Region, Category,
# ChoiceOption below) edited in the admin; these enums only seed them and the demo data.
# ---------------------------------------------------------------------------


class Region(models.TextChoices):
    COPENHAGEN = "copenhagen", "Copenhagen"
    NORTH_ZEALAND = "north-zealand", "North Zealand"
    REST_OF_ZEALAND = "rest-of-zealand", "Rest of Zealand"
    LOLLAND = "lolland", "Lolland-Falster"
    NORTH_JUTLAND = "north-jutland", "North Jutland"
    AARHUS = "aarhus", "Aarhus"
    MID_WEST_JUTLAND = "mid-west-jutland", "Mid & West Jutland"
    SOUTH_JUTLAND = "south-jutland", "South Jutland"
    FUNEN = "funen", "Funen"
    BORNHOLM = "bornholm", "Bornholm"


class Category(models.TextChoices):
    ESCORT = "escort", "Escort"
    MASSAGE = "massage", "Massage"
    TANTRIC = "tantric-massage", "Tantric massage"
    CLINIC = "clinic", "Clinic"
    STRIP = "strip", "Strip & shows"
    PHONE = "phone", "Phone"
    WEBCAM = "webcam", "Webcam"
    EVENT = "event", "Events"


class Gender(models.TextChoices):
    FEMALE = "female", "Woman"
    MALE = "male", "Man"
    COUPLE = "couple", "Couple"
    TRANS = "trans", "Trans"


class Audience(models.TextChoices):
    WOMEN = "women", "Women"
    MEN = "men", "Men"
    COUPLES = "couples", "Couples"


class Accessibility(models.TextChoices):
    NO = "no", "No"
    PARTLY = "partly", "Partly"
    YES = "yes", "Yes"


class Tier(models.TextChoices):
    FREE = "free", "Free"
    STANDARD = "standard", "Standard"
    PREMIUM = "premium", "Premium"
    VIP = "vip", "VIP"


TIER_RANK = {Tier.FREE: 0, Tier.STANDARD: 1, Tier.PREMIUM: 2, Tier.VIP: 3}
PAID_TIERS = [Tier.STANDARD, Tier.PREMIUM, Tier.VIP]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create_user(email, password, **extra)


def profile_picture_upload_to(instance, filename):
    return f"profiles/{uuid.uuid4().hex}.webp"


class User(AbstractUser):
    """Email-based account. Visitors and advertisers share one model."""

    class SenderName(models.TextChoices):
        STANDARD = "standard", "Standard (site name)"
        NEUTRAL = "neutral", "Neutral (Notification)"

    class Language(models.TextChoices):
        EN = "en", "English"
        DA = "da", "Dansk"

    class Theme(models.TextChoices):
        SYSTEM = "system", "System"
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    username = None
    email = models.EmailField(unique=True)
    email_confirmed = models.BooleanField(default=False)
    display_name = models.CharField(max_length=60, blank=True)
    phone_country = models.CharField(max_length=2, default="NP", help_text="ISO country code for the phone prefix")
    phone = models.CharField(max_length=30, blank=True)
    phone_verified = models.BooleanField(default=False)
    profile_picture = models.ImageField(upload_to=profile_picture_upload_to, blank=True)
    sender_name = models.CharField(max_length=10, choices=SenderName.choices, default=SenderName.STANDARD)
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    theme = models.CharField(max_length=6, choices=Theme.choices, default=Theme.DARK)
    notify_messages = models.BooleanField(default=True, help_text="Email me about new messages")
    notify_expiry = models.BooleanField(default=True, help_text="Email me before my ads expire")
    notify_offers = models.BooleanField(default=False, help_text="Email me about offers")
    diamonds = models.IntegerField("coins", default=0, help_text="Coin balance (value per coin: Site settings → Coin value; changes are logged in the coin ledger)")
    show_ad_history = models.BooleanField(default=True, help_text="Show my expired ads on my public profile")
    is_advertiser = models.BooleanField(default=False)
    adult_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Advertisers must be verified as 18+ by staff (ID check) before ads go live.
    age_verified = models.BooleanField(default=False)
    # Visitor membership: Free, or VIP until this time (messages, comments, reviews and ad videos).
    vip_until = models.DateTimeField("VIP until", null=True, blank=True, help_text="Visitor VIP membership ends at this time")

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    @property
    def is_vip(self):
        """VIP visitor (staff always count as VIP)."""
        return bool(self.is_staff or (self.vip_until and self.vip_until > timezone.now()))

    @property
    def customer_no(self):
        return f"C-{self.pk:07d}" if self.pk else ""

    def __str__(self):
        return self.display_name or self.email


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


TAG_COLORS = ["#F2A78C", "#F7C873", "#A8D5A2", "#8FD3E8", "#B9A6F2", "#F2A0C4", "#9FC5F8", "#F5B5A8", "#C5E1A5", "#FFD180"]


class Tag(models.Model):
    name = models.CharField(max_length=40, unique=True)
    slug = models.SlugField(max_length=50, unique=True)
    color = models.CharField(max_length=7, blank=True, help_text="Pill colour, e.g. #F2A78C. Left empty, one is picked.")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                   help_text="Set when an advertiser created the tag in the ad editor")

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        if not self.color:
            self.color = TAG_COLORS[sum(map(ord, self.slug)) % len(TAG_COLORS)]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class AdQuerySet(models.QuerySet):
    def live(self):
        now = timezone.now()
        return self.filter(status=Ad.Status.ACTIVE).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
        )


class Ad(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Awaiting review"
        ACTIVE = "active", "Live"
        REJECTED = "rejected", "Rejected"
        PAUSED = "paused", "Paused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ads"
    )
    title = models.CharField(max_length=80)
    slug = models.SlugField(max_length=90, blank=True)
    headline = models.CharField(max_length=140, blank=True)
    description = models.TextField(max_length=20000, help_text="Plain text or cleaned HTML from the editor (core/richtext.py)")
    category = models.CharField(max_length=40, blank=True, help_text="Main category slug (= first of `categories`)")
    gender = models.CharField(max_length=20, blank=True, help_text="Option value (Admin → Options → Gender)")
    audiences = models.JSONField(default=list, blank=True)
    region = models.CharField(max_length=40, blank=True, help_text="Main region slug (= first of `regions`)")
    regions = models.JSONField(default=list, blank=True, help_text="All region slugs the ad is shown in")
    places = models.JSONField(default=list, blank=True, help_text="Places (neighbourhoods/towns) inside the chosen areas")
    categories = models.JSONField(default=list, blank=True, help_text="All category slugs (first = main `category`)")
    city = models.CharField(max_length=60, blank=True)
    age = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(18), MaxValueValidator(99)],
        help_text="Empty while the ad is a draft; required to activate",
    )
    languages = models.CharField(max_length=120, blank=True)
    phone_country = models.CharField(max_length=2, default="NP", help_text="Country of the phone number (dial code)")
    # Activation (core/activation.py): the plan waits here until a moderator approves the ad.
    pending_tier = models.CharField(max_length=10, blank=True, help_text="Plan bought before approval")
    pending_days = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Days that start when the ad is approved")
    free_listing = models.BooleanField(default=False, help_text="Running on the Free plan (max. one per advertiser)")
    phone = models.CharField(max_length=30, blank=True)
    show_phone = models.BooleanField(default=True)
    show_call = models.BooleanField(default=True, help_text="Show the 'Call' button")
    show_sms = models.BooleanField(default=True, help_text="Show the 'SMS' button")
    contact_email = models.EmailField(blank=True, help_text="Optional e-mail shown on the ad (never the login e-mail unless chosen)")
    accessibility = models.CharField(max_length=20, default=Accessibility.NO)
    tags = models.ManyToManyField(Tag, blank=True, related_name="ads")
    height_cm = models.PositiveSmallIntegerField(null=True, blank=True)
    body_type = models.CharField(max_length=20, blank=True)
    bust_size = models.CharField(max_length=4, blank=True)
    eye_color = models.CharField(max_length=20, blank=True)
    hair_color = models.CharField(max_length=20, blank=True)
    services = models.JSONField(default=list, blank=True, help_text='[{"name": "60 min massage", "price": "800 kr"}]')
    contact_hours = models.JSONField(default=dict, blank=True, help_text='{"mon": "10–20", ...}')
    contact_note = models.CharField(max_length=200, blank=True, help_text="How to contact, e.g. 'SMS first'")

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    rejection_reason = models.CharField(max_length=200, blank=True)
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.FREE)
    tier_until = models.DateTimeField(null=True, blank=True, help_text="Paid tier ends at this time")
    top_until = models.DateTimeField(null=True, blank=True, help_text="Add-on: shown first in its plan until")
    slider_until = models.DateTimeField(null=True, blank=True, help_text="Add-on: in the front-page slider until")
    published_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    bumped_at = models.DateTimeField(null=True, blank=True)

    view_count = models.PositiveIntegerField(default=0)
    heart_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AdQuerySet.as_manager()

    class Meta:
        ordering = ["-bumped_at", "-published_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "region", "category"]),
            models.Index(fields=["status", "-published_at"]),
        ]

    def needs_go_live(self):
        """Active, but the go-live step never ran (e.g. status set to Active by hand in the admin)."""
        return self.status == self.Status.ACTIVE and (bool(self.pending_days) or not self.published_at or not self.expires_at)

    def save(self, *args, **kwargs):
        self.slug = slugify(self.title)[:90]
        went_live = self.needs_go_live()
        if went_live:
            self._go_live()
            if kwargs.get("update_fields") is not None:  # the go-live fields must be saved too
                kwargs["update_fields"] = set(kwargs["update_fields"]) | set(self.GO_LIVE_FIELDS)
        # Keep the main category/region and the full lists in step (admin, seed data, older clients).
        for single, many in (("category", "categories"), ("region", "regions")):
            value, values = getattr(self, single), list(getattr(self, many) or [])
            if value and value not in values:
                values.insert(0, value)
            elif values and not value:
                value = values[0]
            if values and values[0] != value:
                values.remove(value)
                values.insert(0, value)
            setattr(self, single, value)
            setattr(self, many, values)
        super().save(*args, **kwargs)
        if went_live:
            self._start_waiting_addons()

    def grant_plan_extras(self):
        """What a plan includes beyond its badge (see the plan features). Premium/VIP: bumped to the top (done by the
        callers). VIP: first in every list and the front-page slider for the whole VIP period – the slider only while
        one of the Site settings → Slider slots is free (or the ad already has one). Does not save."""
        if self.tier != Tier.VIP or not self.tier_until:
            return
        self.top_until = max(self.top_until or self.tier_until, self.tier_until)
        from .slider import slots

        info = slots(self)
        if info["has_slot"] or info["free"] > 0:
            self.slider_until = max(self.slider_until or self.tier_until, self.tier_until)

    GO_LIVE_FIELDS = ("status", "rejection_reason", "published_at", "bumped_at", "tier", "tier_until", "expires_at",
                      "pending_tier", "pending_days", "free_listing", "top_until", "slider_until", "updated_at")

    def _go_live(self):
        """Start what was bought before the ad went live (plan days, VIP extras). Does not save."""
        now = timezone.now()
        self.status = self.Status.ACTIVE
        self.rejection_reason = ""
        self.published_at = self.published_at or now
        self.bumped_at = now
        if self.pending_days:
            end = now + timedelta(days=self.pending_days)
            running = self.tier_until and self.tier_until > now and self.tier != Tier.FREE
            if self.pending_tier and self.pending_tier != Tier.FREE:
                if running and self.tier == self.pending_tier:
                    end = self.tier_until + timedelta(days=self.pending_days)  # same plan: add the days
                    self.tier_until = end
                elif running and TIER_RANK[self.pending_tier] < TIER_RANK[self.tier]:
                    end = max(self.tier_until, end)  # never downgrade a running higher plan; the ad stays online longer
                else:
                    self.tier, self.tier_until = self.pending_tier, end
                self.grant_plan_extras()
            self.expires_at = max(self.expires_at or now, end)
            self.pending_tier, self.pending_days = "", None
        elif not self.expires_at or self.expires_at < now:
            # No plan chosen (e.g. approved from the admin): the free period.
            self.expires_at = now + timedelta(days=SiteSettings.load().free_ad_days)
            self.free_listing = True

    def _start_waiting_addons(self):
        for order in self.orders.filter(status="paid", addons_applied=False).exclude(addons=[]):
            order.apply_addons()

    def approve(self):
        """Go live. A plan chosen before approval (free or paid) starts now, so no days are lost in review."""
        self._go_live()
        self.save()
        self._start_waiting_addons()

    @property
    def effective_tier(self):
        """Paid tiers only count while they are paid for."""
        if self.tier != Tier.FREE and self.tier_until and self.tier_until > timezone.now():
            return self.tier
        return Tier.FREE

    # Display names come from the admin-edited lookup tables (core/taxonomy.py).
    def _label(self, kind, value):
        from .taxonomy import label
        return label(kind, value)

    region_label = property(lambda self: self._label("region", self.region))
    category_label = property(lambda self: self._label("category", self.category))
    gender_label = property(lambda self: self._label("gender", self.gender))
    accessibility_label = property(lambda self: self._label("accessibility", self.accessibility))
    body_type_label = property(lambda self: self._label("body_type", self.body_type))
    eye_color_label = property(lambda self: self._label("eye_color", self.eye_color))
    hair_color_label = property(lambda self: self._label("hair_color", self.hair_color))
    bust_size_label = property(lambda self: self._label("bust_size", self.bust_size))

    @property
    def country(self):
        from .taxonomy import lists
        return lists()["labels"]["region_country"].get(self.region, "")

    @property
    def is_featured(self):
        """Bought "First in search" or "Front page ad" and it is still running: cards show a Featured mark."""
        now = timezone.now()
        return any(t and t > now for t in (self.top_until, self.slider_until))

    @property
    def card_frame(self):
        """Every card has a frame matching its plan: free, standard, premium or vip (paid ones while paid)."""
        return self.effective_tier

    @property
    def is_live(self):
        return self.status == self.Status.ACTIVE and (
            self.expires_at is None or self.expires_at > timezone.now()
        )

    def __str__(self):
        return self.title


def picture_upload_to(instance, filename):
    return f"ads/{instance.ad_id}/{uuid.uuid4().hex}.webp"


class AdPicture(models.Model):
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="pictures")
    image = models.ImageField(upload_to=picture_upload_to)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    position = models.PositiveSmallIntegerField(default=0)
    caption = models.CharField(max_length=120, blank=True)
    heart_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]


def video_upload_to(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp4"
    return f"ads/{instance.ad_id}/videos/{uuid.uuid4().hex}.{ext}"


class AdVideo(models.Model):
    """A short video (up to 30 seconds) on an ad. Only VIP visitors can watch it."""

    MAX_SECONDS = 30
    MAX_PER_AD = 3

    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="videos")
    file = models.FileField(upload_to=video_upload_to)
    content_type = models.CharField(max_length=40, default="video/mp4")
    duration = models.FloatField(default=0, help_text="Seconds")
    size = models.PositiveIntegerField(default=0)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "ad video"

    def __str__(self):
        return f"Video {self.pk} ({self.duration:.0f}s) – {self.ad}"


class AdReview(models.Model):
    """A star rating and short review of an ad by a VIP visitor (one per visitor per ad)."""

    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField(help_text="1 to 5 stars")
    body = models.TextField(max_length=1000, blank=True)
    is_hidden = models.BooleanField(default=False, help_text="Hidden by a moderator")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["ad", "user"], name="one_review_per_user_ad")]

    def __str__(self):
        return f"{self.rating}★ by {self.user} on {self.ad}"


class Heart(models.Model):
    """A 'like' from a signed-in user on an ad."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hearts")
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="hearts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "ad"], name="one_heart_per_user_ad")]


class PictureHeart(models.Model):
    """A 'like' from a signed-in user on one picture."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="picture_hearts")
    picture = models.ForeignKey(AdPicture, on_delete=models.CASCADE, related_name="hearts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "picture"], name="one_heart_per_user_picture")]


class Report(models.Model):
    class Reason(models.TextChoices):
        SCAM = "scam", "I have been scammed"
        FAKE = "fake", "Fake ad or profile – pretending to be someone else"
        STOLEN_PHOTOS = "stolen-photos", "My pictures or details are used without permission"
        ILLEGAL = "illegal", "Illegal content"
        OFFENSIVE = "offensive", "Offensive or abusive content, e.g. racist"
        UNDERAGE = "underage", "Minor (under 18)"
        COERCION = "coercion", "Human trafficking or coercion"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFT = "draft", "Waiting for e-mail confirmation"
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    reason = models.CharField(max_length=20, choices=Reason.choices)
    message = models.TextField(max_length=2000, blank=True)
    reporter_name = models.CharField(max_length=80, blank=True)
    contact_email = models.EmailField(blank=True)
    picture_ids = models.JSONField(default=list, blank=True, help_text="Pictures on the ad the report is about")
    good_faith = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_urgent(self):
        return self.reason in {self.Reason.UNDERAGE, self.Reason.COERCION}


# ---------------------------------------------------------------------------
# Content (articles, blog, dictionary, static pages)
# ---------------------------------------------------------------------------


class FaqItem(models.Model):
    """One question on the FAQ page (Admin → FAQ). Answers are Markdown."""

    class Category(models.TextChoices):
        GENERAL = "general", "General"
        ACCOUNT = "account", "Account"
        ADS = "ads", "Ads"
        PAYMENTS = "payments", "Payments & coins"
        SAFETY = "safety", "Safety & privacy"

    category = models.CharField(max_length=20, choices=Category.choices, default=Category.GENERAL)
    question = models.CharField(max_length=200)
    answer = models.TextField(help_text="Markdown")
    order = models.PositiveSmallIntegerField(default=0)
    is_published = models.BooleanField(default=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "FAQ item"

    def __str__(self):
        return self.question


class Topic(models.Model):
    class Kind(models.TextChoices):
        ARTICLE = "article", "Article"
        BLOG = "blog", "Blog post"
        DICTIONARY = "dictionary", "Dictionary entry"
        PAGE = "page", "Static page"

    kind = models.CharField(max_length=12, choices=Kind.choices)
    slug = models.SlugField(max_length=100)
    title = models.CharField(max_length=140)
    summary = models.CharField(max_length=300, blank=True)
    aliases = models.CharField(
        max_length=300, blank=True,
        help_text="Dictionary only: other words for this entry, comma-separated. Each is listed and links here.",
    )
    body = models.TextField(help_text="Markdown")
    image = models.ImageField(upload_to="topics/", blank=True, help_text="Cover image (articles and blog posts)")
    document = models.FileField(
        upload_to="pages/", blank=True,
        validators=[FileExtensionValidator(["pdf"])],
        help_text="Optional PDF (e.g. the full Terms). Shown on the page with View and Download buttons.",
    )
    heart_count = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    show_in_menu = models.BooleanField(default=False)
    menu_order = models.PositiveSmallIntegerField(default=0)
    published_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["menu_order", "-published_at"]
        constraints = [models.UniqueConstraint(fields=["kind", "slug"], name="unique_topic_slug")]

    def alias_list(self):
        return [a.strip() for a in self.aliases.split(",") if a.strip()]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.title}"


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class SiteSettings(models.Model):
    """One editable record for site-wide branding and contact details (Admin → Site settings)."""

    site_name = models.CharField(max_length=60, default="EscortArea")
    tagline = models.CharField(max_length=80, default="your trusted guide", help_text="Small text above the logo")
    meta_title = models.CharField(
        max_length=120, blank=True, help_text="Browser/Google title for the front page. Defaults to the site name."
    )
    meta_description = models.CharField(
        max_length=300, blank=True,
        default="Browse verified 18+ listings for massage, wellness and companionship. Every advertiser is age-verified.",
    )
    logo = models.ImageField(upload_to="site/", blank=True, help_text="Shown in the header instead of the default mark")
    favicon = models.ImageField(upload_to="site/", blank=True, help_text="Square PNG/ICO, at least 64×64")
    accent_color = models.CharField(max_length=7, default="#ff4fd7", help_text="Main brand colour, e.g. #ff4fd7")
    accent_color_2 = models.CharField(max_length=7, default="#9333ea", help_text="Second brand colour for gradients")
    contact_email = models.EmailField(default="support@example.com", help_text="Support address: shown in the footer, About and Contact pages, receives the contact form, and is where replies to site e-mails go. E-mails are sent from DEFAULT_FROM_EMAIL (e.g. info@) in .env")
    country = models.CharField(max_length=40, default="Nepal")
    copyright_name = models.CharField(max_length=80, blank=True, help_text="Defaults to the site name")
    facebook_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    x_url = models.URLField(blank=True, verbose_name="X (Twitter) URL")
    about_intro = models.TextField(
        blank=True,
        default="EscortArea is where independent, age-verified adults advertise massage, wellness and companionship – "
        "and where visitors find them safely.",
        help_text="Intro text on the About page",
    )
    support_hours = models.CharField(max_length=120, blank=True, default="Mon–Fri 9–17")
    free_ad_days = models.PositiveSmallIntegerField(
        default=7, help_text="Days a Free ad is live (one Free ad per advertiser at a time)."
    )
    review_ads = models.BooleanField(
        default=False,
        help_text="Off: ads go live as soon as they are activated (and stay live when edited). "
                  "On: a moderator must approve new and edited ads first.",
    )
    slider_slots = models.PositiveSmallIntegerField(
        default=10, help_text="How many ads can be in the front-page slider at the same time (the “Front page ad” add-on)."
    )
    CURRENCIES = [("USD", "US dollar ($)"), ("EUR", "Euro (€)"), ("GBP", "British pound (£)"), ("DKK", "Danish krone (kr.)"),
                  ("SEK", "Swedish krona (kr)"), ("NOK", "Norwegian krone (kr)")]
    currency = models.CharField(max_length=3, choices=CURRENCIES, default="USD",
                                help_text="All prices are shown and charged in this currency (plans, coin packages, payments).")
    coin_value = models.PositiveIntegerField(
        default=100, help_text="What 1 coin is worth, in cents (100 = 1 coin is 1.00 in the currency above). "
                               "Coin prices of plans and add-ons, and the 5 % coin cashback, use this value.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        self.coin_value = max(1, self.coin_value or 100)
        super().save(*args, **kwargs)
        # One currency for the whole site: plans and coin packages follow it.
        Product.objects.exclude(currency=self.currency).update(currency=self.currency)
        AddOn.objects.exclude(currency=self.currency).update(currency=self.currency)

    def delete(self, *args, **kwargs):
        pass  # never delete the settings

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Site settings"


class Duration(models.IntegerChoices):
    DAY = 1, "1 day"
    WEEK = 7, "1 week"
    MONTH = 30, "1 month"
    QUARTER = 90, "3 months"
    HALF_YEAR = 180, "6 months"
    YEAR = 365, "1 year"


class Plan(models.Model):
    """A visibility level (Free, Standard, Premium, VIP) – priced per duration. Free prices are 0."""

    tier = models.CharField(max_length=10, choices=Tier.choices, unique=True)
    name = models.CharField(max_length=40)
    description = models.CharField(max_length=200, blank=True)
    features = models.TextField(blank=True, help_text="One feature per line, shown as a checklist")
    is_highlighted = models.BooleanField(default=False, help_text="Mark as 'Most popular'")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def feature_list(self):
        return [f.strip() for f in self.features.splitlines() if f.strip()]

    def __str__(self):
        return self.name


class Product(models.Model):
    """One price: a plan for a duration (e.g. Premium – 1 month). Bought per ad."""

    class Kind(models.TextChoices):
        PLAN = "plan", "Plan"
        DIAMONDS = "diamonds", "Coin package"
        MEMBERSHIP = "membership", "Visitor VIP membership"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PLAN)
    diamonds = models.PositiveIntegerField("coins", default=0, help_text="Coin packages: coins credited")
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, null=True, blank=True, related_name="prices")
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.FREE)
    days = models.PositiveSmallIntegerField(default=30, choices=Duration.choices)
    price = models.PositiveIntegerField(help_text="Price in øre/cents incl. VAT (e.g. 19900 = 199.00)")
    currency = models.CharField(max_length=3, default="USD", help_text="Follows Site settings → Currency")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "days", "price"]

    def save(self, *args, **kwargs):
        if self.plan_id:
            self.tier = self.plan.tier
            if not self.name:
                self.name = f"{self.plan.name} – {self.get_days_display()}"
            if not self.code:
                self.code = f"{self.plan.tier}-{self.days}d"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.price / 100:.2f} {self.currency})"


class AddOnGroup(models.Model):
    """An add-on on the payment page, e.g. "First in search". Its periods and prices are the options below."""

    class Effect(models.TextChoices):
        TOP = "top", "First in search (top of the lists)"
        SLIDER = "slider", "Front page ad (front-page slider)"
        BUMP = "bump", "Bump to the top (one time)"

    name = models.CharField(max_length=60)
    badge = models.CharField(max_length=20, blank=True, default="Featured", help_text="Small label next to the name (optional)")
    description = models.CharField(max_length=200, blank=True)
    effect = models.CharField(max_length=12, choices=Effect.choices)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "add-on"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        for option in self.options.all():
            option.save()  # keep the copied name/effect in step


class AddOn(models.Model):
    """One period of an add-on with its price (e.g. First in search – 7 days – 49.00)."""

    Effect = AddOnGroup.Effect

    group = models.ForeignKey(AddOnGroup, on_delete=models.CASCADE, related_name="options")
    code = models.SlugField(max_length=40, unique=True, blank=True)
    days = models.PositiveSmallIntegerField(default=7, help_text="How long it lasts. Bump: one time (0).")
    price = models.PositiveIntegerField(help_text="Price in øre/cents incl. VAT (e.g. 4900 = 49.00)")
    is_active = models.BooleanField(default=True)
    # Copied from the group when saved (orders keep a snapshot of these).
    name = models.CharField(max_length=80, editable=False)
    effect = models.CharField(max_length=12, choices=Effect.choices, editable=False)
    currency = models.CharField(max_length=3, default="USD", editable=False)

    class Meta:
        ordering = ["group__sort_order", "days", "price"]
        verbose_name = "period and price"
        verbose_name_plural = "periods and prices"

    @property
    def period_label(self):
        return "One time" if self.days == 0 else f"{self.days} day" if self.days == 1 else f"{self.days} days"

    def save(self, *args, **kwargs):
        self.effect = self.group.effect
        if self.effect == self.Effect.BUMP:
            self.days = 0
        self.name = self.group.name if self.days == 0 else f"{self.group.name} – {self.period_label}"
        if not self.code:
            base = f"{self.group.effect}-{self.days}d"
            self.code = base if not AddOn.objects.filter(code=base).exclude(pk=self.pk).exists() else f"{base}-{self.group_id}"
        self.currency = SiteSettings.load().currency
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.price / 100:.2f} {self.currency})"


def new_order_id():
    # Our own order reference (sent to the gateway as metadata).
    return "VL" + uuid.uuid4().hex[:14].upper()


class Order(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Waiting for payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    order_id = models.CharField(max_length=20, unique=True, default=new_order_id, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    ad = models.ForeignKey(Ad, on_delete=models.SET_NULL, null=True, related_name="orders")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="orders")
    # Snapshot of what was bought, so later product edits don't change history.
    product_name = models.CharField(max_length=80)
    tier = models.CharField(max_length=10, choices=Tier.choices)
    days = models.PositiveSmallIntegerField()
    diamonds = models.PositiveIntegerField(default=0)
    offer = models.ForeignKey("Offer", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    addons = models.JSONField(default=list, blank=True, help_text="Add-ons bought with the plan: [{code, name, effect, days, price}]")
    addons_applied = models.BooleanField(default=False)
    list_amount = models.PositiveIntegerField(default=0, help_text="Price before offer discount")
    amount = models.PositiveIntegerField(help_text="Minor units (øre/cents)")
    currency = models.CharField(max_length=3)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    gateway = models.CharField(max_length=20, default="paymentwings")
    gateway_payment_id = models.CharField(max_length=40, blank=True, db_index=True)
    payment_url = models.URLField(max_length=500, blank=True)
    card_brand = models.CharField(max_length=30, blank=True)
    last_gateway_state = models.JSONField(default=dict, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.order_id} – {self.product_name} – {self.get_status_display()}"

    @property
    def addons_total(self):
        return sum(a["price"] for a in self.addons)

    def apply_addons(self):
        """Switch on the add-ons on the ad. Waits (does nothing) until the ad is live, so no days are lost in review."""
        ad = self.ad
        if self.addons_applied or not self.addons or ad is None or ad.status != Ad.Status.ACTIVE:
            return
        now = timezone.now()
        fields = {"top": "top_until", "slider": "slider_until"}
        for a in self.addons:
            if a["effect"] == AddOn.Effect.BUMP:
                ad.bumped_at = now
            elif a["effect"] in fields:
                field = fields[a["effect"]]
                current = getattr(ad, field)
                end = (current if current and current > now else now) + timedelta(days=a["days"])
                setattr(ad, field, end)
        ad.save(update_fields=["bumped_at", *fields.values(), "updated_at"])
        self.addons_applied = True
        self.save(update_fields=["addons_applied", "updated_at"])


class TopicHeart(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="topic_hearts")
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="hearts")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "topic"], name="one_heart_per_user_topic")]


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class PrivateStorage(FileSystemStorage):
    """Files with no public URL, stored outside MEDIA_ROOT (location follows settings)."""

    @property
    def base_location(self):
        return settings.PRIVATE_MEDIA_ROOT

    @property
    def location(self):
        import os
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError("Private files have no public URL.")


_private_storage = PrivateStorage()


def private_storage():
    return _private_storage


class Conversation(models.Model):
    """One thread between a visitor and an advertiser about one ad."""

    ad = models.ForeignKey(Ad, on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations")
    ad_title = models.CharField(max_length=80, help_text="Kept if the ad is deleted")
    visitor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations_started")
    advertiser = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations_received")
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(default=timezone.now, db_index=True)
    visitor_read_at = models.DateTimeField(null=True, blank=True)
    advertiser_read_at = models.DateTimeField(null=True, blank=True)
    visitor_hidden = models.BooleanField(default=False)
    advertiser_hidden = models.BooleanField(default=False)
    blocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-last_message_at"]
        constraints = [models.UniqueConstraint(fields=["ad", "visitor"], name="one_conversation_per_ad_visitor")]

    def role_of(self, user):
        if user.pk == self.visitor_id:
            return "visitor"
        if user.pk == self.advertiser_id:
            return "advertiser"
        return None

    def other(self, user):
        return self.advertiser if user.pk == self.visitor_id else self.visitor

    def __str__(self):
        return f"{self.ad_title} ({self.visitor} ↔ {self.advertiser})"


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="messages_sent")
    body = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reply_to = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True, help_text="Unsent by the sender (removed for everyone)")
    # "Remove for you": hidden only for that side of the conversation.
    hidden_for_visitor = models.BooleanField(default=False)
    hidden_for_advertiser = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at", "id"]


def chat_upload_to(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"chat/{instance.message.conversation_id}/{uuid.uuid4().hex}.{ext}"


class MessageAttachment(models.Model):
    """Photo or video in a message. Stored privately; only the two participants can open it."""

    class Kind(models.TextChoices):
        IMAGE = "image", "Photo"
        VIDEO = "video", "Video"

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    file = models.FileField(upload_to=chat_upload_to, storage=private_storage, max_length=200)
    content_type = models.CharField(max_length=60)
    size = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class MessageReaction(models.Model):
    """One emoji per person per message (like Messenger)."""

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    emoji = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["message", "user"], name="one_reaction_per_user")]


# ---------------------------------------------------------------------------
# Coins (stored as 'diamonds') and offers
# ---------------------------------------------------------------------------


class DiamondTransaction(models.Model):
    """Ledger of every change to a user's coin balance."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="diamond_log")
    amount = models.IntegerField(help_text="Positive = received, negative = spent")
    reason = models.CharField(max_length=120)
    order = models.ForeignKey("Order", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "coin transaction"
        verbose_name_plural = "coin ledger"


class Offer(models.Model):
    """A time-limited discount, applied automatically at checkout."""

    class AppliesTo(models.TextChoices):
        ALL = "all", "Everything"
        PLANS = "plans", "Plans"
        DIAMONDS = "diamonds", "Coin packages"

    title = models.CharField(max_length=80)
    text = models.CharField(max_length=200, blank=True)
    discount_percent = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(90)])
    applies_to = models.CharField(max_length=10, choices=AppliesTo.choices, default=AppliesTo.ALL)
    tier = models.CharField(max_length=10, choices=Tier.choices, blank=True, help_text="Only this plan (optional)")
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    image = models.ImageField(upload_to="offers/", blank=True, help_text="Banner photo (wide pictures look best, e.g. 1600×700)")
    button_label = models.CharField(max_length=30, blank=True, help_text="Optional, e.g. 'Get coins'")
    show_on_home = models.BooleanField(default=True, help_text="Show this offer as a banner on the home page")
    code = models.CharField(max_length=30, blank=True, help_text="Optional campaign code shown on the card (the discount is still applied automatically)")
    max_uses = models.PositiveIntegerField(default=0, help_text="Total purchases allowed with this offer (0 = unlimited). When reached it shows as 'Sold out'.")
    once_per_user = models.BooleanField(default=False, help_text="Each advertiser can use it once – afterwards it shows as 'Used' for them")
    no_end_date = models.BooleanField(default=False, help_text="Runs until further notice (the end date is ignored)")

    class Meta:
        ordering = ["ends_at"]

    def __str__(self):
        return f"{self.title} (-{self.discount_percent}%)"

    def save(self, *args, **kwargs):
        # Uploaded banners are re-encoded (EXIF/location data removed) like ad pictures.
        if self.image and not self.image._committed:
            from .images import process_upload
            content, _, _ = process_upload(self.image.file)
            self.image.save(content.name, content, save=False)
        super().save(*args, **kwargs)


class Favorite(models.Model):
    """Private bookmark ('Add to favourites') – separate from public likes."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorites")
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["user", "ad"], name="one_favorite_per_user_ad")]


class Comment(models.Model):
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField(max_length=1000)
    is_hidden = models.BooleanField(default=False, help_text="Hidden by a moderator")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class UserReport(models.Model):
    """A person reported from a chat (not an ad). Moderators see the chat it came from."""

    class Reason(models.TextChoices):
        SCAM = "scam", "Scam or asking for money"
        HARASSMENT = "harassment", "Harassment or threats"
        SPAM = "spam", "Spam"
        FAKE = "fake", "Fake profile – pretending to be someone else"
        UNDERAGE = "underage", "Minor (under 18)"
        COERCION = "coercion", "Human trafficking or coercion"
        OFFENSIVE = "offensive", "Offensive or abusive messages"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_reports_made")
    reported = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="user_reports")
    conversation = models.ForeignKey("Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="reports")
    reason = models.CharField(max_length=20, choices=Reason.choices)
    message = models.TextField(max_length=2000, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reported} – {self.get_reason_display()}"

    @property
    def is_urgent(self):
        return self.reason in {self.Reason.UNDERAGE, self.Reason.COERCION}


class ContactMessage(models.Model):
    """A message sent with the Contact us form. It is e-mailed to the address in Site settings."""

    class Topic(models.TextChoices):
        GENERAL = "general", "General question"
        ACCOUNT = "account", "My account"
        ADS = "ads", "My ad"
        PAYMENTS = "payments", "Payments and coins"
        SAFETY = "safety", "Safety or abuse"
        PRIVACY = "privacy", "Privacy / delete my data"
        BUSINESS = "business", "Business and press"

    topic = models.CharField(max_length=20, choices=Topic.choices, default=Topic.GENERAL)
    name = models.CharField(max_length=80)
    email = models.EmailField()
    message = models.TextField(max_length=4000)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    handled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_topic_display()} – {self.name}"


class ReportImage(models.Model):
    """Screenshot attached to a report (private – only moderators see it)."""

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="reports/")


# ---------------------------------------------------------------------------
# ID verification (documents are private and deleted once reviewed)
# ---------------------------------------------------------------------------


def verification_upload_to(instance, filename):
    return f"verification/{instance.user_id}/{uuid.uuid4().hex}.webp"


class IdVerification(models.Model):
    """An advertiser's ID check. Only staff see the files; they are deleted after the decision."""

    class DocumentType(models.TextChoices):
        PASSPORT = "passport", "Passport"
        ID_CARD = "id_card", "National ID card"
        DRIVING_LICENCE = "driving_licence", "Driving licence"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="verifications")
    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    full_name = models.CharField(max_length=120, help_text="Name as written on the document")
    date_of_birth = models.DateField()
    document_front = models.ImageField(storage=private_storage, upload_to=verification_upload_to, blank=True)
    document_back = models.ImageField(storage=private_storage, upload_to=verification_upload_to, blank=True)
    selfie = models.ImageField(storage=private_storage, upload_to=verification_upload_to, blank=True,
                               help_text="Selfie holding the document")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    reject_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} – {self.get_status_display()}"

    @property
    def files(self):
        return [f for f in (self.document_front, self.document_back, self.selfie) if f]

    def delete_files(self):
        for f in self.files:
            f.delete(save=False)

    def decide(self, approved: bool, by=None, reason: str = ""):
        self.status = self.Status.APPROVED if approved else self.Status.REJECTED
        self.reject_reason = "" if approved else (reason or "The documents could not be verified.")
        self.reviewed_at = timezone.now()
        self.reviewed_by = by
        self.delete_files()  # data minimisation: keep the decision, not the ID copies
        self.save()
        type(self.user).objects.filter(pk=self.user_id).update(age_verified=approved)


# ---------------------------------------------------------------------------
# Lookup lists edited in the admin (countries, regions, categories, options)
# ---------------------------------------------------------------------------


class Country(models.Model):
    class Continent(models.TextChoices):
        EUROPE = "EU", "Europe"
        ASIA = "AS", "Asia"
        AFRICA = "AF", "Africa"
        NORTH_AMERICA = "NA", "North America"
        SOUTH_AMERICA = "SA", "South America"
        OCEANIA = "OC", "Oceania"
        ANTARCTICA = "AN", "Antarctica"

    code = models.CharField(max_length=2, unique=True, help_text="ISO code, e.g. DK")
    name = models.CharField(max_length=60)
    dial_code = models.CharField(max_length=6, help_text="e.g. +45")
    flag = models.CharField(max_length=8, blank=True, help_text="Emoji flag, e.g. 🇩🇰")
    continent = models.CharField(max_length=2, choices=Continent.choices, blank=True,
                                 help_text="Groups the country on the map chooser on the home page")
    has_ads = models.BooleanField(default=False, help_text="Ads can be placed in this country's regions")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "countries"

    def __str__(self):
        return f"{self.flag} {self.name}".strip()

    def save(self, *args, **kwargs):
        self.code = self.code.upper()
        if not self.continent:
            from .continents import CONTINENT
            self.continent = CONTINENT.get(self.code, "")
        super().save(*args, **kwargs)


class AdRegion(models.Model):
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="regions")
    slug = models.SlugField(max_length=40, unique=True, help_text="Used in links – don't change once ads use it")
    name = models.CharField(max_length=60)
    geo_code = models.CharField(max_length=20, blank=True, db_index=True, editable=False,
                                help_text="GeoNames code (e.g. IN.07) for areas added by the world seed")
    lat = models.FloatField(null=True, blank=True, help_text="Map position (centre of the region), e.g. 55.676")
    lng = models.FloatField(null=True, blank=True, help_text="Map position (centre of the region), e.g. 12.568")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["country__sort_order", "sort_order", "name"]
        verbose_name = "region"

    def __str__(self):
        return self.name


class AdPlace(models.Model):
    """A neighbourhood or town inside an area, e.g. Lakeside in Pokhara. Advertisers pick several."""

    region = models.ForeignKey(AdRegion, on_delete=models.CASCADE, related_name="places")
    slug = models.SlugField(max_length=80, unique=True, blank=True, help_text="Filled in from the name")
    name = models.CharField(max_length=80)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["region__sort_order", "sort_order", "name"]
        verbose_name = "place"

    def __str__(self):
        return f"{self.name} ({self.region.name})"

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base = slugify(self.name) or "place"
            slug, n = base, 2
            while AdPlace.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{self.region.slug}" if n == 2 else f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)


class AdCategory(models.Model):
    slug = models.SlugField(max_length=40, unique=True, help_text="Used in links – don't change once ads use it")
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "category"
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class ChoiceOption(models.Model):
    """Small option lists: gender, who the ad welcomes, accessibility, body type, eye/hair colour, bust size."""

    class Group(models.TextChoices):
        GENDER = "gender", "Gender"
        AUDIENCE = "audience", "Offers to"
        ACCESSIBILITY = "accessibility", "Customers with disabilities"
        BODY_TYPE = "body_type", "Body type"
        EYE_COLOR = "eye_color", "Eye colour"
        HAIR_COLOR = "hair_color", "Hair colour"
        BUST_SIZE = "bust_size", "Bust size"
        SERVICE = "service", "Service (ticked in the ad editor)"

    group = models.CharField(max_length=20, choices=Group.choices, db_index=True)
    value = models.CharField(max_length=20, help_text="Stored on ads – don't change once used")
    label = models.CharField(max_length=60)
    show_in_menu = models.BooleanField(default=False, help_text="Gender only: show as a menu in the header (with all regions)")
    menu_label = models.CharField(max_length=60, blank=True, help_text="Gender only: name in the header menu, e.g. 'Women' (empty = label)")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["group", "sort_order", "label"]
        constraints = [models.UniqueConstraint(fields=["group", "value"], name="unique_option_per_group")]
        verbose_name = "option"

    def __str__(self):
        return f"{self.get_group_display()}: {self.label}"


class AdDailyView(models.Model):
    """Views per ad per day (a visitor counts at most once every 2 minutes; nothing about the visitor is stored)."""

    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, related_name="daily_views")
    day = models.DateField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["ad", "day"], name="one_daily_view_row")]
        ordering = ["day"]


# ---------------------------------------------------------------------------
# Notifications (core/notifications.py creates them; the header bell shows them live)
# ---------------------------------------------------------------------------


class Notification(models.Model):
    class Kind(models.TextChoices):
        AD_LIKED = "ad_liked", "Ad liked"
        PICTURE_LIKED = "picture_liked", "Picture liked"
        AD_FAVORITED = "ad_favorited", "Ad saved to favourites"
        AD_COMMENT = "ad_comment", "New comment"
        AD_APPROVED = "ad_approved", "Ad approved"
        AD_REJECTED = "ad_rejected", "Ad rejected"
        AD_PAUSED = "ad_paused", "Ad paused by a moderator"
        AD_EXPIRING = "ad_expiring", "Ad expires soon"
        AD_EXPIRED = "ad_expired", "Ad expired"
        PLAN_ENDING = "plan_ending", "Plan ends soon"
        PAYMENT = "payment", "Payment received"
        ID_APPROVED = "id_approved", "ID approved"
        ID_REJECTED = "id_rejected", "ID rejected"
        REPORT_REVIEWED = "report_reviewed", "Report reviewed"
        OFFER = "offer", "New offer"
        FAVORITE_GONE = "favorite_gone", "Favourite ad expired"
        ID_RECEIVED = "id_received", "ID documents received"
        VIP_STARTED = "vip_started", "VIP membership started"
        VIP_ENDING = "vip_ending", "VIP membership ends soon"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    ad = models.ForeignKey(Ad, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    actor_name = models.CharField(max_length=60, blank=True, help_text="Latest person who did it (likes, comments)")
    count = models.PositiveIntegerField(default=1, help_text="Similar unread events grouped into one (e.g. 3 likes)")
    text = models.CharField(max_length=300, blank=True, help_text="Extra detail, e.g. a rejection reason")
    link = models.CharField(max_length=200, blank=True)
    key = models.CharField(max_length=120, blank=True, help_text="Stops the same reminder being sent twice")
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self):
        return f"{self.user} – {self.get_kind_display()}"
