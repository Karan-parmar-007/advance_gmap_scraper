"""CLI: scrape Google Maps by search term + location filters."""

from __future__ import annotations

import argparse
import json
import sys

from scraper.config import PROXY_ENABLED, PROXY_MODE, PROXY_TARGETING, ensure_dirs
from scraper.proxy_manager import PROXY_MODES, TARGETING_LEVELS
from scraper.runner import ScraperRunner


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Google Maps company scraper (ZIP-based)")
    p.add_argument("term", help="Search term, e.g. plumber")
    p.add_argument("--country", action="append", default=[], help="Country (repeatable)")
    p.add_argument("--state", action="append", default=[], help="State name or abbr (repeatable)")
    p.add_argument("--city", action="append", default=[], help="City (repeatable)")
    p.add_argument("--limit", type=int, default=50, help="Total companies (0 = no limit; >0 splits evenly by region)")
    p.add_argument(
        "--per-zip",
        type=int,
        default=20,
        help=(
            "Target unique companies per ZIP (paginates in pages of 20 until met, "
            "exhausted, or no new results)"
        ),
    )
    p.add_argument("--delay-min", type=float, default=2.0)
    p.add_argument("--delay-max", type=float, default=6.0)
    p.add_argument(
        "--max-parallel",
        type=int,
        default=4,
        help="Maximum concurrent state/country pipelines",
    )
    p.add_argument(
        "--use-proxy",
        action="store_true",
        default=PROXY_ENABLED,
        help="Use DataImpulse residential proxies from .env (default from PROXY_ENABLED)",
    )
    p.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxies even if PROXY_ENABLED=true",
    )
    p.add_argument(
        "--proxy-mode",
        choices=list(PROXY_MODES),
        default=PROXY_MODE if PROXY_MODE in PROXY_MODES else "sticky",
        help="sticky (ports 10000-20000, same IP per ZIP) or rotating (port 823)",
    )
    p.add_argument(
        "--proxy-targeting",
        choices=list(TARGETING_LEVELS),
        default=PROXY_TARGETING if PROXY_TARGETING in TARGETING_LEVELS else "country",
        help="Geo targeting: country (1x) | state|city|zip (2x Target Filters)",
    )
    p.add_argument("--proxy", action="append", default=[], help="Static proxy URL override (repeatable)")
    p.add_argument("--json", action="store_true", help="Print JSON to stdout")
    args = p.parse_args(argv)

    ensure_dirs()
    use_proxies = bool(args.proxy) or (args.use_proxy and not args.no_proxy)

    def on_progress(info: dict) -> None:
        if info.get("event") == "pipeline_done":
            pipeline = info.get("pipeline") or {}
            print(
                f"{pipeline.get('label')}: "
                f"{pipeline.get('collected')}/{pipeline.get('target')} "
                f"({pipeline.get('zips_used')} ZIPs, {pipeline.get('pages')} pages; "
                f"total {info.get('companies_found')})",
                flush=True,
            )
        elif info.get("event") == "redistribution_start":
            print(
                f"Redistributing {info.get('remaining')} remaining companies "
                f"(round {info.get('round')})",
                flush=True,
            )
        elif info.get("event") == "error":
            print(f"ERROR: {info.get('message')}", file=sys.stderr, flush=True)

    runner = ScraperRunner(
        search_term=args.term,
        countries=args.country or ["United States"],
        states=args.state or None,
        cities=args.city or None,
        limit=args.limit,
        per_zip_cap=args.per_zip,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        proxy_urls=args.proxy,
        use_proxies=use_proxies,
        proxy_targeting=args.proxy_targeting if use_proxies else None,
        proxy_mode=args.proxy_mode if use_proxies else None,
        max_parallel_pipelines=args.max_parallel,
        on_progress=on_progress,
    )
    companies = runner.run()
    csv_path = runner.export_csv()
    xlsx_path = runner.export_excel()
    print(f"\nDone: {len(companies)} companies")
    print(f"CSV:   {csv_path}")
    print(f"Excel: {xlsx_path}")

    if args.json:
        print(json.dumps([c.to_dict() for c in companies], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
