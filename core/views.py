import json
import struct

from django.conf import settings
from django.contrib.auth import (
    authenticate,
    login,
    logout,
    password_validation,
    update_session_auth_hash,
)
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.text import slugify
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import ensure_csrf_cookie
from django_filters import rest_framework as df
from rest_framework import generics, mixins, permissions, status, viewsets
from rest_framework.decorators import (
    action,
    api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from . import account, economy, payments, slider
from . import serializers as s
from .images import InvalidImage, process_upload
from .models import (
    Accessibility,
    Ad,
    AddOn,
    AddOnGroup,
    AdPicture,
    Heart,
    Order,
    PictureHeart,
    Plan,
    Product,
    Report,
    SiteSettings,
    Tag,
    Tier,
    Topic,
    TopicHeart,
)


def choices(enum):
    return [{"value": v, "label": label} for v, label in enum.choices]


# ---------------------------------------------------------------------------
# Site metadata
# ---------------------------------------------------------------------------


DEPLOYED_AT = timezone.now()


def public_cached(seconds: int = 60):
    """Cache a public, user-independent GET answer per URL (server cache + browser Cache-Control)."""
    import hashlib
    from functools import wraps

    from django.core.cache import cache

    def deco(fn):
        @wraps(fn)
        def wrapper(self, request, *args, **kwargs):
            key = "pub:" + hashlib.sha256(request.get_full_path().encode()).hexdigest()[:40]
            data = cache.get(key)
            if data is None:
                res = fn(self, request, *args, **kwargs)
                if res.status_code != 200:
                    return res
                data = res.data
                cache.set(key, data, seconds)
            res = Response(data)
            res["Cache-Control"] = f"public, max-age={seconds}"
            return res

        return wrapper

    return deco


@cache_control(public=True, max_age=60)  # browsers reuse it; admin edits show within 1 min
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def site(request):
    """Branding, contact details and build info (edited in Admin → Site settings)."""
    data = s.SiteSettingsSerializer(SiteSettings.load()).data
    data["version"] = settings.APP_VERSION
    data["deployed_at"] = DEPLOYED_AT
    return Response(data)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def plans(request):
    qs = Plan.objects.filter(is_active=True).prefetch_related("prices")  # prices include live offers
    ctx = {"request": request, "offer": _chosen_offer(request)}
    return Response(s.PlanSerializer(qs, many=True, context=ctx).data)


def _chosen_offer(request):
    """?offer=<id or code> on price lists: ignored when it can't be used (the check endpoint explains why)."""
    try:
        return economy.resolve_offer(request.user, request.query_params.get("offer"))
    except ValidationError:
        return None


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def meta_areas(request):
    """Areas and places of the given countries (?countries=DK,SE) for the ad editor."""
    from .models import AdRegion
    from .taxonomy import editor_areas
    codes = [c.strip() for c in request.query_params.get("countries", "").split(",") if c.strip()][:20]
    # ?regions=… adds the countries of an ad's saved areas (used when an existing ad is opened).
    slugs = [r.strip() for r in request.query_params.get("regions", "").split(",") if r.strip()][:20]
    if slugs:
        codes += list(AdRegion.objects.filter(slug__in=slugs).values_list("country__code", flat=True).distinct())
    return Response(editor_areas(codes) if codes else {"regions": [], "places": []})


@cache_control(public=True, max_age=300)  # browsers reuse it; admin edits show within 5 min
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def meta(request):
    from .taxonomy import lists as taxonomy_lists
    site_settings = SiteSettings.load()
    lists = taxonomy_lists()
    return Response(
        {
            "site_name": site_settings.site_name,
            "country": site_settings.country,
            "contact_email": site_settings.contact_email,
            "free_ad_days": site_settings.free_ad_days,
            "currency": site_settings.currency,
            "coin_value": site_settings.coin_value,
            "max_pictures": settings.MAX_PICTURES_PER_AD,
            # Edited in the admin: Countries, Regions, Categories and Options.
            "countries": lists["countries"],
            "regions": lists["regions"],
            "places": lists["places"],
            "categories": lists["categories"],
            "genders": lists["options"]["gender"],
            "audiences": lists["options"]["audience"],
            "accessibility": lists["options"]["accessibility"],
            "body_types": lists["options"]["body_type"],
            "eye_colors": lists["options"]["eye_color"],
            "hair_colors": lists["options"]["hair_color"],
            "bust_sizes": lists["options"]["bust_size"],
            "services": lists["options"]["service"],
            "report_reasons": choices(Report.Reason),
            "menu": s.TopicListSerializer(
                Topic.objects.filter(is_published=True, show_in_menu=True), many=True
            ).data,
        }
    )


# ---------------------------------------------------------------------------
# Auth (session cookies + CSRF; the frontend proxies /api so it's same-origin)
# ---------------------------------------------------------------------------


class AuthThrottle(ScopedRateThrottle):
    """Uses the view's throttle_scope ("auth")."""


class ReportThrottle(SimpleRateThrottle):
    scope = "report"

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


@method_decorator(ensure_csrf_cookie, name="get")
class MeView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        if not request.user.is_authenticated:
            return Response(None)
        from .membership import can_go_live
        from .messaging import unread_total
        from .notifications import unread_count

        from .economy import active_offers
        from .models import Favorite

        u = request.user
        # Everything the site needs once per visit rides along (badges, live flag, which hearts are filled,
        # offer count), so a page load makes one request here instead of seven.
        return Response({**s.UserSerializer(u).data, "live_updates": can_go_live(u),
                         "unread_messages": unread_total(u), "unread_notifications": unread_count(u),
                         "hearts": {
                             "ads": [str(i) for i in Ad.objects.live().filter(hearts__user=u).values_list("id", flat=True)],
                             "pictures": list(PictureHeart.objects.filter(user=u).values_list("picture_id", flat=True)),
                             "topics": list(TopicHeart.objects.filter(user=u).values_list("topic_id", flat=True)),
                             "favorites": [str(i) for i in Favorite.objects.filter(user=u).values_list("ad_id", flat=True)],
                         },
                         "offer_count": active_offers().count()})

    def patch(self, request):
        if not request.user.is_authenticated:
            raise PermissionDenied()
        ser = s.UserSerializer(request.user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)


class SignupView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        ser = s.SignupSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        from .models import User

        user = User.objects.create_user(
            email=d["email"],
            password=d["password"],
            display_name=d.get("display_name", ""),
            is_advertiser=d["is_advertiser"],
            adult_confirmed_at=timezone.now(),
        )
        login(request, user)
        try:
            account.send_confirmation(user, welcome=True)
        except Exception:  # mail outage must not block sign-up; user can resend from Settings
            pass
        res = Response(s.UserSerializer(user).data, status=status.HTTP_201_CREATED)
        account.login_alert(request, res, user, quiet=True)
        return res


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        ser = s.LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=ser.validated_data["email"].lower(),
            password=ser.validated_data["password"],
        )
        if user is None:
            raise ValidationError({"detail": "Wrong email or password."})
        login(request, user)
        res = Response(s.UserSerializer(user).data)
        account.login_alert(request, res, user)
        return res


