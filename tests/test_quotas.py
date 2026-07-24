"""Tests for hierarchical quota distribution."""

from __future__ import annotations

from scraper.config import auto_discovery_workers
from scraper.quotas import build_scrape_units, divide_evenly, preview_pipeline_plan


def test_divide_evenly():
    assert divide_evenly(3000, 3) == [1000, 1000, 1000]
    assert divide_evenly(100, 3) == [34, 33, 33]
    assert divide_evenly(0, 4) == [0, 0, 0, 0]
    assert sum(divide_evenly(901, 7)) == 901


def test_country_only_auto_splits_states():
    """Country-only selection fans out into multiple state/city pipelines."""
    units = build_scrape_units(
        limit=1000,
        countries=["United States"],
        states=None,
        cities=None,
    )
    assert len(units) > 1
    assert sum(u.quota for u in units) == 1000
    assert all(u.country == "United States" for u in units)
    # Should cover many states rather than one giant country unit.
    states = {u.state for u in units if u.state}
    assert len(states) >= 10


def test_single_state_fans_out_like_country():
    """One state fans out into ceil(limit / per_zip_cap) ZIP-bucket pipelines."""
    units = build_scrape_units(
        limit=500,
        countries=["United States"],
        states=["Illinois"],
        cities=None,
        per_zip_cap=50,
    )
    assert len(units) == 10
    assert sum(u.quota for u in units) == 500
    assert all(u.quota == 50 for u in units)
    assert all(u.city is None for u in units)
    assert all(u.zips for u in units)
    assert all("Illinois" in u.state for u in units)


def test_single_state_one_pipeline_when_per_zip_covers_limit():
    """per_zip_cap >= limit → one pipeline with the full state ZIP pool."""
    units = build_scrape_units(
        limit=100,
        countries=["United States"],
        states=["Illinois"],
        cities=None,
        per_zip_cap=100,
    )
    assert len(units) == 1
    assert units[0].city is None
    assert units[0].state == "Illinois"
    assert units[0].quota == 100
    assert len(units[0].zips) > 10


def test_state_split():
    units = build_scrape_units(
        limit=500,
        countries=["United States"],
        states=["California", "Texas"],
        cities=None,
        per_zip_cap=50,
    )
    # 250 per state → 5 pipelines each → 10 total
    assert len(units) == 10
    state_quotas: dict[str, int] = {}
    for unit in units:
        real_state = unit.zips[0].state
        state_quotas[real_state] = state_quotas.get(real_state, 0) + unit.quota
    assert state_quotas == {"California": 250, "Texas": 250}
    assert sum(state_quotas.values()) == 500
    assert all(u.quota == 50 for u in units)

def test_city_split_when_quota_large_enough():
    units = build_scrape_units(
        limit=300,
        countries=["United States"],
        states=["Illinois"],
        cities=["Chicago", "Aurora", "Naperville"],
    )
    city_units = [u for u in units if u.city]
    assert len(city_units) == 3
    assert sorted(u.quota for u in city_units) == [100, 100, 100]


def test_city_split_for_small_limits():
    units = build_scrape_units(
        limit=50,
        countries=["United States"],
        states=["Illinois"],
        cities=["Chicago", "Aurora", "Naperville"],
    )
    city_units = [u for u in units if u.city]
    assert len(city_units) == 3
    assert sum(u.quota for u in city_units) == 50


def test_state_and_city_split():
    units = build_scrape_units(
        limit=600,
        countries=["United States"],
        states=["Illinois", "Massachusetts"],
        cities=["Chicago", "Boston"],
    )
    labels = {unit.label(): unit.quota for unit in units}
    assert labels == {
        "Chicago, IL (United States)": 300,
        "Boston, MA (United States)": 300,
    }


def test_city_filter_matches_cities():
    """Explicit city filter filters to matching cities."""
    cities = [
        "Anaheim",
        "Boise",
        "Bakersfield",
    ]
    units = build_scrape_units(
        limit=3000,
        countries=["United States"],
        states=None,
        cities=cities,
    )
    found_cities = {u.city for u in units}
    assert found_cities == set(cities)
    assert sum(u.quota for u in units) == 3000


def test_limit_zero_with_city_filter():
    units = build_scrape_units(
        limit=0,
        countries=["United States"],
        states=["Illinois"],
        cities=["Chicago"],
    )
    assert len(units) == 1
    assert units[0].quota == 0
    assert units[0].city == "Chicago"
    assert units[0].zips
    assert all(z.city == "Chicago" for z in units[0].zips)


def test_limit_zero_state_pooled():
    units = build_scrape_units(
        limit=0,
        countries=["United States"],
        states=["Illinois"],
        cities=None,
    )
    assert len(units) == 1
    assert units[0].city is None
    assert units[0].quota == 0
    assert units[0].zips


def test_auto_workers_use_per_zip_cap() -> None:
    assert auto_discovery_workers(500, 100, per_zip_cap=50) == 10
    assert auto_discovery_workers(500, 100, per_zip_cap=20) == 25
    assert auto_discovery_workers(500, 8, per_zip_cap=50) == 8


def test_country_only_respects_per_zip_cap() -> None:
    units_20 = build_scrape_units(
        limit=500,
        countries=["United States"],
        per_zip_cap=20,
    )
    units_50 = build_scrape_units(
        limit=500,
        countries=["United States"],
        per_zip_cap=50,
    )
    assert len(units_50) < len(units_20)
    assert len(units_50) == 10
    assert sum(u.quota for u in units_50) == 500
    assert all(u.quota == 50 for u in units_50)


def test_preview_shows_city_rows():
    plan = preview_pipeline_plan(
        limit=100,
        countries=["United States"],
        states=["Illinois"],
        cities=["Chicago", "Aurora"],
    )
    assert len(plan) == 2
    assert {row["city"] for row in plan} == {"Chicago", "Aurora"}
    assert sum(row["quota"] for row in plan) == 100


def test_preview_state_only_shows_state_pipelines():
    plan = preview_pipeline_plan(
        limit=100,
        countries=["United States"],
        states=["Illinois"],
        cities=None,
        per_zip_cap=20,
    )
    assert len(plan) == 5
    assert sum(row["quota"] for row in plan) == 100
    assert all(row["state"].startswith("IL") for row in plan)

if __name__ == "__main__":
    test_divide_evenly()
    test_country_only_auto_splits_states()
    test_single_state_auto_splits_cities()
    test_state_split()
    test_city_split_when_quota_large_enough()
    test_city_split_for_small_limits()
    test_state_and_city_split()
    test_states_keep_quota_when_cities_only_match_some()
    test_limit_zero_with_city_filter()
    test_limit_zero_state_fans_out_cities()
    test_preview_shows_city_rows()
    test_preview_state_only_shows_multiple_pipelines()
    print("All quota tests passed.")
