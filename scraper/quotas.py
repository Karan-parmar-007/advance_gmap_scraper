"""Hierarchical quota distribution for multi-region scrapes."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import MIN_CITY_QUOTA
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


def build_location_hierarchy(
    *,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    path: str | None = None,
) -> list[CountryNode]:
    """
    Group ZIP rows as country → state → city.

    City filters are applied later per-state so a selected state is never
    dropped just because none of the selected cities live in it.
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


def _cities_for_state(
    snode: StateNode,
    cities_sel: list[str],
) -> list[tuple[str, list[ZipLocation]]]:
    """
    Apply city filter only when it matches cities inside this state.

    If the user selected cities from other states only, keep ALL cities in
    this state so the state pipeline still runs.
    """
    all_cities = [
        (city, zips)
        for city, zips in sorted(snode.cities.items(), key=lambda item: item[0].lower())
    ]
    if not cities_sel:
        return all_cities
    matched = _selected_cities_in_state(snode, cities_sel)
    return matched if matched else all_cities


def _append_city_units(
    units: list[GeoUnit],
    *,
    country: str,
    snode: StateNode,
    quota: int,
    cities_sel: list[str],
    min_city_quota: int,
) -> None:
    if quota <= 0:
        return

    selected = _cities_for_state(snode, cities_sel)
    if not selected:
        return

    # Split within this state only when every city would get a meaningful quota.
    if len(selected) > 1 and (quota // len(selected)) >= min_city_quota:
        for (city, zips), city_quota in zip(
            selected,
            divide_evenly(quota, len(selected)),
        ):
            units.append(
                GeoUnit(
                    country=country,
                    state=snode.state,
                    state_abbr=snode.state_abbr,
                    city=city,
                    quota=city_quota,
                    zips=list(zips),
                )
            )
        return

    if len(selected) == 1:
        city, zips = selected[0]
        units.append(
            GeoUnit(
                country=country,
                state=snode.state,
                state_abbr=snode.state_abbr,
                city=city if cities_sel else None,
                quota=quota,
                zips=list(zips),
            )
        )
        return

    zips: list[ZipLocation] = []
    for _, city_zips in selected:
        zips.extend(city_zips)
    units.append(
        GeoUnit(
            country=country,
            state=snode.state,
            state_abbr=snode.state_abbr,
            city=None,
            quota=quota,
            zips=zips,
        )
    )


def _append_country_units(
    units: list[GeoUnit],
    *,
    cnode: CountryNode,
    quota: int,
    states_sel: list[str],
    cities_sel: list[str],
    min_city_quota: int,
) -> None:
    if quota <= 0:
        return

    state_nodes = sorted(cnode.states.values(), key=lambda n: n.state.lower())
    if not state_nodes:
        return

    # Multiple selected states → one equal share / pipeline per state.
    if states_sel and len(state_nodes) > 1:
        for snode, state_quota in zip(
            state_nodes,
            divide_evenly(quota, len(state_nodes)),
        ):
            _append_city_units(
                units,
                country=cnode.country,
                snode=snode,
                quota=state_quota,
                cities_sel=cities_sel,
                min_city_quota=min_city_quota,
            )
        return

    # One selected state (or hierarchy collapsed to one).
    if len(state_nodes) == 1:
        _append_city_units(
            units,
            country=cnode.country,
            snode=state_nodes[0],
            quota=quota,
            cities_sel=cities_sel,
            min_city_quota=min_city_quota,
        )
        return

    # Country selected with no state multi-filter: one combined unit.
    zips: list[ZipLocation] = []
    for snode in state_nodes:
        for _, city_zips in _cities_for_state(snode, cities_sel):
            zips.extend(city_zips)
    if not zips:
        return
    units.append(
        GeoUnit(
            country=cnode.country,
            state="",
            state_abbr="",
            city=None,
            quota=quota,
            zips=zips,
        )
    )


def build_scrape_units(
    *,
    limit: int,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    cities: list[str] | None = None,
    min_city_quota: int = MIN_CITY_QUOTA,
    path: str | None = None,
) -> list[GeoUnit]:
    """
    Build geographic scrape units with per-unit quotas.

    When *limit* is 0, returns one unit with quota 0 (unlimited flat scrape).
    When *limit* > 0, divides evenly across selected countries, then states,
    then cities (city split only if each city would get at least *min_city_quota*).

    City filters narrow ZIPs inside a state when matches exist; states without
    matching cities still keep their full ZIP pool and their equal quota share.
    """
    countries_sel = countries or []
    states_sel = states or []
    cities_sel = cities or []

    hierarchy = build_location_hierarchy(
        countries=countries or None,
        states=states or None,
        path=path,
    )
    if not hierarchy:
        return []

    if limit <= 0:
        # Unlimited: still respect optional city narrowing where it matches.
        units: list[GeoUnit] = []
        for cnode in hierarchy:
            for snode in sorted(cnode.states.values(), key=lambda n: n.state.lower()):
                selected = _cities_for_state(snode, cities_sel)
                zips: list[ZipLocation] = []
                for _, city_zips in selected:
                    zips.extend(city_zips)
                if not zips:
                    continue
                units.append(
                    GeoUnit(
                        country=cnode.country,
                        state=snode.state,
                        state_abbr=snode.state_abbr,
                        city=None,
                        quota=0,
                        zips=zips,
                    )
                )
        for unit in units:
            random.shuffle(unit.zips)
        return units

    units = []
    divide_countries = bool(countries_sel and len(countries_sel) > 1 and len(hierarchy) > 1)
    if divide_countries:
        country_quotas = divide_evenly(limit, len(hierarchy))
    else:
        country_quotas = [limit] * len(hierarchy)

    for cnode, country_quota in zip(hierarchy, country_quotas):
        _append_country_units(
            units,
            cnode=cnode,
            quota=country_quota,
            states_sel=states_sel,
            cities_sel=cities_sel,
            min_city_quota=min_city_quota,
        )

    for unit in units:
        random.shuffle(unit.zips)

    return [unit for unit in units if unit.zips]


def preview_pipeline_plan(
    *,
    limit: int,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    cities: list[str] | None = None,
) -> list[dict]:
    """Return a compact plan of state pipelines and quotas for the UI."""
    units = build_scrape_units(
        limit=limit,
        countries=countries,
        states=states,
        cities=cities,
    )
    by_state: dict[tuple[str, str], dict] = {}
    for unit in units:
        key = (unit.country, unit.state or unit.label())
        row = by_state.setdefault(
            key,
            {
                "country": unit.country,
                "state": unit.state_abbr or unit.state or unit.label(),
                "quota": 0,
                "zips": 0,
                "cities": 0,
            },
        )
        row["quota"] += unit.quota
        row["zips"] += len(unit.zips)
        row["cities"] += 1 if unit.city else len({z.city for z in unit.zips})
    return sorted(by_state.values(), key=lambda item: item["state"])
