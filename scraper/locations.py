"""Load and filter zip codes from the Excel location database."""

from __future__ import annotations

import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .config import ZIP_EXCEL_PATH


@dataclass(frozen=True)
class ZipLocation:
    zip_code: str
    city: str
    state: str
    state_abbr: str


@lru_cache(maxsize=1)
def load_zip_dataframe(path: str | None = None) -> pd.DataFrame:
    excel = Path(path) if path else ZIP_EXCEL_PATH
    if not excel.exists():
        # Fall back to root copy
        alt = excel.parent.parent / "All Segment Zip Combinations.xlsx"
        if alt.exists():
            excel = alt
        else:
            raise FileNotFoundError(f"Zip Excel not found: {excel}")

    df = pd.read_excel(excel, dtype={"Zip Code": str})
    df.columns = [str(c).strip() for c in df.columns]

    # Normalize
    rename = {}
    for col in df.columns:
        low = col.lower()
        if low in ("zip code", "zip", "pincode", "postal"):
            rename[col] = "zip_code"
        elif low == "city":
            rename[col] = "city"
        elif low == "state":
            rename[col] = "state"
        elif low in ("state abbr", "state_abbr", "abbr"):
            rename[col] = "state_abbr"
    df = df.rename(columns=rename)

    required = {"zip_code", "city", "state"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Excel missing columns: {missing}")

    if "state_abbr" not in df.columns:
        df["state_abbr"] = ""

    df["zip_code"] = df["zip_code"].astype(str).str.strip().str.zfill(5)
    df["city"] = df["city"].astype(str).str.strip()
    df["state"] = df["state"].astype(str).str.strip()
    df["state_abbr"] = df["state_abbr"].astype(str).str.strip().str.upper()

    # Unique location rows (ignore search-phrase duplicates)
    loc = (
        df[["zip_code", "city", "state", "state_abbr"]]
        .drop_duplicates(subset=["zip_code", "city", "state"])
        .reset_index(drop=True)
    )
    return loc


def list_states(path: str | None = None) -> list[str]:
    df = load_zip_dataframe(path)
    return sorted(df["state"].dropna().unique().tolist())


def list_state_abbrs(path: str | None = None) -> list[str]:
    df = load_zip_dataframe(path)
    return sorted([a for a in df["state_abbr"].dropna().unique().tolist() if a])


def list_cities(states: list[str] | None = None, path: str | None = None) -> list[str]:
    df = load_zip_dataframe(path)
    if states:
        states_norm = {s.strip().lower() for s in states}
        abbrs = {s.strip().upper() for s in states if len(s.strip()) == 2}
        mask = df["state"].str.lower().isin(states_norm) | df["state_abbr"].isin(abbrs)
        df = df[mask]
    return sorted(df["city"].dropna().unique().tolist())


def list_countries() -> list[str]:
    """Currently the Excel is US-only; keep list API for future DBs."""
    return ["United States"]


def build_zip_pool(
    *,
    countries: list[str] | None = None,
    states: list[str] | None = None,
    cities: list[str] | None = None,
    shuffle: bool = True,
    path: str | None = None,
) -> list[ZipLocation]:
    """
    Build candidate zip list.

    Priority / filters (AND when multiple given):
      - countries: currently only US supported; empty = all
      - states: filter by state name or abbr (multi allowed)
      - cities: filter by city (multi allowed); if states also given, both apply
    """
    # Country gate (future multi-country)
    if countries:
        allowed = {c.strip().lower() for c in countries}
        us_aliases = {"united states", "usa", "us", "america"}
        if not (allowed & us_aliases) and allowed:
            # Non-US requested with no data — empty pool
            return []

    df = load_zip_dataframe(path)

    if states:
        states_norm = {s.strip().lower() for s in states}
        abbrs = {s.strip().upper() for s in states if len(s.strip()) <= 2}
        # Also map full names
        mask = df["state"].str.lower().isin(states_norm) | df["state_abbr"].isin(
            {s.strip().upper() for s in states}
        )
        # include abbr matches for 2-letter
        if abbrs:
            mask = mask | df["state_abbr"].isin(abbrs)
        df = df[mask]

    if cities:
        cities_norm = {c.strip().lower() for c in cities}
        df = df[df["city"].str.lower().isin(cities_norm)]

    rows = [
        ZipLocation(
            zip_code=str(r.zip_code),
            city=str(r.city),
            state=str(r.state),
            state_abbr=str(r.state_abbr),
        )
        for r in df.itertuples(index=False)
    ]

    # Deduplicate by zip primarily (same zip may appear under one city)
    seen: set[str] = set()
    unique: list[ZipLocation] = []
    for loc in rows:
        key = f"{loc.zip_code}|{loc.city.lower()}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(loc)

    if shuffle:
        random.shuffle(unique)
    return unique
