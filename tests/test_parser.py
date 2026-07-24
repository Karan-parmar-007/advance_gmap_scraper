"""Parser tests with synthetic place payloads matching captured indexes."""

from __future__ import annotations

import json
from pathlib import Path

from scraper.parser import (
    extract_place_entries,
    parse_place,
    unwrap_response,
)
from scraper.models import Company


def make_place(
    name: str,
    *,
    rating: float = 4.8,
    reviews: int = 98,
    website: str = "https://example.com/",
    domain: str = "example.com",
    lat: float = 41.88,
    lng: float = -87.63,
    street: str = "123 Main St",
    city_line: str = "Chicago, IL 60602",
    phone: str = "+1 312-555-0100",
    e164: str = "+13125550100",
    place_id: str = "0x880e2cac6e03efd5:0x4a0f97b68c3969b6",
    categories: list | None = None,
) -> list:
    """Build a sparse place_data list with known indexes populated."""
    place: list = [None] * 190
    place[2] = [street, city_line, "United States"]
    place[4] = [None, None, None, [None, f"{reviews} reviews"], None, None, None, rating, reviews]
    place[7] = [website, domain]
    place[9] = [None, None, lat, lng]
    place[10] = place_id
    place[11] = name
    place[13] = categories or ["Plumber"]
    place[14] = "Loop"
    place[18] = f"{name}, {street}, {city_line}, United States"
    place[37] = [None, reviews]
    place[78] = "/g/1w2y_0k8"
    place[178] = [[phone, [[phone, 1], [phone, 2]], None, e164]]
    place[183] = [None, ["Loop", street, street, "Chicago", "60602", "Illinois", "US"]]
    return place


def test_parse_place_from_wrapped_payload() -> None:
    place = make_place("Roto-Rooter Plumbing & Water Cleanup")
    payload = [
        [
            "meta",
            [
                ["header"],
                [None, place],
            ],
        ]
    ]
    inner = json.dumps(payload, separators=(",", ":"))
    wrapped = json.dumps({"c": 0, "d": ")]}'\n" + inner, "e": "abc", "p": True}) + '/*""*/'

    decoded = unwrap_response(wrapped)
    entries = extract_place_entries(decoded)
    assert len(entries) == 1

    company = parse_place(entries[0], search_term="plumber", search_zip="60602")
    assert company is not None
    assert company.name == "Roto-Rooter Plumbing & Water Cleanup"
    assert company.phone == "+1 312-555-0100"
    assert company.phone_e164 == "+13125550100"
    assert company.website.startswith("https://")
    assert company.rating == 4.8
    assert company.review_count == 98
    assert company.city == "Chicago"
    assert company.state == "IL"
    assert company.zip_code == "60602"
    assert company.place_id.startswith("0x")


def test_review_count_from_place_37() -> None:
    place = make_place("Compact Co", reviews=0)
    place[4] = [None, None, None, None, None, None, None, 4.5]
    place[37] = [None, 42]
    company = parse_place(place)
    assert company is not None
    assert company.rating == 4.5
    assert company.review_count == 42


def test_review_count_new_shape_and_url_variant() -> None:
    place_new = make_place("New Shape Co", reviews=43)
    place_new[4] = [
        None,
        None,
        None,
        [None, "43 reviews", None, "0ahUKEwiExample"],
        None,
        None,
        None,
        5,
        43,
    ]
    place_new[37] = None
    company_new = parse_place(place_new)
    assert company_new is not None
    assert company_new.review_count == 43
    assert company_new.rating == 5.0


def test_rich_reviews_fixture() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "maps_rich_reviews.txt"
    places = extract_place_entries(unwrap_response(fixture.read_text(encoding="utf-8")))
    assert len(places) == 20
    with_reviews = [
        company_f
        for p in places
        if (company_f := parse_place(p)) and company_f.review_count
    ]
    assert len(with_reviews) == 20


def test_timezone_and_cid_from_search_shape() -> None:
    place = make_place("Attr Co")
    place[30] = "America/Chicago"
    place[181] = [None, None, None, None, None, "7253115058397360114"]
    company = parse_place(place)
    assert company is not None
    assert company.timezone == "America/Chicago"
    assert company.cid == "7253115058397360114"


if __name__ == "__main__":
    test_parse_place_from_wrapped_payload()
    test_review_count_from_place_37()
    test_review_count_new_shape_and_url_variant()
    test_rich_reviews_fixture()
    test_timezone_and_cid_from_search_shape()
    print("parser self-test OK")