class ForgotPasswordView(APIView):
    """POST {email}: e-mail a reset link. Always answers the same, so it can't be used to find accounts."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        from .models import User
        email = str(request.data.get("email", "")).strip().lower()
        user = User.objects.filter(email__iexact=email, is_active=True).first() if email else None
        if user and user.has_usable_password():
            account.send_password_reset(user)
        return Response({"detail": "If an account uses that e-mail, we have sent a link to reset the password."})


class ResetPasswordView(APIView):
    """POST {uid, token} checks a link; POST {uid, token, new_password} sets the password and signs in."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        try:
            user = account.read_reset_link(str(request.data.get("uid", "")), str(request.data.get("token", "")))
        except account.InvalidToken as exc:
            raise ValidationError({"detail": str(exc)})
        new = request.data.get("new_password")
        if new is None:
            return Response({"valid": True, "email": user.email})
        try:
            password_validation.validate_password(str(new), user)
        except Exception as exc:  # django.core.exceptions.ValidationError
            raise ValidationError({"new_password": list(getattr(exc, "messages", [str(exc)]))})
        user.set_password(str(new))
        if not user.email_confirmed:
            user.email_confirmed = True  # they opened a link sent to this address
        user.save(update_fields=["password", "email_confirmed"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        account.send_password_changed(user)
        res = Response(s.UserSerializer(user).data)
        account.login_alert(request, res, user, quiet=True)
        return res


class ClaimAccountView(APIView):
    """Moving from escortside.dk (core/legacy.py).
    POST {token} -> what the page shows; {token, password, adult: true} claims and signs in; {token, delete: true}."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        from . import legacy

        try:
            acc = legacy.read_token(str(request.data.get("token", "")))
        except legacy.InvalidLink as exc:
            raise ValidationError({"detail": str(exc)})
        user = acc.user
        claimed = user.legacy_accounts.filter(claimed_at__isnull=False).exists()
        if request.data.get("delete"):
            if claimed:
                raise ValidationError({"detail": "This profile was already moved. Delete it from Settings instead."})
            user.delete()
            return Response({"deleted": True})
        password = request.data.get("password")
        if password is None:
            tier, days = legacy.reward_for(acc)
            return Response({
                "valid": True, "claimed": claimed, "email": user.email, "name": user.display_name or acc.legacy_username,
                "old_username": acc.legacy_username, "ads": user.ads.count(),
                "reward": {"tier": tier, "label": Tier(tier).label, "days": days},
                "steps": [{**st, "until": st["until"].isoformat()} for st in legacy.reward_deadlines(acc)],
            })
        if not request.data.get("adult"):
            raise ValidationError({"adult": ["Please confirm that you are 18 or older."]})
        try:
            password_validation.validate_password(str(password), user)
        except Exception as exc:  # django.core.exceptions.ValidationError
            raise ValidationError({"password": list(getattr(exc, "messages", [str(exc)]))})
        try:
            acc = legacy.claim(acc, str(password))
        except legacy.InvalidLink as exc:
            raise ValidationError({"detail": str(exc)})
        user.refresh_from_db()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        res = Response({**s.UserSerializer(user).data,
                        "reward": {"tier": acc.reward_tier, "label": Tier(acc.reward_tier or "free").label,
                                   "days": acc.reward_days}})
        account.login_alert(request, res, user, quiet=True)
        return res


class LogoutView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdultConfirmationView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        request.user.adult_confirmed_at = timezone.now()
        request.user.save(update_fields=["adult_confirmed_at"])
        return Response(s.UserSerializer(request.user).data)


# ---------------------------------------------------------------------------
# Public ads
# ---------------------------------------------------------------------------


class CommaInFilter(df.BaseInFilter, df.CharFilter):
    pass


class AdFilter(df.FilterSet):
    region = df.CharFilter(method="filter_regions")
    place = df.CharFilter(method="filter_places")
    country = df.CharFilter(method="filter_country")
    category = df.CharFilter(method="filter_categories")
    gender = CommaInFilter(field_name="gender")
    audience = df.CharFilter(method="filter_audience")
    accessible = df.BooleanFilter(method="filter_accessible")
    tag = df.CharFilter(field_name="tags__slug")
    q = df.CharFilter(method="filter_q")
    min_age = df.NumberFilter(field_name="age", lookup_expr="gte")
    max_age = df.NumberFilter(field_name="age", lookup_expr="lte")

    class Meta:
        model = Ad
        fields = []

    def filter_audience(self, qs, name, value):
        cond = Q()
        for v in [x.strip() for x in value.split(",") if x.strip()]:
            # JSON list stored as text; match the quoted value exactly.
            cond |= Q(audiences__icontains=f'"{v}"')
        return qs.filter(cond)

    @staticmethod
    def _in_list(qs, single, many, values):
        """Matches the main value or any value in the ad's list (JSON stored as text)."""
        cond = Q(**{f"{single}__in": values})
        for v in values:
            cond |= Q(**{f"{many}__icontains": f'"{v}"'})
        return qs.filter(cond)

    def filter_regions(self, qs, name, value):
        return self._in_list(qs, "region", "regions", [v.strip() for v in value.split(",") if v.strip()])

    def filter_places(self, qs, name, value):
        cond = Q()
        for v in [x.strip() for x in value.split(",") if x.strip()]:
            cond |= Q(places__icontains=f'"{v}"')
        return qs.filter(cond)

    def filter_categories(self, qs, name, value):
        return self._in_list(qs, "category", "categories", [v.strip() for v in value.split(",") if v.strip()])

    def filter_country(self, qs, name, value):
        from .taxonomy import regions_in_country
        return self._in_list(qs, "region", "regions", regions_in_country(value) or ["-"])

    def filter_accessible(self, qs, name, value):
        if value:
            return qs.filter(accessibility__in=[Accessibility.YES, Accessibility.PARTLY])
        return qs

    def filter_q(self, qs, name, value):
        return qs.filter(
            Q(title__icontains=value) | Q(headline__icontains=value)
            | Q(description__icontains=value) | Q(city__icontains=value)
        )


# Add-on effects a plan already includes (shown as "Included" on the payment page, refused at checkout).
INCLUDED_ADDONS = {"premium": {"bump"}, "vip": {"bump", "top", "slider"}}

ORDERINGS = {
    "recommended": ["-tier_rank", "-is_top", "-bumped_at"],
    "newest": ["-published_at"],
    "popular": ["-view_count"],
    "hearts": ["-heart_count"],
}


class AdPagination(PageNumberPagination):
    page_size = 24
    page_size_query_param = "page_size"
    max_page_size = 60


def count_view(request, ad):
    """+1 view (total and today's). The same visitor counts at most every 2 minutes; the owner's own views don't count.
    Only a short-lived hash is kept in the cache – nothing about visitors is stored."""
    import hashlib

    from django.core.cache import cache

    from .models import AdDailyView

    if request.user.is_authenticated and request.user.pk == ad.owner_id:
        return
    who = (f"u{request.user.pk}" if request.user.is_authenticated
           else request.META.get("REMOTE_ADDR", "") + request.META.get("HTTP_USER_AGENT", "")[:200])
    key = "view:" + hashlib.sha256(f"{ad.pk}|{who}".encode()).hexdigest()[:32]
    if not cache.add(key, 1, 120):
        return
    Ad.objects.filter(pk=ad.pk).update(view_count=F("view_count") + 1)
    today = timezone.localdate()
    row, created = AdDailyView.objects.get_or_create(ad_id=ad.pk, day=today, defaults={"count": 1})
    if not created:
        AdDailyView.objects.filter(pk=row.pk).update(count=F("count") + 1)


class AdViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [permissions.AllowAny]
    filterset_class = AdFilter
    pagination_class = AdPagination

    def get_queryset(self):
        from django.db.models import Case, IntegerField, Value, When

        qs = (
            Ad.objects.live()
            .select_related("owner")
            .prefetch_related("pictures", "tags")
            .annotate(
                tier_rank=Case(
                    When(tier="vip", tier_until__gt=timezone.now(), then=Value(3)),
                    When(tier="premium", tier_until__gt=timezone.now(), then=Value(2)),
                    When(tier="standard", tier_until__gt=timezone.now(), then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
                is_top=Case(When(top_until__gt=timezone.now(), then=Value(1)), default=Value(0), output_field=IntegerField()),
            )
        )
        order = ORDERINGS.get(self.request.query_params.get("sort", "recommended"), ORDERINGS["recommended"])
        return qs.order_by(*order).distinct()

    def get_serializer_class(self):
        return s.AdDetailSerializer if self.action == "retrieve" else s.AdCardSerializer

    def retrieve(self, request, *args, **kwargs):
        ad = self.get_object()
        count_view(request, ad)
        return Response(self.get_serializer(ad).data)

    @action(detail=True, url_path="picture-likes")
    def picture_likes(self, request, pk=None):
        """Current like count per picture – light, and does not count as a view."""
        ad = self.get_object()
        return Response({"pictures": [{"id": p.id, "heart_count": p.heart_count} for p in ad.pictures.all()]})

    @action(detail=False)
    def front(self, request):
        # ?country=DK limits the lists to the visitor's chosen country.
        qs = self.filter_queryset(self.get_queryset())
        data = {
            # Only ads with a running "Front page ad" (at most Site settings → Slider slots).
            "featured": s.AdCardSerializer(
                qs.filter(slider_until__gt=timezone.now()).order_by("-bumped_at")[: SiteSettings.load().slider_slots], many=True
            ).data,
            "slider_slots": slider.slots(),
            "newest": s.AdCardSerializer(qs.order_by("-published_at")[:12], many=True).data,
            "popular": s.AdCardSerializer(qs.order_by("-heart_count")[:8], many=True).data,
            "counts": {
                r["region"]: r["n"]
                for r in Ad.objects.live().values("region").annotate(n=Count("id"))
            },
        }
        return Response(data)

    @action(detail=False)
    @public_cached(120)
    def menu(self, request):
        """Header menus (Women, Men, Trans, Clinics …): live ads per area in one country, ?country=DK.
        {"gender": {"female": {"copenhagen": 120, …}}, "category": {"clinic": {…}}, "labels": {slug: name}}."""
        from collections import defaultdict

        from .models import AdRegion
        from .taxonomy import regions_in_country
        code = (request.query_params.get("country") or "").upper()[:2]
        areas = set(regions_in_country(code) or []) if code else set()
        by_gender: dict = defaultdict(lambda: defaultdict(int))
        by_category: dict = defaultdict(lambda: defaultdict(int))
        if areas:
            for gender, region, regions, categories in Ad.objects.live().values_list("gender", "region", "regions", "categories"):
                for r in set(regions or [region]) & areas:
                    if gender:
                        by_gender[gender][r] += 1
                    for c in categories or []:
                        by_category[c][r] += 1
        used = {r for d in (*by_gender.values(), *by_category.values()) for r in d}
        labels = dict(AdRegion.objects.filter(slug__in=used).values_list("slug", "name"))
        return Response({"country": code, "gender": {k: dict(v) for k, v in by_gender.items()},
                         "category": {k: dict(v) for k, v in by_category.items()}, "labels": labels})

    @action(detail=False)
    @public_cached(60)
    def map(self, request):
        """Ads as pins for the map: ?country=DK. Each ad sits near its region's centre (spread a little so
        pins don't stack), plus live-ad counts per country for the zoomed-out world view."""
        import hashlib

        from .models import AdRegion
        centres = {r.slug: (r.lat, r.lng) for r in AdRegion.objects.exclude(lat=None).exclude(lng=None)}
        pins = []
        if request.query_params.get("country"):
            qs = self.filter_queryset(self.get_queryset()).order_by("-bumped_at")[:300]
            new_since = timezone.now() - timezone.timedelta(days=2)
            for ad, card in zip(qs, s.AdCardSerializer(qs, many=True, context=self.get_serializer_context()).data):
                centre = centres.get(ad.region)
                if not centre:
                    continue
                h = hashlib.md5(str(ad.pk).encode()).digest()
                spread = 0.07 if ad.region != "bornholm" else 0.03
                pins.append({
                    **{k: card[k] for k in ("id", "title", "cover", "tier", "region_label", "featured", "age", "city", "category_label", "published_at")},
                    "region": ad.region,
                    "lat": centre[0] + (h[0] / 255 - 0.5) * spread,
                    "lng": centre[1] + (h[1] / 255 - 0.5) * spread * 1.8,
                    "is_new": bool(ad.published_at and ad.published_at > new_since),
                })
        counts = {}
        from .taxonomy import lists
        region_country = lists()["labels"]["region_country"]
        for r in Ad.objects.live().values("region").annotate(n=Count("id")):
            code = region_country.get(r["region"])
            if code:
                counts[code] = counts.get(code, 0) + r["n"]
        return Response({"pins": pins, "countries": counts})

    @action(detail=True, methods=["get", "post", "delete"], permission_classes=[permissions.IsAuthenticated])
    def favorite(self, request, pk=None):
        from .models import Favorite
        ad = self.get_object()
        if request.method == "POST":
            Favorite.objects.get_or_create(user=request.user, ad=ad)
        elif request.method == "DELETE":
            Favorite.objects.filter(user=request.user, ad=ad).delete()
        return Response({"favorite": Favorite.objects.filter(user=request.user, ad=ad).exists()})

    @action(detail=True, methods=["get", "post"], permission_classes=[permissions.IsAuthenticatedOrReadOnly],
            throttle_classes=[ReportThrottle])
    def comments(self, request, pk=None):
        from .models import Comment
        ad = self.get_object()
        if request.method == "POST":
            if request.user.pk != ad.owner_id:
                from .membership import require_vip
                require_vip(request.user, "comment")  # the advertiser can always answer on their own ad
            ser = s.CommentSerializer(data=request.data, context={"request": request})
            ser.is_valid(raise_exception=True)
            c = ser.save(ad=ad, user=request.user)
            return Response(s.CommentSerializer(c, context={"request": request}).data, status=status.HTTP_201_CREATED)
        qs = Comment.objects.filter(ad=ad, is_hidden=False).select_related("user", "ad")[:100]
        return Response(s.CommentSerializer(qs, many=True, context={"request": request}).data)

    @action(detail=True, url_path="videos")
    def ad_videos(self, request, pk=None):
        """The ad's videos for the signed-in visitor (files for VIPs only). Does not count a view."""
        ad = self.get_object()
        return Response({"videos": s.VideoSerializer(ad.videos.all(), many=True, context={"request": request}).data})

    @action(detail=True, methods=["get", "post"], permission_classes=[permissions.IsAuthenticatedOrReadOnly],
            throttle_classes=[ReportThrottle])
    def reviews(self, request, pk=None):
        """GET: reviews of the ad. POST {rating, body}: write or update your own review (VIP visitors)."""
        from .models import AdReview
        ad = self.get_object()
        if request.method == "POST":
            if request.user.pk == ad.owner_id:
                raise ValidationError({"detail": "You can't review your own ad."})
            from .membership import require_vip
            require_vip(request.user, "review")
            mine = AdReview.objects.filter(ad=ad, user=request.user).first()
            ser = s.ReviewSerializer(mine, data=request.data, context={"request": request})
            ser.is_valid(raise_exception=True)
            r = ser.save(ad=ad, user=request.user)
            return Response(s.ReviewSerializer(r, context={"request": request}).data,
                            status=status.HTTP_200_OK if mine else status.HTTP_201_CREATED)
        qs = AdReview.objects.filter(ad=ad, is_hidden=False).select_related("user")[:100]
        return Response(s.ReviewSerializer(qs, many=True, context={"request": request}).data)

    @action(detail=True)
    def similar(self, request, pk=None):
        ad = get_object_or_404(Ad, pk=pk)
        qs = self.get_queryset().exclude(pk=ad.pk).filter(Q(category=ad.category) | Q(region=ad.region))
        topics = [
            t for t in (
                Topic.objects.filter(kind=k, is_published=True).order_by("?").first() for k in ("article", "blog")
            ) if t
        ]
        return Response({
            "ads": s.AdCardSerializer(qs[:8], many=True).data,
            "topics": s.TopicListSerializer(topics, many=True).data,
        })

    @action(detail=True, methods=["get", "post", "delete"], permission_classes=[permissions.IsAuthenticated])
    def heart(self, request, pk=None):
        ad = self.get_object()
        if request.method == "GET":
            hearted = Heart.objects.filter(user=request.user, ad=ad).exists()
            return Response({"hearted": hearted, "heart_count": ad.heart_count})
        if request.method == "POST":
            try:
                with transaction.atomic():
                    Heart.objects.create(user=request.user, ad=ad)
                    Ad.objects.filter(pk=ad.pk).update(heart_count=F("heart_count") + 1)
            except IntegrityError:
                pass  # already hearted
        else:
            deleted, _ = Heart.objects.filter(user=request.user, ad=ad).delete()
            if deleted:
                Ad.objects.filter(pk=ad.pk, heart_count__gt=0).update(heart_count=F("heart_count") - 1)
        ad.refresh_from_db(fields=["heart_count"])
        return Response({"hearted": request.method == "POST", "heart_count": ad.heart_count})

    @action(
        detail=True, methods=["post"], permission_classes=[permissions.AllowAny],
        throttle_classes=[ReportThrottle], parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def report(self, request, pk=None):
        """Saved as a draft; the reporter confirms it from a link sent by e-mail."""
        from .models import ReportImage
        ad = get_object_or_404(Ad.objects.prefetch_related("pictures"), pk=pk)  # also non-live ads
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if hasattr(request.data, "getlist"):
            data.setlist("picture_ids", request.data.getlist("picture_ids"))
        ser = s.ReportSerializer(data=data)
        ser.is_valid(raise_exception=True)
        files = request.FILES.getlist("files")
        if len(files) > 5:
            raise ValidationError({"files": "At most 5 pictures."})
        valid_ids = {p.id for p in ad.pictures.all()}
        report = ser.save(
            ad=ad, status=Report.Status.DRAFT,
            reporter=request.user if request.user.is_authenticated else None,
            picture_ids=[i for i in ser.validated_data.get("picture_ids", []) if i in valid_ids],
        )
        for f in files:
            try:
                content, _, _ = process_upload(f)
            except InvalidImage as exc:
                report.delete()
                raise ValidationError({"files": f"{f.name}: {exc}"})
            img = ReportImage(report=report)
            img.image.save(content.name, content, save=True)
        account.send_report_confirmation(report)
        return Response({"ok": True, "detail": f"Check {report.contact_email} and confirm the report."},
                        status=status.HTTP_201_CREATED)


class NewPicturesView(APIView):
    """Newest pictures from live ads, honouring the same filters as /api/ads."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        ads = AdFilter(request.query_params, queryset=Ad.objects.live()).qs.distinct()
        try:
            size = max(1, min(int(request.query_params.get("page_size", 108)), 200))
        except ValueError:
            size = 108
        order = {
            "hearts": ["-heart_count", "-created_at"],
            "popular": ["-ad__view_count", "-created_at"],
        }.get(request.query_params.get("sort", ""), ["-created_at", "-id"])
        pics = AdPicture.objects.filter(ad__in=ads).select_related("ad").order_by(*order)[:size]
        return Response({
            "ad_count": ads.count(),
            "results": s.PictureTileSerializer(pics, many=True).data,
        })


class PictureHeartView(APIView):
    """GET current state, POST to give a heart, DELETE to remove it."""

    permission_classes = [permissions.IsAuthenticated]

    def get_picture(self, pk):
        return get_object_or_404(AdPicture.objects.filter(ad__in=Ad.objects.live()), pk=pk)

    def state(self, request, pic):
        pic.refresh_from_db(fields=["heart_count"])
        hearted = PictureHeart.objects.filter(user=request.user, picture=pic).exists()
        return Response({"hearted": hearted, "heart_count": pic.heart_count})

    def get(self, request, pk):
        return self.state(request, self.get_picture(pk))

    def post(self, request, pk):
        pic = self.get_picture(pk)
        try:
            with transaction.atomic():
                PictureHeart.objects.create(user=request.user, picture=pic)
                AdPicture.objects.filter(pk=pic.pk).update(heart_count=F("heart_count") + 1)
        except IntegrityError:
            pass
        return self.state(request, pic)

    def delete(self, request, pk):
        pic = self.get_picture(pk)
        deleted, _ = PictureHeart.objects.filter(user=request.user, picture=pic).delete()
        if deleted:
            AdPicture.objects.filter(pk=pic.pk, heart_count__gt=0).update(heart_count=F("heart_count") - 1)
        return self.state(request, pic)


class MyPictureHeartsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ids = PictureHeart.objects.filter(user=request.user).values_list("picture_id", flat=True)
        return Response({"picture_ids": list(ids)})


class MyHeartsView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = s.AdCardSerializer
    pagination_class = AdPagination

    def get_queryset(self):
        return (
            Ad.objects.live()
            .filter(hearts__user=self.request.user)
            .prefetch_related("pictures")
            .order_by("-hearts__created_at")
        )


# ---------------------------------------------------------------------------
# Advertiser: my ads
# ---------------------------------------------------------------------------


class IsAdvertiser(permissions.BasePermission):
    message = "Switch your account to an advertiser account to create ads."

    def has_permission(self, request, view):
        return bool(request.user.is_authenticated and request.user.is_advertiser)


class MyAdViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet,
):
    permission_classes = [IsAdvertiser]
    serializer_class = s.MyAdSerializer
    pagination_class = None

    def get_queryset(self):
        from datetime import timedelta

        from django.db.models import Sum
        since = timezone.localdate() - timedelta(days=29)
        views = Sum("daily_views__count", filter=Q(daily_views__day__gte=since))
        paid = Exists(Order.objects.filter(ad=OuterRef("pk"), status=Order.Status.PAID))
        return (Ad.objects.filter(owner=self.request.user).annotate(views_30d=views, paid_once=paid)
                .prefetch_related("pictures", "tags").order_by("-created_at"))

    def perform_destroy(self, instance):
        """Advertisers delete inactive ads themselves; a live ad must be paused first (or removed by support)."""
        if instance.is_live:
            raise PermissionDenied("A live ad can't be deleted. Pause it first, or contact us.")
        for pic in instance.pictures.all():
            pic.image.delete(save=False)
        instance.delete()

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """Copy an ad (text, details, pictures and tags) as a new draft."""
        from django.core.files.base import ContentFile
        src = self.get_object()
        copy = Ad.objects.get(pk=src.pk)
        tags = list(src.tags.all())
        copy.pk = None
        copy.id = None
        copy._state.adding = True
        copy.status = Ad.Status.DRAFT
        copy.title = (src.title + " (copy)")[:80]
        copy.published_at = copy.expires_at = copy.bumped_at = copy.tier_until = None
        copy.tier = Ad._meta.get_field("tier").default
        copy.view_count = copy.heart_count = 0
        copy.pending_tier, copy.pending_days, copy.free_listing = "", None, False  # a copy has no plan yet
        copy.rejection_reason = ""
        copy.save()
        copy.tags.set(tags)
        for pic in src.pictures.all():
            if not pic.image:
                continue
            with pic.image.open("rb") as fh:
                data = fh.read()
            new = AdPicture(ad=copy, width=pic.width, height=pic.height, position=pic.position, caption=pic.caption)
            new.image.save(pic.image.name.rsplit("/", 1)[-1], ContentFile(data), save=True)
        return self.fresh(copy, status.HTTP_201_CREATED)

    @action(detail=True)
    def visits(self, request, pk=None):
        """Views per day for the last N days, plus when the ad and its plans were running."""
        from datetime import timedelta

        from .models import AdDailyView, Order
        ad = self.get_object()
        days = max(7, min(int(request.query_params.get("days", 30) or 30), 90))
        today = timezone.localdate()
        start = today - timedelta(days=days - 1)
        counts = dict(AdDailyView.objects.filter(ad=ad, day__gte=start).values_list("day", "count"))
        series = [{"date": start + timedelta(days=i), "count": counts.get(start + timedelta(days=i), 0)} for i in range(days)]
        lanes = []
        if ad.published_at:
            lanes.append({"kind": "ad", "label": "Ad", "from": ad.published_at, "to": ad.expires_at})
        for o in Order.objects.filter(ad=ad, status=Order.Status.PAID, applied_at__isnull=False).exclude(tier=Ad._meta.get_field("tier").default):
            lanes.append({"kind": "plan", "tier": o.tier, "label": o.get_tier_display(), "from": o.applied_at, "to": o.applied_at + timedelta(days=o.days)})
        current = ad.effective_tier
        if current != Ad._meta.get_field("tier").default and ad.tier_until:
            # Days added with coins aren't orders: stretch the current plan's latest bar to its real end.
            same = [l for l in lanes if l["kind"] == "plan" and l["tier"] == current]
            if same and same[-1]["to"] < ad.tier_until:
                same[-1]["to"] = ad.tier_until
            elif not same:
                lanes.append({"kind": "plan", "tier": current, "label": ad.get_tier_display(), "from": ad.published_at, "to": ad.tier_until})
        return Response({"days": series, "total": sum(d["count"] for d in series), "lanes": lanes,
                         "created_today": ad.created_at.date() == today})

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user, status=Ad.Status.DRAFT)

    def fresh(self, ad, status_code=status.HTTP_200_OK):
        """Re-read the ad so prefetched pictures reflect changes made in this request."""
        return Response(self.get_serializer(self.get_queryset().get(pk=ad.pk)).data, status=status_code)

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        ad = self.get_object()
        if ad.status not in {Ad.Status.DRAFT, Ad.Status.REJECTED, Ad.Status.PAUSED}:
            raise ValidationError({"detail": f"Ad is already {ad.get_status_display().lower()}."})
        from . import activation
        activation.check_ready(ad)
        has_time = ad.pending_days or (ad.expires_at and ad.expires_at > timezone.now())
        if not has_time:
            # No plan yet: same as choosing Free (1 week, one Free ad at a time).
            free = Product.objects.filter(kind=Product.Kind.PLAN, tier="free", is_active=True).order_by("days").first()
            if free is None:
                raise ValidationError({"detail": "Choose a plan to activate the ad."})
            activation.activate(request.user, ad, free, "free")
            return self.fresh(ad)
        if not SiteSettings.load().review_ads:
            ad.approve()  # no review: live at once
            return self.fresh(ad)
        ad.status = Ad.Status.PENDING
        ad.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(ad).data)

    @action(detail=True, methods=["get", "post"])
    def activate(self, request, pk=None):
        """GET: what can be chosen (Free allowed?, coin balance). POST {product, method: card|coins}: activate."""
        from . import activation
        from .adstatus import missing_fields
        ad = self.get_object()
        if request.method == "GET":
            other = activation.running_free_ad(request.user, exclude=ad)
            request.user.refresh_from_db(fields=["diamonds"])
            return Response({
                "free_days": SiteSettings.load().free_ad_days,
                "free_allowed": other is None,
                "free_blocked_by": {"id": str(other.id), "title": other.title} if other else None,
                "coins": request.user.diamonds,
                "cashback_percent": economy.CASHBACK_PERCENT,
                "coin_value": economy.coin_value(),
                "missing_fields": missing_fields(ad),
                "is_live": ad.is_live,
                "expires_at": ad.expires_at,
                "pending": ad.status == Ad.Status.PENDING,
                "review": SiteSettings.load().review_ads,
                "addons": s.AddOnGroupSerializer(
                    AddOnGroup.objects.filter(is_active=True).prefetch_related("options"), many=True).data,
                "slider_slots": slider.slots(ad),
            })
        product = get_object_or_404(Product, code=request.data.get("product"), is_active=True)
        offer = economy.resolve_offer(request.user, request.data.get("offer"))
        codes = request.data.get("addons") or []
        if not isinstance(codes, list):
            raise ValidationError({"addons": "Send a list of add-on codes."})
        addons = list(AddOn.objects.filter(code__in=[str(c) for c in codes], is_active=True, group__is_active=True))
        included = INCLUDED_ADDONS.get(product.tier, set())
        clash = [a for a in addons if a.effect in included]
        if clash:
            raise ValidationError({"addons": f"{clash[0].group.name} is already included in {product.get_tier_display()}."})
        from .slider import check_free
        check_free(ad, addons)
        if len(addons) != len(set(codes)):
            raise ValidationError({"addons": "One of the add-ons is no longer available. Reload the page."})
        if len({a.group_id for a in addons}) != len(addons):
            raise ValidationError({"addons": "Choose one period per add-on."})
        result = activation.activate(request.user, ad, product, str(request.data.get("method", "card")), offer, addons)
        return Response(result, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def checkout(self, request, pk=None):
        ad = self.get_object()
        ser = s.CheckoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if ser.validated_data["product"].tier == "free":
            from .activation import running_free_ad
            other = running_free_ad(request.user, exclude=ad)
            if other:
                raise ValidationError({"detail": f"You already have a Free ad (“{other.title}”). Only one Free ad at a time."})
        try:
            offer = economy.resolve_offer(request.user, ser.validated_data.get("offer"))
            order = payments.start_checkout(request.user, ad, ser.validated_data["product"], offer)
        except payments.PaymentError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(
            {"order_id": order.order_id, "payment_url": order.payment_url},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def bump(self, request, pk=None):
        ad = economy.bump_ad(request.user, self.get_object())
        return Response({**self.get_serializer(ad).data, "diamonds": request.user.diamonds})

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        ad = self.get_object()
        if ad.status == Ad.Status.ACTIVE:
            ad.status = Ad.Status.PAUSED
            ad.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(ad).data)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def pictures(self, request, pk=None):
        ad = self.get_object()
        files = request.FILES.getlist("files")
        if not files:
            raise ValidationError({"files": "Choose at least one image."})
        if ad.pictures.count() + len(files) > settings.MAX_PICTURES_PER_AD:
            raise ValidationError({"files": f"An ad can have at most {settings.MAX_PICTURES_PER_AD} pictures."})
        start = ad.pictures.count()
        for i, f in enumerate(files):
            try:
                content, w, h = process_upload(f)
            except InvalidImage as exc:
                raise ValidationError({"files": f"{f.name}: {exc}"})
            pic = AdPicture(ad=ad, width=w, height=h, position=start + i)
            pic.image.save(content.name, content, save=True)
        if ad.status == Ad.Status.ACTIVE and SiteSettings.load().review_ads:
            ad.status = Ad.Status.PENDING  # new pictures need review
            ad.save(update_fields=["status", "updated_at"])
        return self.fresh(ad, status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def videos(self, request, pk=None):
        """Add a video (MP4 or MOV, at most 30 seconds and 50 MB)."""
        from .membership import InvalidVideo, mp4_duration
        from .models import AdVideo
        ad = self.get_object()
        f = request.FILES.get("file")
        if not f:
            raise ValidationError({"file": "Choose a video."})
        if ad.videos.count() >= AdVideo.MAX_PER_AD:
            raise ValidationError({"file": f"An ad can have at most {AdVideo.MAX_PER_AD} videos."})
        ctype = (f.content_type or "").split(";")[0].lower()
        if ctype not in ("video/mp4", "video/quicktime"):
            raise ValidationError({"file": "Upload an MP4 or MOV video."})
        if f.size > 50 * 1024 * 1024:
            raise ValidationError({"file": "Videos can be at most 50 MB."})
        try:
            seconds = mp4_duration(f)
        except (InvalidVideo, struct.error, IndexError) as exc:
            raise ValidationError({"file": str(exc) or "This is not a valid video."})
        if seconds > AdVideo.MAX_SECONDS + 0.5:  # a little slack for rounding in phone videos
            raise ValidationError({"file": f"Videos can be at most {AdVideo.MAX_SECONDS} seconds – this one is {seconds:.0f} seconds."})
        if seconds < 1:
            raise ValidationError({"file": "This video is too short."})
        ext = "mov" if ctype == "video/quicktime" else "mp4"
        video = AdVideo(ad=ad, content_type=ctype, duration=seconds, size=f.size, position=ad.videos.count())
        video.file.save(f"video.{ext}", f, save=True)
        if ad.status == Ad.Status.ACTIVE and SiteSettings.load().review_ads:
            ad.status = Ad.Status.PENDING  # new videos need review, like pictures
            ad.save(update_fields=["status", "updated_at"])
        return self.fresh(ad, status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"videos/(?P<video_id>\d+)")
    def delete_video(self, request, pk=None, video_id=None):
        from .models import AdVideo
        ad = self.get_object()
        video = get_object_or_404(AdVideo, ad=ad, pk=video_id)
        video.file.delete(save=False)
        video.delete()
        return self.fresh(ad)

    @action(detail=True, methods=["post"], url_path=r"pictures/(?P<picture_id>\d+)/replace", parser_classes=[MultiPartParser])
    def replace_picture(self, request, pk=None, picture_id=None):
        """Swap a picture for a cropped/rotated version; position and caption stay."""
        ad = self.get_object()
        pic = get_object_or_404(AdPicture, ad=ad, pk=picture_id)
        f = request.FILES.get("file")
        if not f:
            raise ValidationError({"file": "Choose an image."})
        try:
            content, w, h = process_upload(f)
        except InvalidImage as exc:
            raise ValidationError({"file": str(exc)})
        pic.image.delete(save=False)
        pic.width, pic.height = w, h
        pic.image.save(content.name, content, save=True)
        if ad.status == Ad.Status.ACTIVE and SiteSettings.load().review_ads:
            ad.status = Ad.Status.PENDING  # changed pictures need review
            ad.save(update_fields=["status", "updated_at"])
        return self.fresh(ad)

    @action(detail=True, methods=["patch", "delete"], url_path=r"pictures/(?P<picture_id>\d+)")
    def delete_picture(self, request, pk=None, picture_id=None):
        """PATCH {caption} edits the picture text; DELETE removes the picture."""
        ad = self.get_object()
        pic = get_object_or_404(AdPicture, ad=ad, pk=picture_id)
        if request.method == "PATCH":
            pic.caption = str(request.data.get("caption", "")).strip()[:120]
            pic.save(update_fields=["caption"])
            return self.fresh(ad)
        pic.image.delete(save=False)
        pic.delete()
        return self.fresh(ad)

    @action(detail=True, methods=["post"], url_path="pictures/order")
    def order_pictures(self, request, pk=None):
        ad = self.get_object()
        ids = request.data.get("ids", [])
        pics = {p.id: p for p in ad.pictures.all()}
        if not isinstance(ids, list):
            raise ValidationError({"ids": "Send a list of picture ids."})
        for pos, pid in enumerate(ids):
            pid = int(pid) if str(pid).isdigit() else None
            if pid in pics:
                pics[pid].position = pos
        AdPicture.objects.bulk_update(pics.values(), ["position"])
        return self.fresh(ad)


# ---------------------------------------------------------------------------
# Tags & content
# ---------------------------------------------------------------------------


class TagListView(generics.ListCreateAPIView):
    """GET all tags; POST {name} lets an advertiser create their own tag (or get the existing one)."""

    permission_classes = [permissions.AllowAny]
    serializer_class = s.TagSerializer
    pagination_class = None
    throttle_scope = "tags"

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.request.method == "POST" else super().get_throttles()

    def create(self, request, *args, **kwargs):
        if not (request.user.is_authenticated and request.user.is_advertiser):
            raise PermissionDenied("Only advertisers can create tags.")
        import re
        name = " ".join(str(request.data.get("name", "")).split())[:40]
        if len(name) < 2 or not re.fullmatch(r"[\w][\w &+\-]*", name):
            raise ValidationError({"name": "Use 2–40 letters, numbers or spaces."})
        tag = Tag.objects.filter(name__iexact=name).first() or Tag.objects.filter(slug=slugify(name)).first()
        created = tag is None
        if created:
            tag = Tag.objects.create(name=name, created_by=request.user)
        return Response(s.TagSerializer(tag).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def get_queryset(self):
        qs = Tag.objects.annotate(ad_count=Count("ads", filter=Q(ads__status=Ad.Status.ACTIVE)))
        if self.request.query_params.get("used") == "1":
            qs = qs.filter(ad_count__gt=0)
        return qs.order_by("name")


class TopicListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = s.TopicListSerializer
    pagination_class = AdPagination

    def get_queryset(self):
        qs = Topic.objects.filter(is_published=True)
        kind = self.request.query_params.get("kind")
        if kind in {Topic.Kind.ARTICLE, Topic.Kind.BLOG}:
            return qs.filter(kind=kind).order_by("-published_at")
        return qs.filter(kind=kind) if kind else qs


class TopicDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = s.TopicSerializer

    def get_object(self):
        return get_object_or_404(
            Topic, kind=self.kwargs["kind"], slug=self.kwargs["slug"], is_published=True
        )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


class ProductListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = s.ProductSerializer
    pagination_class = None
    queryset = Product.objects.filter(is_active=True)


class MyOrdersView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = s.OrderSerializer

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).select_related("ad")


class OrderStatusView(APIView):
    """Used by the return page: re-checks the gateway while the order is pending."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, order_id):
        order = get_object_or_404(Order, order_id=order_id, user=request.user)
        order = payments.refresh_order(order)
        return Response(s.OrderSerializer(order).data)


class StripeWebhookView(APIView):
    """Stripe webhook. Checked with the Stripe-Signature header; the session is then re-read from Stripe."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def post(self, request):
        gateway = payments.get_gateway()
        if gateway.name != "stripe" or not gateway.verify_webhook(request.body, request.headers.get("Stripe-Signature", "")):
            return Response({"detail": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            event = json.loads(request.body)
        except ValueError:
            return Response({"detail": "Invalid body"}, status=status.HTTP_400_BAD_REQUEST)
        if not str(event.get("type", "")).startswith("checkout.session."):
            return Response({"ok": True})  # other events are not used
        session_id = str(((event.get("data") or {}).get("object") or {}).get("id") or "")
        order = Order.objects.filter(gateway="stripe", gateway_payment_id=session_id).first() if session_id else None
        if order is None:
            return Response({"detail": "Unknown order"}, status=status.HTTP_200_OK)  # acknowledge so Stripe stops retrying
        try:
            payments.apply_gateway_state(order.pk, gateway.fetch(session_id))
        except payments.PaymentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True})


class PaymentWingsWebhookView(APIView):
    """PaymentWings server-to-server webhook. Authenticated by its md5 signature, not by session."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def post(self, request):
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            body = request.POST.dict()  # in case it arrives as a form
        if not isinstance(body, dict):
            return Response({"detail": "Invalid body"}, status=status.HTTP_400_BAD_REQUEST)
        txn, timestamp = str(body.get("txn") or ""), str(body.get("timestamp") or "")
        gateway = payments.get_gateway()
        if not gateway.verify_webhook(txn, timestamp, str(body.get("signature") or "")):
            return Response({"detail": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)
        order = Order.objects.filter(gateway=gateway.name, gateway_payment_id=txn).first()
        metadata = body.get("metadata")
        if order is None or (metadata and str(metadata) != order.order_id):
            # Acknowledge so PaymentWings stops retrying; nothing to do for unknown orders.
            return Response({"detail": "Unknown order"}, status=status.HTTP_200_OK)
        state_name = str(body.get("status") or "").lower()
        state = {"id": txn, "accepted": state_name == "completed", "state": state_name, "source": "webhook",
                 "timestamp": timestamp}
        try:
            payments.apply_gateway_state(order.pk, state)
        except payments.PaymentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True})


class FakePaymentView(APIView):
    """Development-only stand-in for the PaymentWings checkout page."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, order_id):
        if not settings.PAYMENTS_ALLOW_FAKE:
            raise PermissionDenied("Fake payments are disabled.")
        order = get_object_or_404(Order, order_id=order_id, user=request.user, gateway="fake")
        result = request.data.get("result")
        if result == "success":
            state = {
                "id": order.gateway_payment_id, "order_id": order.order_id, "accepted": True,
                "currency": order.currency, "state": "completed", "metadata": {"brand": "test-card"},
                "paid_amount": order.amount,
            }
        else:
            state = {"id": order.gateway_payment_id, "order_id": order.order_id, "accepted": False,
                     "currency": order.currency, "state": "rejected"}
        order.last_gateway_state = state
        order.save(update_fields=["last_gateway_state", "updated_at"])
        order = payments.apply_gateway_state(order.pk, state)
        return Response(s.OrderSerializer(order).data)


def _letter(term: str) -> str:
    first = term.strip()[:1].upper()
    return first if first.isalpha() else "#"


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def dictionary_index(request):
    """Every dictionary term and alias, A–Z. Aliases link to their main entry."""
    terms = []
    for t in Topic.objects.filter(kind=Topic.Kind.DICTIONARY, is_published=True):
        terms.append({"term": t.title, "slug": t.slug, "letter": _letter(t.title), "is_alias": False, "summary": t.summary})
        for alias in t.alias_list():
            terms.append({"term": alias, "slug": t.slug, "letter": _letter(alias), "is_alias": True, "summary": t.summary})
    terms.sort(key=lambda x: (x["letter"] != "#", x["term"].casefold()))
    return Response({"terms": terms, "entry_count": len({t["slug"] for t in terms})})


class TopicHeartView(APIView):
    """GET current state, POST to give a heart, DELETE to remove it (articles and blogs)."""

    permission_classes = [permissions.IsAuthenticated]

    def get_topic(self, pk):
        return get_object_or_404(Topic, pk=pk, is_published=True)

    def state(self, request, topic):
        topic.refresh_from_db(fields=["heart_count"])
        hearted = TopicHeart.objects.filter(user=request.user, topic=topic).exists()
        return Response({"hearted": hearted, "heart_count": topic.heart_count})

    def get(self, request, pk):
        return self.state(request, self.get_topic(pk))

    def post(self, request, pk):
        topic = self.get_topic(pk)
        try:
            with transaction.atomic():
                TopicHeart.objects.create(user=request.user, topic=topic)
                Topic.objects.filter(pk=topic.pk).update(heart_count=F("heart_count") + 1)
        except IntegrityError:
            pass
        return self.state(request, topic)

    def delete(self, request, pk):
        topic = self.get_topic(pk)
        deleted, _ = TopicHeart.objects.filter(user=request.user, topic=topic).delete()
        if deleted:
            Topic.objects.filter(pk=topic.pk, heart_count__gt=0).update(heart_count=F("heart_count") - 1)
        return self.state(request, topic)


class MyTopicHeartsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ids = TopicHeart.objects.filter(user=request.user).values_list("topic_id", flat=True)
        return Response({"topic_ids": list(ids)})


class TagDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = s.TagSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return Tag.objects.annotate(ad_count=Count("ads", filter=Q(ads__status=Ad.Status.ACTIVE)))


# ---------------------------------------------------------------------------
# Account settings
# ---------------------------------------------------------------------------


class SendConfirmationView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        if request.user.email_confirmed:
            return Response({"detail": "Your e-mail is already confirmed."})
        account.send_confirmation(request.user)
        return Response({"detail": f"We sent a confirmation link to {request.user.email}."})


class ConfirmEmailView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        try:
            user = account.read_token(str(request.data.get("token", "")))
        except account.InvalidToken as exc:
            raise ValidationError({"detail": str(exc)})
        if not user.email_confirmed:
            user.email_confirmed = True
            user.save(update_fields=["email_confirmed"])
        return Response({"detail": "Thank you – your e-mail is confirmed.", "email": user.email})


class ChangeEmailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        ser = s.EmailChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not request.user.check_password(ser.validated_data["password"]):
            raise ValidationError({"password": "Wrong password."})
        user = request.user
        old_email = user.email
        user.email = ser.validated_data["email"]
        user.email_confirmed = False
        user.save(update_fields=["email", "email_confirmed"])
        if old_email.lower() != user.email.lower():
            account.send_email_changed(user, old_email)
        try:
            account.send_confirmation(user)
        except Exception:
            pass
        return Response(s.UserSerializer(user).data)


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        ser = s.PasswordChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(ser.validated_data["current_password"]):
            raise ValidationError({"current_password": "Wrong password."})
        try:
            password_validation.validate_password(ser.validated_data["new_password"], user)
        except Exception as exc:  # django.core.exceptions.ValidationError
            raise ValidationError({"new_password": list(getattr(exc, "messages", [str(exc)]))})
        user.set_password(ser.validated_data["new_password"])
        user.save(update_fields=["password"])
        update_session_auth_hash(request, user)  # stay logged in on this device
        account.send_password_changed(user)
        return Response({"detail": "Your password has been changed."})


class ProfilePictureView(APIView):
    """Private avatar: shown only in the user's own menu and settings."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            raise ValidationError({"file": "Choose a picture."})
        try:
            content, _, _ = process_upload(f)
        except InvalidImage as exc:
            raise ValidationError({"file": str(exc)})
        user = request.user
        if user.profile_picture:
            user.profile_picture.delete(save=False)
        user.profile_picture.save(content.name, content, save=True)
        return Response(s.UserSerializer(user).data)

    def delete(self, request):
        user = request.user
        if user.profile_picture:
            user.profile_picture.delete(save=True)
        return Response(s.UserSerializer(user).data)


class DeleteAccountView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [AuthThrottle]
    throttle_scope = "auth"

    def post(self, request):
        user = request.user
        if not user.check_password(str(request.data.get("password", ""))):
            raise ValidationError({"password": "Wrong password."})
        if user.orders.exists():
            # Keep payment records for bookkeeping: anonymise instead of deleting.
            user.ads.all().delete()
            user.is_active = False
            user.email = f"deleted-{user.pk}@deleted.invalid"
            user.display_name = ""
            user.phone = ""
            if user.profile_picture:
                user.profile_picture.delete(save=False)
            user.set_unusable_password()
            user.save()
        else:
            user.delete()
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Coins and offers
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def offers(request):
    return Response(s.OfferSerializer(economy.active_offers(), many=True).data)


ENDED_OFFER_DAYS = 3


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def check_offer(request):
    """GET /api/offers/check?offer=<id or code> – can I use it? Returns the offer, or 400 with the reason."""
    offer = economy.resolve_offer(request.user, request.query_params.get("offer"))
    if offer is None:
        raise ValidationError({"offer": "Enter a code."})
    return Response({
        "id": offer.id, "title": offer.title, "code": offer.code, "discount_percent": offer.discount_percent,
        "applies_to": offer.applies_to, "tier": offer.tier, "scope": _offer_scope(offer),
        "ends_at": None if offer.no_end_date else offer.ends_at,
    })


def _offer_scope(o):
    if o.applies_to == "diamonds":
        return "on coin packages"
    if o.applies_to == "plans":
        return f"on {o.get_tier_display()} plans" if o.tier else "on all plans"
    return "on plans and coins"


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def my_offers(request):
    """GET /api/offers/mine – the Offers page: offers with the user's status, plus their ads for 'Use offer'."""
    from datetime import timedelta

    from django.db.models import Count, Q

    from .models import Offer
    now = timezone.now()
    user = request.user
    ads = list(Ad.objects.filter(owner=user).exclude(status=Ad.Status.REJECTED).prefetch_related("pictures").order_by("-updated_at"))
    used = economy.used_offer_ids(user)
    qs = (Offer.objects.filter(is_active=True, starts_at__lte=now)
          .filter(Q(no_end_date=True) | Q(ends_at__gt=now - timedelta(days=ENDED_OFFER_DAYS)))
          .annotate(paid_uses=Count("orders", filter=Q(orders__status="paid"))).order_by("ends_at"))
    rows = []
    for o in qs:
        st = economy.offer_status(o, used=used)
        rows.append({
            **s.OfferSerializer(o, context={"request": request}).data,
            "status": st,
            "code": o.code,
            "discount": f"−{o.discount_percent}% {_offer_scope(o)}",
            "packages_only": o.applies_to == "diamonds",
        })
    order = {"active": 0, "used": 1, "sold_out": 2, "ended": 3}
    rows.sort(key=lambda r: order[r["status"]])
    ad_rows = []
    for ad in ads:
        pic = ad.pictures.first()
        ad_rows.append({
            "id": str(ad.id), "title": ad.title, "cover": pic.image.url if pic and pic.image else None,
            "categories": ad.category_label,
            "finalized": ad.status != Ad.Status.DRAFT,
            "active_until": ad.expires_at if ad.is_live else None,
        })
    return Response({"offers": rows, "ads": ad_rows, "ended_days": ENDED_OFFER_DAYS})


class DiamondsView(APIView):
    """GET /api/coins – balance, packages, add-ons per live ad and history. POST /api/coins/spend."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .models import DiamondTransaction
        packages = Product.objects.filter(kind=Product.Kind.DIAMONDS, is_active=True).order_by("diamonds")
        request.user.refresh_from_db(fields=["diamonds"])
        log = DiamondTransaction.objects.filter(user=request.user)[:50]
        ads = Ad.objects.live().filter(owner=request.user).prefetch_related("pictures").order_by("-bumped_at")
        ad_rows = []
        for ad in ads:
            pic = ad.pictures.first()
            ad_rows.append({
                "id": str(ad.id), "title": ad.title, "cover": pic.image.url if pic and pic.image else None,
                "expires_at": ad.expires_at, "tier": ad.effective_tier, "tier_until": ad.tier_until if ad.effective_tier != "free" else None,
                "addons": economy.addons_for(ad),
            })
        return Response({
            "balance": request.user.diamonds,
            "bump_cost": economy.BUMP_COST,
            "cashback_percent": economy.CASHBACK_PERCENT,
            "coin_value": economy.coin_value(),
            "currency": SiteSettings.load().currency,
            "packages": s.PlanPriceSerializer(packages, many=True, context={"request": request, "offer": _chosen_offer(request)}).data,
            "ads": ad_rows,
            "log": [{"amount": t.amount, "reason": t.reason, "created_at": t.created_at} for t in log],
        })


class SpendCoinsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ad = get_object_or_404(Ad, pk=request.data.get("ad"), owner=request.user)
        codes = request.data.get("items") or []
        if not isinstance(codes, list):
            raise ValidationError({"items": "Send a list of add-on codes."})
        result = economy.spend_on_ad(request.user, ad, [str(c) for c in codes][:10])
        return Response(result)


class BuyDiamondsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        product = get_object_or_404(Product, code=request.data.get("product"), kind=Product.Kind.DIAMONDS, is_active=True)
        offer = economy.resolve_offer(request.user, request.data.get("offer"))
        try:
            order = payments.start_checkout(request.user, None, product, offer)
        except payments.PaymentError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"order_id": order.order_id, "payment_url": order.payment_url}, status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([permissions.IsAuthenticated])
def delete_review(request, pk):
    """Authors (and staff) can delete a review."""
    from .models import AdReview
    r = get_object_or_404(AdReview, pk=pk)
    if request.user.pk != r.user_id and not request.user.is_staff:
        raise PermissionDenied()
    r.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


class VipView(APIView):
    """GET /api/vip – Free vs VIP, prices and your status. POST {product, method: card|coins} buys VIP."""

    def get_permissions(self):
        return [permissions.IsAuthenticated()] if self.request.method == "POST" else [permissions.AllowAny()]

    def get(self, request):
        from . import membership
        user = request.user
        prices = [{"code": p.code, "name": p.name, "days": p.days, "duration_label": p.get_days_display(),
                   "price": p.price, "currency": p.currency, "coins": membership.coin_price(p),
                   "per_day": round(p.price / max(1, p.days))} for p in membership.products()]
        return Response({
            "perks": membership.PERKS,
            "prices": prices,
            "status": membership.status(user) if user.is_authenticated else {"is_vip": False, "vip_until": None},
            "coins": user.diamonds if user.is_authenticated else 0,
        })

    def post(self, request):
        from . import membership
        product = get_object_or_404(membership.products(), code=request.data.get("product"))
        if request.data.get("method") == "coins":
            order = membership.buy_with_coins(request.user, product)
            request.user.refresh_from_db(fields=["vip_until", "diamonds"])
            return Response({"order_id": order.order_id, "paid": True, **membership.status(request.user),
                             "coins": request.user.diamonds}, status=status.HTTP_201_CREATED)
        try:
            order = payments.start_checkout(request.user, None, product)
        except payments.PaymentError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"order_id": order.order_id, "payment_url": order.payment_url, "paid": False},
                        status=status.HTTP_201_CREATED)


@api_view(["DELETE"])
@permission_classes([permissions.IsAuthenticated])
def delete_comment(request, pk):
    """Authors and the ad's owner can delete a comment."""
    from .models import Comment
    c = get_object_or_404(Comment.objects.select_related("ad"), pk=pk)
    if request.user.pk not in (c.user_id, c.ad.owner_id) and not request.user.is_staff:
        raise PermissionDenied()
    c.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


class MyFavoritesView(generics.ListAPIView):
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = s.AdCardSerializer
    pagination_class = AdPagination

    def get_queryset(self):
        return Ad.objects.live().filter(favorited_by__user=self.request.user).prefetch_related("pictures").order_by(
            "-favorited_by__created_at")


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def advertiser_profile(request, pk):
    """Public profile: live ads plus (if the advertiser allows) earlier ads. Never shows contact details."""
    from .models import User
    user = get_object_or_404(User, pk=pk, is_active=True, is_advertiser=True)
    live = list(Ad.objects.live().filter(owner=user).prefetch_related("pictures").order_by("-bumped_at"))
    now = timezone.now()
    old = list(Ad.objects.filter(owner=user, published_at__isnull=False)
               .filter(Q(expires_at__lte=now) | Q(status=Ad.Status.PAUSED))
               .exclude(pk__in=[a.pk for a in live])
               .prefetch_related("pictures").order_by("-published_at")[:50])
    shown_old = old if user.show_ad_history else []
    past = []
    for ad, card in zip(shown_old, s.AdCardSerializer(shown_old, many=True).data):
        past.append({**card, "status": "expired" if ad.status == Ad.Status.ACTIVE else "paused",
                     "ended_at": ad.expires_at if ad.status == Ad.Status.ACTIVE else ad.updated_at})
    every = live + shown_old
    stats = {
        "likes": sum(a.heart_count for a in every),
        "views": sum(a.view_count for a in every),
        "active": len(live),
        "inactive": len(shown_old),
    }
    name = user.display_name or "Advertiser"
    return Response({
        "id": user.pk, "name": name,
        "initials": "".join(w[0] for w in name.split() if w[0].isalnum())[:2].upper() or "?",
        "avatar": None,  # profile pictures are private (Settings promises they are never public)
        "verified": user.age_verified, "member_since": user.date_joined,
        "likes": stats["likes"], "stats": stats, "active_ads": s.AdCardSerializer(live, many=True).data,
        "past_ads": past, "history_hidden": not user.show_ad_history,
    })


@api_view(["POST"])
@permission_classes([permissions.AllowAny])
def confirm_report(request):
    try:
        report = account.read_report_token(str(request.data.get("token", "")))
    except account.InvalidToken as exc:
        raise ValidationError({"detail": str(exc)})
    if report.status == Report.Status.DRAFT:
        report.status = Report.Status.OPEN
        report.confirmed_at = timezone.now()
        report.save(update_fields=["status", "confirmed_at"])
    return Response({"detail": "Thank you – your report has been sent to our moderators.", "urgent": report.is_urgent})


# ---------------------------------------------------------------------------
# ID verification
# ---------------------------------------------------------------------------


def verification_data(v):
    if not v:
        return None
    return {"status": v.status, "status_label": v.get_status_display(), "document_type": v.document_type,
            "created_at": v.created_at, "reviewed_at": v.reviewed_at, "reject_reason": v.reject_reason}


class VerificationThrottle(SimpleRateThrottle):
    scope = "report"  # 20/hour is plenty for uploads

    def get_cache_key(self, request, view):
        return None if request.method == "GET" else self.cache_format % {"scope": "verification", "ident": request.user.pk}


class VerificationView(APIView):
    """GET my latest ID check; POST documents (multipart) for a new one."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [VerificationThrottle]

    def get(self, request):
        from .models import IdVerification
        return Response({"verified": request.user.age_verified,
                         "latest": verification_data(IdVerification.objects.filter(user=request.user).first()),
                         "document_types": [{"value": v, "label": l} for v, l in IdVerification.DocumentType.choices]})

    def post(self, request):
        from datetime import date

        from .models import IdVerification
        user = request.user
        # A visitor converts to an advertiser by sending the documents with `become_advertiser` – never before.
        converting = not user.is_advertiser and request.data.get("become_advertiser") in ("true", "1", "on")
        if not user.is_advertiser and not converting:
            raise ValidationError({"detail": "Only advertisers need ID verification."})
        if user.age_verified:
            raise ValidationError({"detail": "You are already verified."})
        if IdVerification.objects.filter(user=user, status=IdVerification.Status.PENDING).exists():
            raise ValidationError({"detail": "Your documents are already waiting for review."})

        d = request.data
        errors = {}
        doc_type = d.get("document_type", "")
        if doc_type not in IdVerification.DocumentType.values:
            errors["document_type"] = "Choose a document type."
        full_name = (d.get("full_name") or "").strip()
        if len(full_name) < 3:
            errors["full_name"] = "Write your name as it is on the document."
        try:
            dob = date.fromisoformat(d.get("date_of_birth", ""))
            today = timezone.localdate()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            if age < 18:
                errors["date_of_birth"] = "You must be at least 18 years old."
            elif age > 110:
                errors["date_of_birth"] = "Check your date of birth."
        except ValueError:
            dob = None
            errors["date_of_birth"] = "Enter your date of birth."
        required = ["document_front", "selfie"] + (["document_back"] if doc_type in ("id_card", "driving_licence") else [])
        files = {}
        for key in ("document_front", "document_back", "selfie"):
            f = request.FILES.get(key)
            if not f:
                if key in required:
                    errors[key] = "This picture is required."
                continue
            try:
                files[key] = process_upload(f)[0]
            except InvalidImage as exc:
                errors[key] = str(exc)
        if d.get("consent") not in ("true", "1", "on"):
            errors["consent"] = "Please accept that we check your documents."
        if errors:
            raise ValidationError(errors)

        v = IdVerification(user=user, document_type=doc_type, full_name=full_name, date_of_birth=dob)
        for key, content in files.items():
            getattr(v, key).save(content.name, content, save=False)
        v.save()
        if converting:
            user.is_advertiser = True
            user.save(update_fields=["is_advertiser"])
        from .notifications import K, notify
        notify(user, K.ID_RECEIVED, link="/account", key=f"idrecv:{v.pk}")
        return Response({"verified": False, "latest": verification_data(v), "is_advertiser": user.is_advertiser},
                        status=status.HTTP_201_CREATED)


class ContactThrottle(SimpleRateThrottle):
    scope = "report"

    def get_cache_key(self, request, view):
        if request.method != "POST":
            return None
        ident = request.user.pk if request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": "contact", "ident": ident}


@api_view(["GET", "POST"])
@permission_classes([permissions.AllowAny])
@throttle_classes([ContactThrottle])
def contact(request):
    """GET the topics; POST a message from the Contact us form (saved, then e-mailed to the site address)."""
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import validate_email

    from .models import ContactMessage

    if request.method == "GET":
        return Response({"topics": [{"value": v, "label": l} for v, l in ContactMessage.Topic.choices]})
    d = request.data
    if d.get("website"):  # honeypot – bots fill every field
        return Response({"sent": True}, status=status.HTTP_201_CREATED)
    errors = {}
    topic = d.get("topic") or ContactMessage.Topic.GENERAL
    if topic not in ContactMessage.Topic.values:
        errors["topic"] = "Choose a topic."
    name = str(d.get("name") or "").strip()[:80]
    if len(name) < 2:
        errors["name"] = "Write your name."
    email = str(d.get("email") or "").strip()
    try:
        validate_email(email)
    except DjangoValidationError:
        errors["email"] = "Write a valid e-mail address so we can answer you."
    message = str(d.get("message") or "").strip()
    if len(message) < 10:
        errors["message"] = "Write a little more (at least 10 characters)."
    elif len(message) > 4000:
        errors["message"] = "Keep it under 4000 characters."
    if errors:
        raise ValidationError(errors)
    msg = ContactMessage.objects.create(topic=topic, name=name, email=email, message=message,
                                        user=request.user if request.user.is_authenticated else None)
    to = account.site_email()
    from . import emails
    emails.send(to=to, subject=f"contact form – {msg.get_topic_display()} – {name}", heading=f"New message: {msg.get_topic_display()}",
                lines=[message, f"From: {name} <{email}>"]
                + ([f"User id: {request.user.pk}"] if request.user.is_authenticated else []),
                note="Reply to this e-mail to answer the sender directly.", reply=email)
    return Response({"sent": True}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def faq(request):
    """FAQ items grouped by category (Admin → FAQ items)."""
    from .models import FaqItem
    items = FaqItem.objects.filter(is_published=True)
    groups = []
    for value, label in FaqItem.Category.choices:
        rows = [{"id": i.id, "question": i.question, "answer": i.answer} for i in items if i.category == value]
        if rows:
            groups.append({"value": value, "label": label, "items": rows})
    return Response({"groups": groups})
