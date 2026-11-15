from django.contrib.auth import password_validation
from rest_framework import serializers

from .models import Ad, AddOn, AddOnGroup, AdPicture, Order, Plan, Product, Report, SiteSettings, Tag, Topic, User




class UserSerializer(serializers.ModelSerializer):
    customer_no = serializers.CharField(read_only=True)
    is_vip = serializers.BooleanField(read_only=True)
    profile_picture = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "email", "email_confirmed", "customer_no", "display_name", "phone_country", "phone",
            "phone_verified", "profile_picture", "sender_name", "language", "theme", "notify_messages",
            "notify_expiry", "notify_offers", "diamonds", "show_ad_history", "is_advertiser", "age_verified", "adult_confirmed_at", "is_staff",
            "date_joined", "is_vip", "vip_until",
        ]
        read_only_fields = [
            "id", "email", "email_confirmed", "customer_no", "phone_verified", "profile_picture", "diamonds",
            "age_verified", "adult_confirmed_at", "is_staff", "date_joined", "is_vip", "vip_until",
        ]

    def get_profile_picture(self, obj):
        return obj.profile_picture.url if obj.profile_picture else None

    def validate_is_advertiser(self, value):
        # Visitors become advertisers only by sending their ID documents (POST /account/verification).
        if value and self.instance is not None and not self.instance.is_advertiser:
            raise serializers.ValidationError("Send your ID documents to convert this account to an advertiser account.")
        return value

    def validate_phone_country(self, value):
        from .taxonomy import phone_countries
        if value not in phone_countries():
            raise serializers.ValidationError("Unsupported country.")
        return value

    def validate_phone(self, value):
        digits = "".join(ch for ch in value if ch.isdigit())
        if value and not 6 <= len(digits) <= 15:
            raise serializers.ValidationError("Enter a valid phone number.")
        return value.strip()

    def update(self, instance, validated_data):
        fields = list(validated_data)
        changed_phone = any(
            k in validated_data and validated_data[k] != getattr(instance, k) for k in ("phone", "phone_country")
        )
        if changed_phone:
            instance.phone_verified = False  # a new number must be verified again
            fields.append("phone_verified")
        for key, value in validated_data.items():
            setattr(instance, key, value)
        # Save only what changed, so two settings saved at the same time can't overwrite each other.
        instance.save(update_fields=fields or None)
        return instance


class EmailChangeSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("That e-mail address is already in use.")
        return value


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    repeat_password = serializers.CharField(write_only=True)

    def validate(self, data):
        if data["new_password"] != data["repeat_password"]:
            raise serializers.ValidationError({"repeat_password": "The passwords don't match."})
        return data


class SignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)
    display_name = serializers.CharField(max_length=60, required=False, allow_blank=True)
    is_advertiser = serializers.BooleanField(default=False)
    confirm_adult = serializers.BooleanField()
    accept_terms = serializers.BooleanField()

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, data):
        if not data.get("confirm_adult"):
            raise serializers.ValidationError({"confirm_adult": "You must be 18 or older."})
        if not data.get("accept_terms"):
            raise serializers.ValidationError({"accept_terms": "You must accept the terms."})
        password_validation.validate_password(data["password"], User(email=data["email"]))
        return data


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class TagSerializer(serializers.ModelSerializer):
    ad_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Tag
        fields = ["id", "name", "slug", "color", "ad_count"]


class PictureSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = AdPicture
        fields = ["id", "url", "width", "height", "position", "caption", "heart_count"]

    def get_url(self, obj):
        return obj.image.url if obj.image else None


class VideoSerializer(serializers.Serializer):
    """Ad video. The file is only given to VIP visitors, the owner and staff; others get `locked`."""

    def to_representation(self, v):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        open_ = bool(user and user.is_authenticated and (user.is_vip or user.pk == v.ad.owner_id))
        url = v.file.url if open_ else None
        if url and request is not None:
            url = request.build_absolute_uri(url) if not url.startswith("http") else url
        return {"id": v.id, "duration": round(v.duration, 1), "locked": not open_, "url": url,
                "content_type": v.content_type}


