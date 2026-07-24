from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .config import DEFAULT_PER_ZIP_CAP, MAX_DISCOVERY_WORKERS, MIN_CITY_QUOTA
from .locations import ZipLocation, build_zip_pool


@dataclass
class GeoUnit:
    """A geographic slice with a target company quota and its ZIP pool."""

    country: str
    state: str
    state_abbr: str
    city: str | None
    quota: int
    zips: list[ZipLocation] = field(default_factory=list)

    def label(self) -> str:
        if self.city:
            return f"{self.city}, {self.state_abbr or self.state} ({self.country})"
        if self.state:
            return f"{self.state_abbr or self.state} ({self.country})"
        return self.country or "all locations"


@dataclass
class CountryNode:
    country: str
    states: dict[str, StateNode] = field(default_factory=dict)


@dataclass
class StateNode:
    state: str
    state_abbr: str
    cities: dict[str, list[ZipLocation]] = field(default_factory=dict)


def divide_evenly(total: int, parts: int) -> list[int]:
    """Split *total* into *parts* whole quotas; remainder goes to first buckets."""
    if parts <= 0:
        return []
    if total <= 0:
        return [0] * parts
    base, remainder = divmod(total, parts)
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def calculate_optimal_pipeline_count(
    limit: int,
    total_zips: int,
    max_cap: int = MAX_DISCOVERY_WORKERS,
    per_zip_cap: int = DEFAULT_PER_ZIP_CAP,
) -> int:
    """
    Calculate the optimal number of discovery pipelines needed.

    Assumes each ZIP can contribute up to ``per_zip_cap`` companies
    (paginating across Maps pages under one sticky session).
    Never plans more pipelines than available ZIPs or ``max_cap``.
    """
    if total_zips <= 0:
        return 1
    if limit <= 0:
        return min(max(1, total_zips), max_cap)

    per_zip = max(1, int(per_zip_cap or DEFAULT_PER_ZIP_CAP))
    zips_needed = math.ceil(limit / per_zip)
    return max(1, min(zips_needed, total_zips, max_cap))


def build_location_hierarchy(
    *,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    path: str | None = None,
) -> list[CountryNode]:
    """
    Group ZIP rows as country → state → city.
    """
    locs = build_zip_pool(
        countries=countries,
        states=states,
        cities=None,
        shuffle=False,
        path=path,
    )
    by_country: dict[str, CountryNode] = {}
    for loc in locs:
        cnode = by_country.setdefault(loc.country, CountryNode(country=loc.country))
        snode = cnode.states.setdefault(
            loc.state,
            StateNode(state=loc.state, state_abbr=loc.state_abbr),
        )
        if loc.state_abbr and not snode.state_abbr:
            snode.state_abbr = loc.state_abbr
        snode.cities.setdefault(loc.city, []).append(loc)

    return sorted(by_country.values(), key=lambda n: n.country.lower())


def _selected_cities_in_state(
    snode: StateNode,
    cities_sel: list[str],
) -> list[tuple[str, list[ZipLocation]]]:
    cities_norm = {c.strip().lower() for c in cities_sel}
    return [
        (city, zips)
        for city, zips in sorted(snode.cities.items(), key=lambda item: item[0].lower())
        if city.lower() in cities_norm
    ]


