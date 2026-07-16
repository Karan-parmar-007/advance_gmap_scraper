"""Deeper exploration of where place cards live in the payload."""

from __future__ import annotations

from scraper.parser import safe_get, unwrap_response


def main() -> None:
    raw = open("debug/live_raw.txt", encoding="utf-8").read()
    payload = unwrap_response(raw)
    print("top len", len(payload))

    for i, item in enumerate(payload):
        if not isinstance(item, list):
            continue
        # count place signatures inside
        count = 0
        names = []

        def walk(node, depth=0):
            nonlocal count
            if depth > 6:
                return
            if isinstance(node, list):
                pid = safe_get(node, 10)
                name = safe_get(node, 11)
                if isinstance(pid, str) and pid.startswith("0x") and isinstance(name, str) and len(name) > 2:
                    count += 1
                    if len(names) < 3:
                        names.append(name)
                    return
                for child in node[:80]:
                    walk(child, depth + 1)

        walk(item)
        if count:
            print(f"payload[{i}] list len={len(item)} places~={count} samples={names}")

    # Also try: look for arrays of [null, big_list] where big_list[11] is name
    places = []

    def find_places(node, depth=0):
        if depth > 10 or len(places) > 40:
            return
        if isinstance(node, list):
            # Shape A: [null, place_data]
            if len(node) >= 2 and node[0] is None and isinstance(node[1], list):
                pd = node[1]
                if isinstance(safe_get(pd, 11), str) and isinstance(safe_get(pd, 10), str):
                    if str(safe_get(pd, 10)).startswith("0x"):
                        places.append(pd)
                        return
            # Shape B: place_data itself
            if isinstance(safe_get(node, 11), str) and isinstance(safe_get(node, 10), str):
                if str(safe_get(node, 10)).startswith("0x") and len(node) > 50:
                    places.append(node)
                    return
            for child in node[:100]:
                find_places(child, depth + 1)

    find_places(payload)
    print(f"\nfind_places got {len(places)}")
    for p in places[:5]:
        print(" -", safe_get(p, 11), "|", safe_get(p, 178, 0, 0), "|", safe_get(p, 7, 0))


if __name__ == "__main__":
    main()