class AdCardSerializer(serializers.ModelSerializer):
    """Compact representation for grids and lists."""

    cover = serializers.SerializerMethodField()
    picture_count = serializers.SerializerMethodField()
    category_label = serializers.CharField(read_only=True)
    region_label = serializers.CharField(read_only=True)
    country = serializers.CharField(read_only=True)
    tier = serializers.CharField(source="effective_tier", read_only=True)
    gender_label = serializers.CharField(read_only=True)
    picture_urls = serializers.SerializerMethodField()
    picture_items = serializers.SerializerMethodField()
    excerpt = serializers.SerializerMethodField()
    frame = serializers.CharField(source="card_frame", read_only=True)
    featured = serializers.BooleanField(source="is_featured", read_only=True)

    class Meta:
        model = Ad
        fields = [
            "id", "slug", "title", "headline", "category", "category_label",
            "region", "region_label", "country", "city", "age", "gender", "gender_label", "tier",
            "heart_count", "view_count", "published_at", "cover", "picture_count",
            "picture_urls", "picture_items", "excerpt", "frame", "featured",
        ]

    def get_picture_urls(self, obj):
        return [p.image.url for p in obj.pictures.all()[:8] if p.image]

    def get_picture_items(self, obj):
        """Same pictures with their id and likes, so cards can like each picture on its own."""
        return [{"id": p.id, "url": p.image.url, "heart_count": p.heart_count} for p in obj.pictures.all()[:8] if p.image]

    def get_excerpt(self, obj):
        from .richtext import text_of
        text = " ".join((obj.headline + " " + text_of(obj.description)).split())
        return text[:180] + ("…" if len(text) > 180 else "")

    def get_cover(self, obj):
        pics = list(obj.pictures.all())
        return pics[0].image.url if pics else None

    def get_picture_count(self, obj):
        return len(obj.pictures.all())


