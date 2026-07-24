"""Data models for scraped companies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Company:
    name: str = ""
    phone: str = ""
    phone_e164: str = ""
    website: str = ""
    domain: str = ""
    rating: float | None = None
    review_count: int | None = None
    full_address: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    neighborhood: str = ""
    categories: list[str] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    place_id: str = ""
    kg_mid: str = ""
    timezone: str = ""
    cid: str = ""
    search_term: str = ""
    search_zip: str = ""
    search_city: str = ""
    search_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["categories"] = ", ".join(self.categories) if self.categories else ""
        # Keep ZIP as text so Excel/CSV don't turn 60608 into 60608.0
        d["zip_code"] = str(self.zip_code or "")
        d["search_zip"] = str(self.search_zip or "")
        return d

    def dedupe_key(self) -> str:
        if self.place_id:
            return f"pid:{self.place_id}"
        name = (self.name or "").strip().lower()
        phone = (self.phone_e164 or self.phone or "").strip()
        return f"np:{name}|{phone}"
