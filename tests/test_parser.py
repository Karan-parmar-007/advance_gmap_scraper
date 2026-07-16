"""Quick parser self-test with a synthetic place payload matching captured indexes."""

from __future__ import annotations

import json
import sys

from scraper.parser import extract_place_entries, parse_place, unwrap_response


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
    place[78] = "/g/1w2y_0k8"
    place[178] = [[phone, [[phone, 1], [phone, 2]], None, e164]]
    place[183] = [None, ["Loop", street, street, "Chicago", "60602", "Illinois", "US"]]
    return place


def main() -> int:
    place = make_place("Roto-Rooter Plumbing & Water Cleanup")
    # Wrap like Google: payload[0][1] = [meta, [null, place], ...]
    payload = [[None, [None, [None, place]]]]
    # Simpler: payload[0][1] = [meta_entry, place_entry]
    payload = [
        [
            "meta",
            [
                ["header"],
                [None, place],
            ],
        ]
    ]

    # Also test wrapper unwrap
    inner = json.dumps(payload, separators=(",", ":"))
    wrapped = json.dumps({"c": 0, "d": ")]}'\n" + inner, "e": "abc", "p": True}) + '/*""*/'

    decoded = unwrap_response(wrapped)
    entries = extract_place_entries(decoded)
    assert len(entries) == 1, f"expected 1 place, got {len(entries)}"

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
    print("parser self-test OK")
    print(company.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
