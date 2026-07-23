"""Tests for hierarchical quota distribution."""

from __future__ import annotations

from scraper.quotas import build_scrape_units, divide_evenly


def test_divide_evenly():
    assert divide_evenly(3000, 3) == [1000, 1000, 1000]
    assert divide_evenly(100, 3) == [34, 33, 33]
    assert divide_evenly(0, 4) == [0, 0, 0, 0]
    assert sum(divide_evenly(901, 7)) == 901


def test_single_country_no_state_split():
    units = build_scrape_units(
        limit=1000,
        countries=["United States"],
        states=None,
        cities=None,
    )
    assert len(units) == 1
    assert units[0].quota == 1000
    assert units[0].country == "United States"
    assert len(units[0].zips) > 100


def test_state_split():
    units = build_scrape_units(
        limit=1000,
        countries=["United States"],
        states=["California", "Texas"],
        cities=None,
    )
    state_quotas: dict[str, int] = {}
    for unit in units:
        state_quotas[unit.state] = unit.quota
    assert state_quotas == {"California": 500, "Texas": 500}
    assert sum(state_quotas.values()) == 1000


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


def test_city_split_skipped_when_below_minimum():
    units = build_scrape_units(
        limit=50,
        countries=["United States"],
        states=["Illinois"],
        cities=["Chicago", "Aurora", "Naperville"],
    )
    assert len(units) == 1
    assert units[0].quota == 50
    assert units[0].city is None


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


def test_states_keep_quota_when_cities_only_match_some():
    """Selected states without matching cities still get an equal share."""
    states = [
        "California",
        "Idaho",
        "Kansas",
        "Texas",
        "Illinois",
        "New York",
    ]
    cities = [
        "Anaheim",
        "Boise",
        "Bakersfield",
    ]
    units = build_scrape_units(
        limit=5000,
        countries=["United States"],
        states=states,
        cities=cities,
    )
    state_quotas: dict[str, int] = {}
    for unit in units:
        state_quotas[unit.state] = state_quotas.get(unit.state, 0) + unit.quota
    assert set(state_quotas) == set(states)
    assert sum(state_quotas.values()) == 5000
    assert all(quota in {833, 834} for quota in state_quotas.values())


def test_limit_zero_returns_state_units():
    units = build_scrape_units(
        limit=0,
        countries=["United States"],
        states=["Illinois"],
        cities=["Chicago"],
    )
    assert len(units) == 1
    assert units[0].quota == 0
    assert units[0].zips
    assert all(z.city == "Chicago" for z in units[0].zips)


if __name__ == "__main__":
    test_divide_evenly()
    test_single_country_no_state_split()
    test_state_split()
    test_city_split_when_quota_large_enough()
    test_city_split_skipped_when_below_minimum()
    test_state_and_city_split()
    test_states_keep_quota_when_cities_only_match_some()
    test_limit_zero_returns_state_units()
    print("All quota tests passed.")
