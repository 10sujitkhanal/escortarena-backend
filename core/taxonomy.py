"""Countries, regions, categories and option lists – all edited in the admin.

Everything reads them through `lists()`, which is cached and cleared whenever one of
the tables changes (see the signal at the bottom).
"""

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save

from .models import AdCategory, AdPlace, AdRegion, ChoiceOption, Country

CACHE_KEY = "taxonomy:v1"
OPTION_GROUPS = [g for g, _ in ChoiceOption.Group.choices]


def _build():
    countries = list(Country.objects.filter(is_active=True))
    regions = list(AdRegion.objects.filter(is_active=True, country__is_active=True).select_related("country"))
    categories = list(AdCategory.objects.filter(is_active=True))
    open_regions = {r.slug for r in regions}
    places = [p for p in AdPlace.objects.filter(is_active=True).select_related("region") if p.region.slug in open_regions]
    options = {g: [] for g in OPTION_GROUPS}
    for o in ChoiceOption.objects.filter(is_active=True):
        options[o.group].append({"value": o.value, "label": o.label, "show_in_menu": o.show_in_menu,
                                 "menu_label": o.menu_label or o.label})
    by_country = {}
    for r in regions:
        by_country.setdefault(r.country.code, []).append({"value": r.slug, "label": r.name})
    return {
        "countries": [
            {"code": c.code, "name": c.name, "dial_code": c.dial_code, "flag": c.flag, "continent": c.continent, "has_ads": c.has_ads,
             "regions": by_country.get(c.code, [])}
            for c in countries
        ],
        # Every active area of every active country (Admin → Countries → Regions).
        "regions": [{"value": r.slug, "label": r.name, "country": r.country.code} for r in regions],
        "places": [{"value": p.slug, "label": p.name, "region": p.region.slug} for p in places],
        "categories": [{"value": c.slug, "label": c.name, "description": c.description} for c in categories],
        "options": options,
        # Labels include inactive entries so old ads still show a name.
        "labels": {
            "region": dict(AdRegion.objects.values_list("slug", "name")),
            # Only active places here (the world seed adds ~34 000); others are looked up when needed.
            "place": {p.slug: p.name for p in places},
            "place_region": {p.slug: p.region.slug for p in places},
            "region_country": {slug: code for slug, code in AdRegion.objects.values_list("slug", "country__code")},
            "category": dict(AdCategory.objects.values_list("slug", "name")),
            **{g: {v: lbl for v, lbl in ChoiceOption.objects.filter(group=g).values_list("value", "label")} for g in OPTION_GROUPS},
        },
    }


def lists() -> dict:
    data = cache.get(CACHE_KEY)
    if data is None:
        data = _build()
        cache.set(CACHE_KEY, data, 10)  # short: other server processes pick up admin edits within seconds
    return data


def label(kind: str, value: str) -> str:
    """Display name for a stored value, e.g. label("region", "funen") -> "Funen"."""
    if not value:
        return ""
    found = lists()["labels"].get(kind, {}).get(value)
    if found is None and kind == "place":
        found = AdPlace.objects.filter(slug=value).values_list("name", flat=True).first()
    return found if found is not None else value


def place_regions(values) -> dict[str, str]:
    """Area of each listed place (active or not); typed-in names are not in the result."""
    return dict(AdPlace.objects.filter(slug__in=list(values)).values_list("slug", "region__slug"))


def is_valid(kind: str, value: str) -> bool:
    data = lists()
    if kind == "region":
        return any(r["value"] == value for r in data["regions"]) or region_open(value)
    if kind == "category":
        return any(c["value"] == value for c in data["categories"])
    if kind == "place":
        return any(p["value"] == value for p in data["places"]) or place_open(value)
    return any(o["value"] == value for o in data["options"].get(kind, []))


def _open_regions(qs):
    """Areas an advertiser may pick: the active ones, or – for a country where none were switched on yet
    (the world seed adds them all switched off) – every area of that country."""
    with_active = set(AdRegion.objects.filter(is_active=True).values_list("country_id", flat=True))
    return qs.filter(country__is_active=True).exclude(is_active=False, country_id__in=with_active)


def _open_places(qs):
    """Same rule for places: active ones, or every place of an area where none are switched on."""
    with_active = set(AdPlace.objects.filter(is_active=True).values_list("region_id", flat=True))
    return qs.exclude(is_active=False, region_id__in=with_active)


def editor_areas(codes: list[str]) -> dict:
    """Areas and places of the chosen countries, for the ad editor (loaded per country, not in /meta)."""
    regions = list(_open_regions(AdRegion.objects.filter(country__code__in=[c.upper() for c in codes]))
                   .select_related("country"))
    places = _open_places(AdPlace.objects.filter(region__in=regions)).select_related("region")
    return {
        "regions": [{"value": r.slug, "label": r.name, "country": r.country.code} for r in regions],
        "places": [{"value": p.slug, "label": p.name, "region": p.region.slug} for p in places],
    }


def region_open(slug: str) -> bool:
    return _open_regions(AdRegion.objects.filter(slug=slug)).exists()


def place_open(slug: str) -> bool:
    return _open_places(AdPlace.objects.filter(slug=slug, region__in=_open_regions(AdRegion.objects.all()))).exists()


def phone_countries() -> set[str]:
    return {c["code"] for c in lists()["countries"]}


def regions_in_country(code: str) -> list[str]:
    return [slug for slug, c in lists()["labels"]["region_country"].items() if c == code.upper()]


def _clear(**kwargs):
    cache.delete(CACHE_KEY)


for _model in (Country, AdRegion, AdPlace, AdCategory, ChoiceOption):
    post_save.connect(_clear, sender=_model, dispatch_uid=f"taxonomy-save-{_model.__name__}")
    post_delete.connect(_clear, sender=_model, dispatch_uid=f"taxonomy-delete-{_model.__name__}")
