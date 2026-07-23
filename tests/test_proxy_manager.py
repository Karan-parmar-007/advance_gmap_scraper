"""Tests for DataImpulse sticky sessions and targeting."""

from __future__ import annotations

from scraper.locations import ZipLocation
from scraper.proxy_manager import (
    STICKY_PORT_MAX,
    STICKY_PORT_MIN,
    ProxyManager,
    ProxyTarget,
    country_to_code,
    normalize_filter_value,
)


def test_country_code_mapping() -> None:
    assert country_to_code("United States") == "us"
    assert country_to_code("US") == "us"
    assert country_to_code("Canada") == "ca"


def test_normalize_filter_value() -> None:
    assert normalize_filter_value("Arizona") == "arizona"
    assert normalize_filter_value("New York") == "new_york"
    assert normalize_filter_value("United States:60608") == "united_states_60608"


def test_country_targeting_username() -> None:
    manager = ProxyManager(
        login="mylogin",
        password="secret",
        enabled=True,
        mode="sticky",
        targeting="country",
        use_sessid=False,
    )
    target = ProxyTarget(country_code="us", zip_code="10001", city="new_york")
    assert manager.build_username(target=target, level="country") == "mylogin__cr.us"


def test_sticky_port_is_stable_per_zip() -> None:
    manager = ProxyManager(
        login="mylogin",
        password="secret",
        enabled=True,
        mode="sticky",
        targeting="country",
        use_sessid=True,
    )
    key = "United States:60608"
    port_a = manager.begin_sticky_session(key)
    port_b = manager.begin_sticky_session(key)
    assert STICKY_PORT_MIN <= port_a <= STICKY_PORT_MAX
    assert port_a == port_b
    assert manager.resolve_port(key) == port_a


def test_sticky_url_uses_high_port_and_sessid() -> None:
    manager = ProxyManager(
        login="mylogin",
        password="secret",
        enabled=True,
        mode="sticky",
        targeting="country",
        use_sessid=True,
    )
    loc = ZipLocation(
        zip_code="10001",
        city="New York",
        state="New York",
        state_abbr="NY",
        country="United States",
    )
    key = f"{loc.country}:{loc.zip_code}"
    port = manager.begin_sticky_session(key)
    url = manager.build_proxy_url(
        target=ProxyTarget.from_location(loc),
        level="country",
        session_key=key,
    )
    assert f"@{manager.host}:{port}" in url
    assert port >= 10000
    assert "cr.us" in url
    assert "sessid.united_states_10001" in url
    assert "zip.10001" not in url


def test_rotating_uses_823() -> None:
    manager = ProxyManager(
        login="mylogin",
        password="secret",
        enabled=True,
        mode="rotating",
        targeting="country",
        use_sessid=False,
        port=823,
    )
    url = manager.build_proxy_url(
        target=ProxyTarget(country_code="us"),
        level="country",
    )
    assert url.endswith("@gw.dataimpulse.com:823")


def test_fallback_chain() -> None:
    manager = ProxyManager(login="x", password="y", enabled=True, targeting="zip")
    assert manager.next_fallback_level("zip") == "city"
    assert manager.next_fallback_level("city") == "state"
    assert manager.next_fallback_level("state") == "country"
    assert manager.next_fallback_level("country") is None


if __name__ == "__main__":
    test_country_code_mapping()
    test_normalize_filter_value()
    test_country_targeting_username()
    test_sticky_port_is_stable_per_zip()
    test_sticky_url_uses_high_port_and_sessid()
    test_rotating_uses_823()
    test_fallback_chain()
    print("All sticky proxy tests passed.")