def build_scrape_units(
    *,
    limit: int,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    cities: list[str] | None = None,
    per_zip_cap: int = DEFAULT_PER_ZIP_CAP,
    min_city_quota: int = MIN_CITY_QUOTA,
    path: str | None = None,
) -> list[GeoUnit]:
    """
    Build geographic scrape units with per-unit quotas.

    Pipeline distribution strategy:
    1. If `cities` specified: 1 pipeline per matching city.
    2. If `states` specified (no cities): split each state's quota into
       ceil(quota / per_zip_cap) pipelines and divide that state's ZIP pool.
    3. If country-only: calculate optimal pipeline count from limit / per_zip_cap.
    """
    countries_sel = countries or []
    states_sel = states or []
    cities_sel = cities or []
    per_zip = max(1, int(per_zip_cap or DEFAULT_PER_ZIP_CAP))

    hierarchy = build_location_hierarchy(
        countries=countries or None,
        states=states or None,
        path=path,
    )
    if not hierarchy:
        return []

    units: list[GeoUnit] = []

    # Scenario 1: Explicit cities specified
    if cities_sel:
        matching_cities: list[tuple[str, str, str, str, list[ZipLocation]]] = []
        for cnode in hierarchy:
            for snode in sorted(cnode.states.values(), key=lambda n: n.state.lower()):
                selected = _selected_cities_in_state(snode, cities_sel)
                for city, zips in selected:
                    if zips:
                        matching_cities.append(
                            (cnode.country, snode.state, snode.state_abbr, city, list(zips))
                        )

        if matching_cities:
            quotas = (
                divide_evenly(limit, len(matching_cities))
                if limit > 0
                else [0] * len(matching_cities)
            )
            for (country, state, state_abbr, city, zips), quota in zip(matching_cities, quotas):
                if limit > 0 and quota <= 0:
                    continue
                units.append(
                    GeoUnit(
                        country=country,
                        state=state,
                        state_abbr=state_abbr,
                        city=city,
                        quota=quota,
                        zips=zips,
                    )
                )
            for unit in units:
                random.shuffle(unit.zips)
            return units

    # Scenario 2: Explicit states specified (no cities)
    if states_sel:
        state_entries: list[tuple[str, StateNode]] = []
        for cnode in hierarchy:
            for snode in sorted(cnode.states.values(), key=lambda n: n.state.lower()):
                state_entries.append((cnode.country, snode))

        if state_entries:
            quotas = (
                divide_evenly(limit, len(state_entries))
                if limit > 0
                else [0] * len(state_entries)
            )
            for (country, snode), quota in zip(state_entries, quotas):
                if limit > 0 and quota <= 0:
                    continue
                all_zips: list[ZipLocation] = []
                for zips in snode.cities.values():
                    all_zips.extend(zips)
                if not all_zips:
                    continue

                # Unlimited: one pipeline per state. Otherwise fan out like country.
                if limit <= 0:
                    pipeline_count = 1
                else:
                    pipeline_count = calculate_optimal_pipeline_count(
                        quota,
                        len(all_zips),
                        max_cap=MAX_DISCOVERY_WORKERS,
                        per_zip_cap=per_zip,
                    )
                pipeline_count = max(1, min(pipeline_count, len(all_zips)))
                p_quotas = (
                    divide_evenly(quota, pipeline_count)
                    if quota > 0
                    else [0] * pipeline_count
                )
                zip_buckets = divide_evenly(len(all_zips), pipeline_count)
                cursor = 0
                for p_idx in range(pipeline_count):
                    b_size = zip_buckets[p_idx]
                    bucket_zips = all_zips[cursor : cursor + b_size]
                    cursor += b_size
                    p_quota = p_quotas[p_idx]
                    if quota > 0 and p_quota <= 0:
                        continue
                    if not bucket_zips:
                        continue
                    if pipeline_count == 1:
                        s_label = snode.state
                        s_abbr = snode.state_abbr
                    else:
                        s_label = f"{snode.state} · Seg {p_idx + 1}"
                        s_abbr = f"{snode.state_abbr}-{p_idx + 1}"
                    units.append(
                        GeoUnit(
                            country=country,
                            state=s_label,
                            state_abbr=s_abbr,
                            city=None,
                            quota=p_quota,
                            zips=bucket_zips,
                        )
                    )
            for unit in units:
                random.shuffle(unit.zips)
            return units

    # Scenario 3: Country-only (no states, no cities)
    divide_countries = len(hierarchy) > 1
    country_quotas = (
        divide_evenly(limit, len(hierarchy))
        if (limit > 0 and divide_countries)
        else ([limit] * len(hierarchy))
    )

    for cnode, c_quota in zip(hierarchy, country_quotas):
        if limit > 0 and c_quota <= 0:
            continue

        state_nodes = sorted(cnode.states.values(), key=lambda n: n.state.lower())
        if not state_nodes:
            continue

        all_cnode_zips: list[ZipLocation] = []
        for snode in state_nodes:
            for zips in snode.cities.values():
                all_cnode_zips.extend(zips)

        if not all_cnode_zips:
            continue

        pipeline_count = calculate_optimal_pipeline_count(
            c_quota,
            len(all_cnode_zips),
            max_cap=MAX_DISCOVERY_WORKERS,
            per_zip_cap=per_zip,
        )
        pipeline_count = max(
            1, min(pipeline_count, len(state_nodes) if len(state_nodes) > 1 else pipeline_count)
        )

        p_quotas = divide_evenly(c_quota, pipeline_count) if c_quota > 0 else [0] * pipeline_count

        if len(state_nodes) >= pipeline_count:
            state_buckets = divide_evenly(len(state_nodes), pipeline_count)
            cursor = 0
            for p_idx in range(pipeline_count):
                b_size = state_buckets[p_idx]
                bucket_snodes = state_nodes[cursor : cursor + b_size]
                cursor += b_size

                bucket_zips: list[ZipLocation] = []
                for snode in bucket_snodes:
                    for zips in snode.cities.values():
                        bucket_zips.extend(zips)

                p_quota = p_quotas[p_idx]
                if c_quota > 0 and p_quota <= 0:
                    continue

                if len(bucket_snodes) == 1:
                    s_label = bucket_snodes[0].state
                    s_abbr = bucket_snodes[0].state_abbr
                else:
                    s_label = f"Group {p_idx + 1} ({bucket_snodes[0].state_abbr}..{bucket_snodes[-1].state_abbr})"
                    s_abbr = f"US-{p_idx + 1}"

                units.append(
                    GeoUnit(
                        country=cnode.country,
                        state=s_label,
                        state_abbr=s_abbr,
                        city=None,
                        quota=p_quota,
                        zips=bucket_zips,
                    )
                )
        else:
            zip_buckets = divide_evenly(len(all_cnode_zips), pipeline_count)
            cursor = 0
            for p_idx in range(pipeline_count):
                b_size = zip_buckets[p_idx]
                bucket_zips = all_cnode_zips[cursor : cursor + b_size]
                cursor += b_size
                p_quota = p_quotas[p_idx]
                if c_quota > 0 and p_quota <= 0:
                    continue
                units.append(
                    GeoUnit(
                        country=cnode.country,
                        state=f"Segment {p_idx + 1}",
                        state_abbr=f"SEG-{p_idx + 1}",
                        city=None,
                        quota=p_quota,
                        zips=bucket_zips,
                    )
                )

    for unit in units:
        random.shuffle(unit.zips)

    return [unit for unit in units if unit.zips and (limit <= 0 or unit.quota > 0)]


def preview_pipeline_plan(
    *,
    limit: int,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    cities: list[str] | None = None,
    per_zip_cap: int = DEFAULT_PER_ZIP_CAP,
) -> list[dict]:
    """Return a compact plan of pipelines and quotas for the UI."""
    units = build_scrape_units(
        limit=limit,
        countries=countries,
        states=states,
        cities=cities,
        per_zip_cap=per_zip_cap,
    )
    rows: list[dict] = []
    for unit in units:
        rows.append(
            {
                "label": unit.label(),
                "country": unit.country,
                "state": unit.state_abbr or unit.state or "",
                "city": unit.city or "",
                "quota": unit.quota,
                "zips": len(unit.zips),
            }
        )
    return sorted(rows, key=lambda item: (item["state"], item["city"], item["label"]))

