"""Explore saved live response structure."""

from __future__ import annotations

from scraper.parser import safe_get, unwrap_response


def main() -> None:
    raw = open("debug/live_raw.txt", encoding="utf-8").read()
    payload = unwrap_response(raw)
    print("top type", type(payload).__name__, "len", len(payload) if isinstance(payload, list) else None)

    p0 = payload[0] if isinstance(payload, list) else None
    print("p0 type", type(p0).__name__, "len", len(p0) if isinstance(p0, list) else None)

    if isinstance(p0, list):
        for i, item in enumerate(p0[:20]):
            if isinstance(item, list):
                print(f"[0][{i}] list len={len(item)}")
            elif isinstance(item, str):
                print(f"[0][{i}] str {item[:80]!r}")
            else:
                print(f"[0][{i}] {type(item).__name__} {item!r}"[:120])

    # Search for place-like hex ids and names recursively (shallow)
    found = []

    def walk(node, path, depth=0):
        if depth > 8 or len(found) > 30:
            return
        if isinstance(node, str) and node.startswith("0x") and ":0x" in node:
            found.append((path, "place_id", node))
        if isinstance(node, list):
            # name often at index 11 when place_id at 10
            pid = safe_get(node, 10)
            name = safe_get(node, 11)
            if isinstance(pid, str) and pid.startswith("0x") and isinstance(name, str):
                found.append((path, "place", f"{name} | {pid}"))
            for i, child in enumerate(node[:40]):
                walk(child, path + [i], depth + 1)

    walk(payload, [])
    print("\nFound signatures:")
    for path, kind, val in found[:25]:
        print(f"  {kind} @ {path}: {val[:100]}")


if __name__ == "__main__":
    main()
