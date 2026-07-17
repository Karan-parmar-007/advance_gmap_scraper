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
    cities: list[str] | None = None,
    path: str | None = None,
) -> list[CountryNode]:
    """Group filtered ZIP rows as country → state → city."""
    locs = build_zip_pool(
        countries=countries,
        states=states,
        cities=cities,
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


def _collect_scope_zips(
    state_nodes: list[StateNode],
    cities_sel: list[str],
) -> list[ZipLocation]:
    zips: list[ZipLocation] = []
    if cities_sel:
        for snode in state_nodes:
            for _, city_zips in _selected_cities_in_state(snode, cities_sel):
                zips.extend(city_zips)
        return zips

    for snode in state_nodes:
        for city_zips in snode.cities.values():
            zips.extend(city_zips)
    return zips


def _scope_label(
    country: str,
    state_nodes: list[StateNode],
    city: str | None,
) -> tuple[str, str, str, str | None]:
    if city and state_nodes:
        loc = next(
            (z for snode in state_nodes for zips in snode.cities.values() for z in zips if z.city == city),
            None,
        )
        if loc:
            return country, loc.state, loc.state_abbr, city

    if len(state_nodes) == 1:
        snode = state_nodes[0]
        return country, snode.state, snode.state_abbr, city

    return country, "", "", city


def _append_city_units(
    units: list[GeoUnit],
    *,
    country: str,
    state_nodes: list[StateNode],
    quota: int,
    cities_sel: list[str],
    min_city_quota: int,
) -> None:
    if quota <= 0 or not state_nodes:
        return

    if cities_sel:
        selected: list[tuple[str, list[ZipLocation]]] = []
        for snode in state_nodes:
            selected.extend(_selected_cities_in_state(snode, cities_sel))

        if not selected:
            return

        if (
            len(cities_sel) > 1
            and len(selected) > 1
            and (quota // len(selected)) >= min_city_quota
        ):
            for (city, zips), city_quota in zip(
                selected,
                divide_evenly(quota, len(selected)),
            ):
                loc = zips[0]
                units.append(
                    GeoUnit(
                        country=country,
                        state=loc.state,
                        state_abbr=loc.state_abbr,
                        city=city,
                        quota=city_quota,
                        zips=list(zips),
                    )
                )
            return

        if len(selected) == 1:
            city, zips = selected[0]
            loc = zips[0]
            units.append(
                GeoUnit(
                    country=country,
                    state=loc.state,
                    state_abbr=loc.state_abbr,
                    city=city,
                    quota=quota,
                    zips=list(zips),
                )
            )
            return

        zips = []
        for _, city_zips in selected:
            zips.extend(city_zips)
        loc = zips[0]
        units.append(
            GeoUnit(
                country=country,
                state=loc.state,
                state_abbr=loc.state_abbr,
                city=None,
                quota=quota,
                zips=zips,
            )
        )
        return

    zips = _collect_scope_zips(state_nodes, cities_sel)
    if not zips:
        return

    country_name, state, abbr, city = _scope_label(
        country,
        state_nodes,
        None,
    )
    units.append(
        GeoUnit(
            country=country_name,
            state=state,
            state_abbr=abbr,
            city=city,
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

    if states_sel and len(states_sel) > 1:
        for snode, state_quota in zip(state_nodes, divide_evenly(quota, len(state_nodes))):
            _append_city_units(
                units,
                country=cnode.country,
                state_nodes=[snode],
                quota=state_quota,
                cities_sel=cities_sel,
                min_city_quota=min_city_quota,
            )
        return

    _append_city_units(
        units,
        country=cnode.country,
        state_nodes=state_nodes,
        quota=quota,
        cities_sel=cities_sel,
        min_city_quota=min_city_quota,
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
    """
    countries_sel = countries or []
    states_sel = states or []
    cities_sel = cities or []

    hierarchy = build_location_hierarchy(
        countries=countries or None,
        states=states or None,
        cities=cities or None,
        path=path,
    )
    if not hierarchy:
        return []

    if limit <= 0:
        pool = build_zip_pool(
            countries=countries or None,
            states=states or None,
            cities=cities or None,
            shuffle=True,
            path=path,
        )
        return [
            GeoUnit(
                country="",
                state="",
                state_abbr="",
                city=None,
                quota=0,
                zips=pool,
            )
        ]

    units: list[GeoUnit] = []
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
