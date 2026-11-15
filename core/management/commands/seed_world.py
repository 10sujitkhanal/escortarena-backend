"""Seed every country, its regions (states/provinces) and its cities from core/data/world.json.gz.

Data: GeoNames (https://www.geonames.org), CC BY 4.0 – countries, first-level regions and all
cities with more than 15 000 people. Safe to run again: existing rows are kept, only missing ones
are added. New regions and cities start *inactive*; switch them on per country in the admin
(Countries → action "Activate areas and cities") or here with --activate.

    manage.py seed_world                    # add everything (inactive)
    manage.py seed_world --activate IN NP   # also switch on India's and Nepal's areas and cities
"""

import gzip
import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from core.continents import CONTINENT
from core.models import AdPlace, AdRegion, Country

DATA = Path(__file__).resolve().parents[2] / "data" / "world.json.gz"
# Countries whose areas were set up by hand (Nepal: Kathmandu, Pokhara … instead of provinces).
HAND_MADE = {"NP"}


def flag(code: str) -> str:
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code.upper()) if len(code) == 2 else ""


def unique(base: str, taken: set[str], limit: int) -> str:
    base = (base or "x")[:limit].strip("-")
    slug, n = base, 2
    while slug in taken:
        tail = f"-{n}"
        slug = base[: limit - len(tail)] + tail
        n += 1
    taken.add(slug)
    return slug


class Command(BaseCommand):
    help = "Add all countries, regions and cities (GeoNames) – new regions and cities start inactive."

    def add_arguments(self, parser):
        parser.add_argument("--activate", nargs="*", default=[], metavar="CC",
                            help="Country codes whose areas and cities should be switched on")

    @transaction.atomic
    def handle(self, *args, activate=(), **opts):
        data = json.loads(gzip.open(DATA, "rt", encoding="utf-8").read())

        # Countries: add the missing ones (active), fill in a missing continent on the rest.
        have = {c.code: c for c in Country.objects.all()}
        new_countries = []
        for code, name, cont, phone in data["countries"]:
            c = have.get(code)
            if c is None:
                new_countries.append(Country(code=code, name=name, dial_code=phone or "", flag=flag(code),
                                             continent=CONTINENT.get(code, cont), is_active=True))
            elif not c.continent:
                c.continent = CONTINENT.get(code, cont)
                c.save(update_fields=["continent"])
        Country.objects.bulk_create(new_countries)
        countries = {c.code: c for c in Country.objects.all()}

        # Regions (states / provinces).
        region_slugs = set(AdRegion.objects.values_list("slug", flat=True))
        by_geo = {r.geo_code: r for r in AdRegion.objects.exclude(geo_code="")}
        new_regions = []
        for i, (cc, a1, name, lat, lng) in enumerate(data["regions"]):
            geo = f"{cc}.{a1}"
            if cc in HAND_MADE or geo in by_geo or cc not in countries:
                continue
            new_regions.append(AdRegion(
                country=countries[cc], geo_code=geo, name=name[:60], lat=lat, lng=lng, is_active=False, sort_order=i % 1000,
                slug=unique(slugify(f"{cc}-{name}"), region_slugs, 40),
            ))
        AdRegion.objects.bulk_create(new_regions, batch_size=1000)
        regions = {r.geo_code: r for r in AdRegion.objects.exclude(geo_code="")}

        # Cities, biggest first in each region.
        place_slugs = set(AdPlace.objects.values_list("slug", flat=True))
        have_places = set(AdPlace.objects.values_list("region_id", "name"))
        new_places, order = [], {}
        for cc, a1, name, lat, lng, _pop in data["cities"]:
            region = regions.get(f"{cc}.{a1}")
            if region is None or (region.pk, name[:80]) in have_places:
                continue
            have_places.add((region.pk, name[:80]))
            order[region.pk] = order.get(region.pk, -1) + 1
            new_places.append(AdPlace(
                region=region, name=name[:80], lat=lat, lng=lng, is_active=False, sort_order=min(order[region.pk], 32000),
                slug=unique(slugify(f"{cc}-{name}"), place_slugs, 80),
            ))
        AdPlace.objects.bulk_create(new_places, batch_size=2000)

        for code in [c.upper() for c in activate]:
            n = activate_country(code)
            self.stdout.write(f"Activated {code}: {n[0]} areas, {n[1]} cities.")

        lookups_changed()
        self.stdout.write(self.style.SUCCESS(
            f"Added {len(new_countries)} countries, {len(new_regions)} regions, {len(new_places)} cities "
            f"(now {Country.objects.count()} / {AdRegion.objects.count()} / {AdPlace.objects.count()})."
        ))


def activate_country(code: str, on: bool = True) -> tuple[int, int]:
    """Switch a country's areas and cities on (or off) together."""
    Country.objects.filter(code=code).update(is_active=True) if on else None
    regions = AdRegion.objects.filter(country__code=code)
    n_regions = regions.update(is_active=on)
    n_places = AdPlace.objects.filter(region__country__code=code).update(is_active=on)
    lookups_changed()
    return n_regions, n_places


def lookups_changed():
    """Bulk updates skip the save signals: refresh the cached lists here and on the website."""
    from core.signals import revalidate
    from core.taxonomy import _clear
    _clear()
    revalidate("lookups")
