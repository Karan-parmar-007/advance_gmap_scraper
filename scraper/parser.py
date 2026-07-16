"""Parse Google Maps tbm=map JSON responses into Company objects."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .models import Company

PHONE_RE = re.compile(r"^\+?[\d\s().\-]{7,}$")
E164_RE = re.compile(r"^\+\d{8,15}$")
PLACE_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+:0x[0-9a-fA-F]+$")
KG_MID_RE = re.compile(r"^/g/[0-9a-zA-Z_]+$")
URL_RE = re.compile(r"^https?://", re.I)


def safe_get(data: Any, *path: int | str, default: Any = None) -> Any:
    cur = data
    for key in path:
        try:
            cur = cur[key]
        except (IndexError, KeyError, TypeError):
            return default
    return cur if cur is not None else default


def unwrap_response(raw_text: str) -> Any:
    """
    Unwrap Google's tch=1 wrapper:
      {"c":0,"d":")]}'\\n[[...]]","e":"...","u":"..."}/*""*/
    or a bare )]}' prefixed array.
    """
    text = (raw_text or "").strip()
    if text.endswith('/*""*/'):
        text = text[: -len('/*""*/')].rstrip()

    # Try wrapper JSON first
    if text.startswith("{"):
        try:
            wrapper = json.loads(text)
            inner = wrapper.get("d")
            if isinstance(inner, str):
                return _parse_inner_payload(inner)
            return wrapper
        except json.JSONDecodeError:
            pass

    return _parse_inner_payload(text)


def _parse_inner_payload(text: str) -> Any:
    text = text.strip()
    if text.startswith(")]}'"):
        text = text[4:].lstrip("\n\r")
    return json.loads(text)


def _is_place_data(node: Any) -> bool:
    if not isinstance(node, list) or len(node) < 12:
        return False
    name = safe_get(node, 11)
    pid = safe_get(node, 10)
    if isinstance(name, str) and name.strip() and PLACE_HEX_RE.match(str(pid or "")):
        return True
    return False


def _places_from_block(block: Any) -> list[Any]:
    """Extract place_data lists from a results block of [null, place_data] entries."""
    places: list[Any] = []
    if not isinstance(block, list):
        return places
    for entry in block:
        if not isinstance(entry, list):
            continue
        # Shape: [null, place_data]
        if len(entry) >= 2 and entry[0] is None and _is_place_data(entry[1]):
            places.append(entry[1])
        elif _is_place_data(entry):
            places.append(entry)
    return places


def extract_place_entries(payload: Any) -> list[Any]:
    """
    Return list of place_data arrays from the decoded Maps payload.

    Live responses typically put local results at payload[64] as
    [[null, place_data], ...]. Older/alternate layouts used payload[0][1].
    """
    if not isinstance(payload, list):
        return []

    # Primary: scan top-level indexes for the densest place block
    best: list[Any] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, list) or len(item) < 1:
            continue
        # Skip tiny/meta blocks; local SERP is usually 5–20 entries
        found = _places_from_block(item)
        if len(found) > len(best):
            best = found

    if best:
        return best

    # Fallback: payload[0][1] skipping header row
    results_block = safe_get(payload, 0, 1)
    if isinstance(results_block, list):
        found = _places_from_block(results_block[1:] if len(results_block) > 1 else results_block)
        if found:
            return found

    # Last resort: recursive scan
    places: list[Any] = []
    seen: set[int] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(places) >= 40:
            return
        if isinstance(node, list):
            if _is_place_data(node):
                key = id(node)
                if key not in seen:
                    seen.add(key)
                    places.append(node)
                return
            if len(node) >= 2 and node[0] is None and _is_place_data(node[1]):
                key = id(node[1])
                if key not in seen:
                    seen.add(key)
                    places.append(node[1])
                return
            for child in node[:120]:
                walk(child, depth + 1)

    walk(payload)
    return places


def _extract_phone(place: Any) -> tuple[str, str]:
    # Primary path from captured payload: place[178][0]
    phone_block = safe_get(place, 178, 0)
    if isinstance(phone_block, list):
        formatted = safe_get(phone_block, 0) or ""
        e164 = safe_get(phone_block, 3) or ""
        if isinstance(formatted, str) and PHONE_RE.match(formatted.strip()):
            return formatted.strip(), (e164.strip() if isinstance(e164, str) else "")
        # sometimes nested [[formatted, ...], ...]
        nested = safe_get(phone_block, 0, 0)
        if isinstance(nested, str) and PHONE_RE.match(nested.strip()):
            e1642 = safe_get(phone_block, 0, 3) or safe_get(phone_block, 3) or ""
            return nested.strip(), (e1642.strip() if isinstance(e1642, str) else "")

    # Fallback: scan for tel: links / E.164
    found_fmt, found_e164 = "", ""

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal found_fmt, found_e164
        if depth > 12 or (found_fmt and found_e164):
            return
        if isinstance(node, str):
            if node.startswith("tel:"):
                found_e164 = found_e164 or node[4:].strip()
            elif E164_RE.match(node.strip()):
                found_e164 = found_e164 or node.strip()
            elif PHONE_RE.match(node.strip()) and ("(" in node or node.startswith("+")):
                found_fmt = found_fmt or node.strip()
        elif isinstance(node, list):
            for item in node[:40]:
                walk(item, depth + 1)

    walk(place)
    return found_fmt, found_e164


def _extract_website(place: Any) -> tuple[str, str]:
    url = safe_get(place, 7, 0)
    domain = safe_get(place, 7, 1)
    if isinstance(url, str) and URL_RE.match(url):
        return url, (domain if isinstance(domain, str) else "")
    # Fallback scan
    found_url, found_domain = "", ""

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal found_url, found_domain
        if depth > 10 or found_url:
            return
        if isinstance(node, list) and len(node) >= 2:
            if isinstance(node[0], str) and URL_RE.match(node[0]) and isinstance(node[1], str):
                # Prefer business websites over googleusercontent / google.com
                u = node[0]
                if "google." not in u and "gstatic." not in u:
                    found_url = u
                    found_domain = node[1]
                    return
        if isinstance(node, list):
            for item in node[:30]:
                walk(item, depth + 1)

    walk(place)
    return found_url, found_domain


def _extract_rating(place: Any) -> tuple[float | None, int | None]:
    rating = safe_get(place, 4, 7)
    reviews = safe_get(place, 4, 8)
    # Alternate: text like "345 reviews" at [4][3][1]
    if reviews is None:
        review_text = safe_get(place, 4, 3, 1)
        if isinstance(review_text, str):
            m = re.search(r"([\d,]+)", review_text.replace("\u202f", "").replace(",", ""))
            if m:
                try:
                    reviews = int(m.group(1).replace(",", ""))
                except ValueError:
                    reviews = None

    # Some responses only include the float rating under [4][7]
    if rating is None:
        block = safe_get(place, 4)
        if isinstance(block, list):
            for item in block:
                if isinstance(item, (int, float)) and 0 < float(item) <= 5:
                    rating = item
                    break

    # Scan for "N reviews" anywhere shallow if still missing
    if reviews is None:
        for s in _find_strings(place, max_depth=5, limit=80):
            m = re.search(r"^([\d,]+)\s+reviews?$", s.replace("\u202f", ""), re.I)
            if m:
                try:
                    reviews = int(m.group(1).replace(",", ""))
                    break
                except ValueError:
                    pass

    try:
        rating_f = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating_f = None
    try:
        reviews_i = int(reviews) if reviews is not None else None
    except (TypeError, ValueError):
        reviews_i = None
    return rating_f, reviews_i


def _extract_address(place: Any) -> dict[str, str]:
    lines = safe_get(place, 2)
    street = city = state = zip_code = neighborhood = full = ""

    if isinstance(lines, list) and lines:
        street = str(lines[0] or "")
        if len(lines) >= 2:
            # "Chicago, IL 60622"
            city_line = str(lines[1] or "")
            m = re.match(r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", city_line)
            if m:
                city, state, zip_code = m.group(1), m.group(2), m.group(3)
            else:
                city = city_line

    # Components: [neighborhood, street, street, city, zip, state, country]
    comps = safe_get(place, 183, 1)
    if isinstance(comps, list):
        if isinstance(safe_get(comps, 0), str) and safe_get(comps, 0):
            neighborhood = comps[0]
        if not street and isinstance(safe_get(comps, 1), str):
            street = comps[1]
        if not city and isinstance(safe_get(comps, 3), str):
            city = comps[3]
        if not zip_code and isinstance(safe_get(comps, 4), str):
            zip_code = str(comps[4])
        if not state and isinstance(safe_get(comps, 5), str):
            state = comps[5]

    full = safe_get(place, 18) or ""
    if not isinstance(full, str):
        full = ""
    # Sometimes full starts with business name — still useful
    if not full and street:
        parts = [street]
        if city and state and zip_code:
            parts.append(f"{city}, {state} {zip_code}")
        elif city:
            parts.append(city)
        full = ", ".join(parts)

    return {
        "street": street,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "neighborhood": neighborhood,
        "full_address": full,
    }


def _extract_coords(place: Any) -> tuple[float | None, float | None]:
    lat = safe_get(place, 9, 2)
    lng = safe_get(place, 9, 3)
    try:
        lat_f = float(lat) if lat is not None else None
        lng_f = float(lng) if lng is not None else None
    except (TypeError, ValueError):
        return None, None
    # Sanity check
    if lat_f is not None and not (-90 <= lat_f <= 90):
        lat_f = None
    if lng_f is not None and not (-180 <= lng_f <= 180):
        lng_f = None
    return lat_f, lng_f


def _extract_hours(place: Any) -> str:
    """Best-effort hours summary string."""
    # Look for blocks containing day names
    days = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
    snippets: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 14 or len(snippets) >= 7:
            return
        if isinstance(node, list) and len(node) >= 4:
            day = node[0]
            if isinstance(day, str) and day in days:
                # node[3] often [["Open 24 hours", ...]] or [["Closed"]]
                status = safe_get(node, 3, 0, 0)
                if isinstance(status, str):
                    snippets.append(f"{day}: {status}")
                    return
        if isinstance(node, list):
            for item in node[:50]:
                walk(item, depth + 1)

    walk(place)
    # Prefer status line if present
    status_line = ""

    def walk_status(node: Any, depth: int = 0) -> None:
        nonlocal status_line
        if depth > 12 or status_line:
            return
        if isinstance(node, str) and (
            node.startswith("Open") or node.startswith("Closed") or "·" in node
        ):
            if len(node) < 80 and ("Open" in node or "Closed" in node):
                status_line = node
                return
        if isinstance(node, list):
            for item in node[:40]:
                walk_status(item, depth + 1)

    walk_status(place)
    if snippets:
        return "; ".join(snippets[:7])
    return status_line


def parse_place(
    place: Any,
    *,
    search_term: str = "",
    search_zip: str = "",
    search_city: str = "",
    search_state: str = "",
) -> Company | None:
    name = safe_get(place, 11)
    if not isinstance(name, str) or not name.strip():
        # Fallback: look for name near place_id
        name = ""
        place_id_probe = safe_get(place, 10)
        if not PLACE_HEX_RE.match(str(place_id_probe or "")):
            return None

    place_id = safe_get(place, 10) or ""
    if not isinstance(place_id, str):
        place_id = str(place_id) if place_id else ""

    # kg mid moved between indexes across response versions (78 vs 89)
    kg_mid = ""
    for idx in (89, 78, 77, 90):
        candidate = safe_get(place, idx)
        if isinstance(candidate, str) and KG_MID_RE.match(candidate):
            kg_mid = candidate
            break
    if not kg_mid:
        for candidate in _find_strings(place, max_depth=6, limit=120):
            if KG_MID_RE.match(candidate):
                kg_mid = candidate
                break

    categories = safe_get(place, 13) or []
    if not isinstance(categories, list):
        categories = []
    categories = [c for c in categories if isinstance(c, str)]

    phone, phone_e164 = _extract_phone(place)
    website, domain = _extract_website(place)
    rating, review_count = _extract_rating(place)
    addr = _extract_address(place)
    lat, lng = _extract_coords(place)
    hours = _extract_hours(place)

    if not name:
        # Last resort: full address field sometimes starts with name
        full = addr.get("full_address") or ""
        if full and "," in full:
            name = full.split(",")[0].strip()
        if not name:
            return None

    return Company(
        name=name.strip(),
        phone=phone,
        phone_e164=phone_e164,
        website=website,
        domain=domain,
        rating=rating,
        review_count=review_count,
        full_address=addr["full_address"],
        street=addr["street"],
        city=addr["city"],
        state=addr["state"],
        zip_code=addr["zip_code"],
        neighborhood=addr["neighborhood"],
        categories=categories,
        latitude=lat,
        longitude=lng,
        place_id=place_id,
        kg_mid=kg_mid,
        hours_text=hours,
        search_term=search_term,
        search_zip=search_zip,
        search_city=search_city,
        search_state=search_state,
    )


def _find_strings(node: Any, max_depth: int = 8, limit: int = 200) -> list[str]:
    out: list[str] = []

    def walk(n: Any, depth: int) -> None:
        if depth > max_depth or len(out) >= limit:
            return
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, list):
            for item in n[:60]:
                walk(item, depth + 1)

    walk(node, 0)
    return out


def parse_response(
    raw_text: str,
    *,
    search_term: str = "",
    search_zip: str = "",
    search_city: str = "",
    search_state: str = "",
    per_zip_cap: int = 20,
    debug_dir: Path | None = None,
) -> list[Company]:
    try:
        payload = unwrap_response(raw_text)
    except Exception as exc:
        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            dump = debug_dir / f"parse_fail_{search_zip or 'nozips'}.txt"
            dump.write_text(raw_text[:200_000], encoding="utf-8", errors="replace")
            (debug_dir / f"parse_fail_{search_zip or 'nozips'}.err").write_text(
                str(exc), encoding="utf-8"
            )
        raise

    places = extract_place_entries(payload)
    companies: list[Company] = []
    for place in places:
        company = parse_place(
            place,
            search_term=search_term,
            search_zip=search_zip,
            search_city=search_city,
            search_state=search_state,
        )
        if company:
            companies.append(company)
        if len(companies) >= per_zip_cap:
            break
    return companies


def decode_url_query_param(url: str) -> str:
    return unquote(url or "")
