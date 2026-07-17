"""Load and filter zip/pincode locations from location_pincodes.json."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import LOCATION_JSON_PATH, ROOT_DIR


@dataclass(frozen=True)
class ZipLocation:
    zip_code: str
    city: str
    state: str
    state_abbr: str
    country: str = "United States"


def _resolve_json_path(path: str | Path | None = None) -> Path:
    if path:
        p = Path(path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Location JSON not found: {p}")

    if LOCATION_JSON_PATH.exists():
        return LOCATION_JSON_PATH

    alt = ROOT_DIR / "location_pincodes.json"
    if alt.exists():
        return alt

    raise FileNotFoundError(
        f"Location JSON not found. Expected {LOCATION_JSON_PATH} or {alt}"
    )


@lru_cache(maxsize=1)
def load_locations(path: str | None = None) -> tuple[ZipLocation, ...]:
    """Load all zip/city/state rows from location_pincodes.json."""
    json_path = _resolve_json_path(path)
    with json_path.open(encoding="utf-8") as f:
        data = json.load(f)

    rows: list[ZipLocation] = []
    for country in data.get("countries", []):
        country_name = str(country.get("country", "")).strip()
        for state in country.get("states", []):
            state_name = str(state.get("state", "")).strip()
            state_code = str(state.get("state_code", "")).strip().upper()
            for city_entry in state.get("cities", []):
                city_name = str(city_entry.get("city", "")).strip()
                for pin in city_entry.get("pincodes", []):
                    rows.append(
                        ZipLocation(
                            zip_code=str(pin).strip().zfill(5),
                            city=city_name,
                            state=state_name,
                            state_abbr=state_code,
                            country=country_name,
                        )
                    )

    # Deduplicate zip+city+state
    seen: set[tuple[str, str, str]] = set()
    unique: list[ZipLocation] = []
    for loc in rows:
        key = (loc.zip_code, loc.city.lower(), loc.state.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(loc)

    return tuple(unique)


def list_countries(path: str | None = None) -> list[str]:
    json_path = _resolve_json_path(path)
    with json_path.open(encoding="utf-8") as f:
        data = json.load(f)
    return sorted(
        {str(c.get("country", "")).strip() for c in data.get("countries", []) if c.get("country")}
    )


def list_states(path: str | None = None) -> list[str]:
    locs = load_locations(path)
    return sorted({loc.state for loc in locs if loc.state})


def list_state_abbrs(path: str | None = None) -> list[str]:
    locs = load_locations(path)
    return sorted({loc.state_abbr for loc in locs if loc.state_abbr})


def list_cities(states: list[str] | None = None, path: str | None = None) -> list[str]:
    locs = load_locations(path)
    if states:
        states_norm = {s.strip().lower() for s in states}
        abbrs = {s.strip().upper() for s in states if len(s.strip()) <= 2}
        locs = [
            loc
            for loc in locs
            if loc.state.lower() in states_norm or loc.state_abbr in abbrs
        ]
    return sorted({loc.city for loc in locs if loc.city})


def build_zip_pool(
    *,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    cities: list[str] | None = None,
    shuffle: bool = True,
    path: str | None = None,
) -> list[ZipLocation]:
    """
    Build candidate zip list from location_pincodes.json.

    Filters (AND when multiple given):
      - countries: match country name (multi allowed)
      - states: filter by state name or abbr (multi allowed)
      - cities: filter by city (multi allowed)
    """
    locs = list(load_locations(path))

    if countries:
        allowed = {c.strip().lower() for c in countries}
        locs = [loc for loc in locs if loc.country.lower() in allowed]

    if states:
        states_norm = {s.strip().lower() for s in states}
        abbrs = {s.strip().upper() for s in states}
        locs = [
            loc
            for loc in locs
            if loc.state.lower() in states_norm or loc.state_abbr in abbrs
        ]

    if cities:
        cities_norm = {c.strip().lower() for c in cities}
        locs = [loc for loc in locs if loc.city.lower() in cities_norm]

    if shuffle:
        random.shuffle(locs)
    return locs