class AdDetailSerializer(AdCardSerializer):
    body_type_label = serializers.CharField(read_only=True)
    eye_color_label = serializers.CharField(read_only=True)
    hair_color_label = serializers.CharField(read_only=True)
    bust_size_label = serializers.CharField(read_only=True)
    pictures = PictureSerializer(many=True, read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    phone = serializers.SerializerMethodField()
    category_labels = serializers.SerializerMethodField()
    region_labels = serializers.SerializerMethodField()
    place_labels = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    phone_dial = serializers.SerializerMethodField()
    accessibility_label = serializers.CharField(read_only=True)
    hearted = serializers.SerializerMethodField()
    advertiser = serializers.SerializerMethodField()
    videos = serializers.SerializerMethodField()
    rating = serializers.SerializerMethodField()

    class Meta(AdCardSerializer.Meta):
        fields = AdCardSerializer.Meta.fields + [
            "categories", "category_labels", "regions", "region_labels", "places", "place_labels", "location", "phone_dial",
            "description", "audiences", "languages", "phone", "show_call", "show_sms", "contact_email", "height_cm", "body_type", "body_type_label",
            "bust_size", "bust_size_label", "eye_color", "eye_color_label", "hair_color", "hair_color_label", "services", "contact_hours", "contact_note",
            "accessibility", "accessibility_label", "pictures", "tags", "hearted",
            "advertiser", "updated_at", "videos", "rating",
        ]

    def get_videos(self, obj):
        return VideoSerializer(obj.videos.all(), many=True, context=self.context).data

    def get_rating(self, obj):
        from django.db.models import Avg, Count
        r = obj.reviews.filter(is_hidden=False).aggregate(avg=Avg("rating"), n=Count("id"))
        return {"average": round(r["avg"], 1) if r["avg"] else None, "count": r["n"]}

    def get_phone(self, obj):
        return obj.phone if obj.show_phone else None

    def get_category_labels(self, obj):
        from .taxonomy import label
        return [label("category", v) for v in obj.categories or [obj.category]]

    def get_place_labels(self, obj):
        from .taxonomy import label
        return [label("place", v) for v in obj.places or []]

    def get_region_labels(self, obj):
        from .taxonomy import label
        return [label("region", v) for v in obj.regions or [obj.region]]

    def get_location(self, obj):
        """Where the ad is, as country → regions → cities (in the order the advertiser chose them).
        Cities the advertiser typed in themselves come under `other_places`."""
        from .models import AdPlace, AdRegion
        slugs = [r for r in (obj.regions or [obj.region]) if r]
        regions = {r.slug: r for r in AdRegion.objects.filter(slug__in=slugs).select_related("country")}
        listed = {p.slug: p for p in AdPlace.objects.filter(slug__in=obj.places or [])}
        countries = []
        for slug in slugs:
            r = regions.get(slug)
            if r is None:
                continue
            country = next((c for c in countries if c["code"] == r.country.code), None)
            if country is None:
                country = {"code": r.country.code, "name": r.country.name, "regions": []}
                countries.append(country)
            country["regions"].append({
                "value": r.slug, "label": r.name,
                "places": [{"value": p, "label": listed[p].name} for p in obj.places or [] if p in listed and listed[p].region_id == r.pk],
            })
        return {"countries": countries, "other_places": [p for p in obj.places or [] if p not in listed]}

    def get_phone_dial(self, obj):
        from .taxonomy import lists
        return next((c["dial_code"] for c in lists()["countries"] if c["code"] == obj.phone_country), "")

    def to_representation(self, obj):
        from .richtext import clean
        data = super().to_representation(obj)
        data["description"] = clean(data.get("description", ""))  # cleaned again on the way out
        return data

    def get_hearted(self, obj):
        user = self.context["request"].user
        return bool(user.is_authenticated and obj.hearts.filter(user=user).exists())

    def get_advertiser(self, obj):
        o = obj.owner
        return {"id": o.pk, "name": o.display_name or "Advertiser", "verified": o.age_verified,
                "member_since": o.date_joined, "live_ads": Ad.objects.live().filter(owner=o).count()}


class MyAdSerializer(serializers.ModelSerializer):
    """Owner's editable view of an ad."""

    pictures = PictureSerializer(many=True, read_only=True)
    videos = serializers.SerializerMethodField()

    def get_videos(self, obj):
        return VideoSerializer(obj.videos.all(), many=True, context=self.context).data
    tags = serializers.SlugRelatedField(slug_field="slug", queryset=Tag.objects.all(), many=True, required=False)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    tier = serializers.CharField(source="effective_tier", read_only=True)

    description = serializers.CharField(max_length=20000)
    missing_fields = serializers.SerializerMethodField()
    complete_percent = serializers.SerializerMethodField()
    views_30d = serializers.SerializerMethodField()
    is_live = serializers.BooleanField(read_only=True)
    category_labels = serializers.SerializerMethodField()
    paid_once = serializers.SerializerMethodField()

    def get_category_labels(self, obj):
        from .taxonomy import label
        return [label("category", v) for v in obj.categories or ([obj.category] if obj.category else [])]

    def get_paid_once(self, obj):
        """True once the ad has been live or paid for – before that My ads says "Payment missing"."""
        value = getattr(obj, "paid_once", None)
        if value is None:
            value = obj.orders.filter(status="paid").exists()
        return bool(value or obj.published_at)

    def get_missing_fields(self, obj):
        from .adstatus import missing_fields
        return missing_fields(obj)

    def get_complete_percent(self, obj):
        from .adstatus import complete_percent
        return complete_percent(obj, len(obj.pictures.all()))

    def get_views_30d(self, obj):
        value = getattr(obj, "views_30d", None)
        if value is None:
            from datetime import timedelta

            from django.db.models import Sum
            from django.utils import timezone
            value = obj.daily_views.filter(day__gte=timezone.localdate() - timedelta(days=29)).aggregate(n=Sum("count"))["n"]
        return value or 0

    class Meta:
        model = Ad
        fields = [
            "missing_fields", "complete_percent", "views_30d", "is_live", "category_labels", "paid_once", "pending_tier",
            "id", "slug", "title", "headline", "description", "category", "gender",
            "audiences", "region", "regions", "places", "categories", "city", "age", "languages", "phone_country", "phone", "show_phone", "show_call", "show_sms", "contact_email",
            "accessibility", "tags", "height_cm", "body_type", "bust_size", "bust_size_label", "eye_color", "eye_color_label", "hair_color", "hair_color_label", "services",
            "contact_hours", "contact_note", "status", "status_label", "rejection_reason",
            "tier", "tier_until", "published_at", "expires_at", "view_count", "heart_count",
            "pictures", "videos", "created_at", "updated_at",
        ]
        # Drafts may be saved before everything is chosen; activating checks the rest (MyAdViewSet.submit).
        extra_kwargs = {f: {"required": False} for f in ("category", "region", "gender", "age")}
        read_only_fields = [
            "id", "slug", "status", "status_label", "rejection_reason", "tier", "tier_until", "pending_tier",
            "published_at", "expires_at", "view_count", "heart_count", "pictures",
            "created_at", "updated_at",
        ]

    def validate_description(self, value):
        from .richtext import MAX_TEXT, clean, text_of
        value = clean(value)
        if not text_of(value).strip():
            raise serializers.ValidationError("Write a description.")
        if len(text_of(value)) > MAX_TEXT:
            raise serializers.ValidationError(f"At most {MAX_TEXT} characters.")
        return value

    def validate_audiences(self, value):
        from .taxonomy import is_valid
        old = set(getattr(self.instance, "audiences", None) or [])
        if not isinstance(value, list) or any(v not in old and not is_valid("audience", v) for v in value):
            raise serializers.ValidationError("Choose from the list.")
        return sorted(set(value))

    def validate_phone_country(self, value):
        from .taxonomy import phone_countries
        old = getattr(self.instance, "phone_country", None)
        if value != old and value not in phone_countries():
            raise serializers.ValidationError("Unsupported country.")
        return value

    def _many(self, attrs, single, many, kind, maximum):
        """Several categories/areas: the first one is the main one shown on cards."""
        from .taxonomy import is_valid
        if many in attrs:
            values = list(dict.fromkeys(str(v) for v in (attrs[many] or [])))
            old = set(getattr(self.instance, many, None) or [])
            bad = [v for v in values if v not in old and not is_valid(kind, v)]
            if bad:
                raise serializers.ValidationError({many: "Choose from the list."})
            if len(values) > maximum:
                raise serializers.ValidationError({many: f"Choose at most {maximum}."})
            attrs[many] = values
            attrs[single] = values[0] if values else ""  # drafts may be empty; activating checks it
        elif single in attrs and self.instance is not None:
            rest = [v for v in (self.instance.__dict__.get(many) or []) if v != attrs[single]]
            attrs[many] = [attrs[single], *rest]

    def _places(self, attrs):
        """Places: entries from the admin list (must belong to one of the ad's areas) and up to 5 places the
        advertiser typed in themselves (plain names, e.g. "Satmuhane"). The first one is shown as the town."""
        from .taxonomy import is_valid, label, place_regions
        if "places" not in attrs and "regions" not in attrs:
            return
        regions = set(attrs.get("regions", getattr(self.instance, "regions", None) or []))
        places = attrs.get("places", getattr(self.instance, "places", None) or [])
        old = set(getattr(self.instance, "places", None) or [])
        place_region = place_regions(" ".join(str(p).split())[:80] for p in places or [])
        values, typed = [], 0
        for raw in places or []:
            v = " ".join(str(raw).split())[:80]
            if not v or v in values:
                continue
            if v in place_region:
                if (v in old or is_valid("place", v)) and place_region[v] in regions:
                    values.append(v)  # places in areas removed from the ad go with them
            else:
                v = v[:40]
                if len(v) < 2 or not any(ch.isalpha() for ch in v):
                    raise serializers.ValidationError({"places": "Type a real place name."})
                typed += 1
                values.append(v)
        if typed > 5:
            raise serializers.ValidationError({"places": "Type at most 5 places of your own."})
        if len(values) > 20:
            raise serializers.ValidationError({"places": "Choose at most 20."})
        attrs["places"] = values
        if "places" in getattr(self, "initial_data", {}):
            attrs["city"] = label("place", values[0]) if values else ""

    def validate(self, attrs):
        """Lookup values must be active entries in the admin lists (an ad may keep a value that was later hidden)."""
        from .taxonomy import is_valid
        self._many(attrs, "category", "categories", "category", 5)
        self._many(attrs, "region", "regions", "region", 10)
        self._places(attrs)
        kinds = {"region": "region", "category": "category", "gender": "gender", "accessibility": "accessibility",
                 "body_type": "body_type", "eye_color": "eye_color", "hair_color": "hair_color", "bust_size": "bust_size"}
        errors = {}
        for field, kind in kinds.items():
            if field not in attrs or not attrs[field]:
                continue
            if self.instance is not None and getattr(self.instance, field) == attrs[field]:
                continue
            if not is_valid(kind, attrs[field]):
                errors[field] = "Choose from the list."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def validate_services(self, value):
        if not isinstance(value, list) or len(value) > 20:
            raise serializers.ValidationError("Up to 20 services.")
        clean = []
        for row in value:
            row = row if isinstance(row, dict) else {}
            name = str(row.get("name", "")).strip()[:60]
            # Line types: service (fixed price), from (from price), range (from – to), heading (no price).
            price_to = str(row.get("price_to", "")).strip()[:30]
            kind = row.get("kind") if row.get("kind") in {"service", "from", "range", "heading"} else ("range" if price_to else "service")
            if kind == "service" and price_to:
                kind = "range"  # older lines with a "to" price
            price = "" if kind == "heading" else str(row.get("price", "")).strip()[:30]
            if name:
                line = {"name": name, "price": price}
                if kind == "range" and price_to:
                    line["price_to"] = price_to
                if kind != "service":
                    line["kind"] = kind
                clean.append(line)
        return clean

    def validate_contact_hours(self, value):
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        if not isinstance(value, dict):
            raise serializers.ValidationError("Invalid contact hours.")
        return {d: str(value.get(d, "")).strip()[:40] for d in days if str(value.get(d, "")).strip()}

    def validate_age(self, value):
        if value is not None and value < 18:
            raise serializers.ValidationError("Advertisers must be at least 18.")
        return value

    def update(self, instance, validated_data):
        # With review on (Site settings), editing a live or rejected ad sends it back to review.
        if instance.status in {Ad.Status.ACTIVE, Ad.Status.REJECTED} and SiteSettings.load().review_ads:
            instance.status = Ad.Status.PENDING
        return super().update(instance, validated_data)


class ReportSerializer(serializers.ModelSerializer):
    picture_ids = serializers.ListField(child=serializers.IntegerField(), required=False)

    class Meta:
        model = Report
        fields = ["reason", "message", "reporter_name", "contact_email", "picture_ids", "good_faith"]
        extra_kwargs = {"reporter_name": {"required": True, "allow_blank": False},
                        "contact_email": {"required": True, "allow_blank": False}}

    def validate_message(self, value):
        if len(value.strip()) < 50:
            raise serializers.ValidationError("Please describe what happened in at least 50 characters.")
        return value.strip()

    def validate_good_faith(self, value):
        if not value:
            raise serializers.ValidationError("Please confirm the report is made in good faith.")
        return value


class TopicListSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = Topic
        fields = ["id", "kind", "slug", "title", "summary", "image", "heart_count", "published_at", "updated_at"]

    def get_image(self, obj):
        return obj.image.url if obj.image else None


class TopicSerializer(serializers.ModelSerializer):
    aliases = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()

    class Meta:
        model = Topic
        fields = ["id", "kind", "slug", "title", "summary", "aliases", "body", "image", "document", "heart_count",
                  "published_at", "updated_at"]

    document = serializers.SerializerMethodField()

    def get_image(self, obj):
        return obj.image.url if obj.image else None

    def get_document(self, obj):
        if not obj.document:
            return None
        try:
            size = obj.document.size
        except OSError:
            size = None
        return {"url": obj.document.url, "name": obj.document.name.rsplit("/", 1)[-1], "size": size}

    def get_aliases(self, obj):
        return obj.alias_list()


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["code", "name", "description", "tier", "days", "price", "currency"]


class OrderSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    ad_id = serializers.UUIDField(read_only=True)
    ad_title = serializers.CharField(source="ad.title", read_only=True, default=None)
    kind = serializers.CharField(source="product.kind", read_only=True)

    class Meta:
        model = Order
        fields = [
            "order_id", "ad_id", "ad_title", "product_name", "kind", "tier", "days", "amount",
            "currency", "status", "status_label", "gateway", "card_brand", "paid_at", "created_at",
        ]


class CheckoutSerializer(serializers.Serializer):
    product = serializers.SlugRelatedField(slug_field="code", queryset=Product.objects.filter(is_active=True))
    offer = serializers.CharField(required=False, allow_blank=True)


class PictureTileSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    ad_id = serializers.UUIDField(read_only=True)
    ad_title = serializers.CharField(source="ad.title", read_only=True)

    class Meta:
        model = AdPicture
        fields = ["id", "url", "width", "height", "heart_count", "ad_id", "ad_title", "created_at"]

    def get_url(self, obj):
        return obj.image.url if obj.image else None


class SiteSettingsSerializer(serializers.ModelSerializer):
    logo = serializers.SerializerMethodField()
    favicon = serializers.SerializerMethodField()

    class Meta:
        model = SiteSettings
        fields = [
            "site_name", "tagline", "meta_title", "meta_description", "logo", "favicon", "accent_color",
            "accent_color_2", "contact_email", "country", "copyright_name", "facebook_url", "instagram_url",
            "x_url", "about_intro", "support_hours", "free_ad_days", "currency", "coin_value", "updated_at",
        ]

    def get_logo(self, obj):
        return obj.logo.url if obj.logo else None

    def get_favicon(self, obj):
        return obj.favicon.url if obj.favicon else None


class PlanPriceSerializer(serializers.ModelSerializer):
    duration_label = serializers.CharField(source="get_days_display", read_only=True)
    price = serializers.SerializerMethodField()
    list_price = serializers.IntegerField(source="price", read_only=True)
    offer = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = ["code", "days", "duration_label", "diamonds", "price", "list_price", "offer", "currency"]

    def get_price(self, obj):
        from .economy import price_for
        return price_for(obj, _ctx_user(self), self.context.get("offer"))[0]

    def get_offer(self, obj):
        from .economy import price_for
        o = price_for(obj, _ctx_user(self), self.context.get("offer"))[1]
        return {"id": o.id, "title": o.title, "discount_percent": o.discount_percent, "ends_at": o.ends_at, "code": o.code} if o else None


class PlanSerializer(serializers.ModelSerializer):
    features = serializers.SerializerMethodField()
    prices = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = ["tier", "name", "description", "features", "is_highlighted", "prices"]

    def get_features(self, obj):
        return obj.feature_list()

    def get_prices(self, obj):
        return PlanPriceSerializer(obj.prices.filter(is_active=True).order_by("days"), many=True, context=self.context).data


def _ctx_user(ser):
    req = ser.context.get("request")
    return getattr(req, "user", None)


class OfferSerializer(serializers.ModelSerializer):
    applies_to_label = serializers.CharField(source="get_applies_to_display", read_only=True)

    class Meta:
        from .models import Offer
        model = Offer
        fields = ["id", "title", "text", "discount_percent", "applies_to", "applies_to_label", "tier", "starts_at", "ends_at",
                  "image", "button_label", "show_on_home", "no_end_date"]


class CommentSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    mine = serializers.SerializerMethodField()
    by_advertiser = serializers.SerializerMethodField()

    class Meta:
        from .models import Comment
        model = Comment
        fields = ["id", "body", "created_at", "author", "mine", "by_advertiser", "author_vip"]
        read_only_fields = ["id", "created_at", "author", "mine", "by_advertiser", "author_vip"]

    author_vip = serializers.SerializerMethodField()

    def get_author_vip(self, obj):
        return obj.user.is_vip and obj.user_id != obj.ad.owner_id

    def get_author(self, obj):
        return obj.user.display_name or f"Member {obj.user_id}"

    def get_mine(self, obj):
        u = self.context["request"].user
        return u.is_authenticated and obj.user_id == u.pk

    def get_by_advertiser(self, obj):
        return obj.user_id == obj.ad.owner_id

    def validate_body(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Write a comment.")
        return value


class AddOnOptionSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source="period_label", read_only=True)

    class Meta:
        model = AddOn
        fields = ["code", "days", "label", "price", "currency"]


class AddOnGroupSerializer(serializers.ModelSerializer):
    options = serializers.SerializerMethodField()

    class Meta:
        model = AddOnGroup
        fields = ["id", "name", "badge", "description", "effect", "options"]

    def get_options(self, obj):
        return AddOnOptionSerializer([o for o in obj.options.all() if o.is_active], many=True).data


class ReviewSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()
    mine = serializers.SerializerMethodField()

    class Meta:
        from .models import AdReview
        model = AdReview
        fields = ["id", "rating", "body", "created_at", "updated_at", "author", "mine"]
        read_only_fields = ["id", "created_at", "updated_at", "author", "mine"]

    def get_author(self, obj):
        return obj.user.display_name or f"Member {obj.user_id}"

    def get_mine(self, obj):
        u = self.context["request"].user
        return u.is_authenticated and obj.user_id == u.pk

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Choose 1 to 5 stars.")
        return value

    def validate_body(self, value):
        return value.strip()
