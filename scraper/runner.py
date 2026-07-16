"""Orchestrate zip-based Google Maps scraping."""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import (
    DEBUG_DIR,
    DEFAULT_DELAY_MAX,
    DEFAULT_DELAY_MIN,
    DEFAULT_PER_ZIP_CAP,
    OUTPUT_DIR,
    ensure_dirs,
    random_delay,
)
from .dedupe import DedupeStore
from .gmaps_client import BlockedError, GMapsClient
from .locations import ZipLocation, build_zip_pool
from .models import Company
from .parser import parse_response
from .proxy_manager import ProxyManager


ProgressCallback = Callable[[dict], None]


@dataclass
class RunStats:
    zips_tried: int = 0
    zips_total: int = 0
    companies_found: int = 0
    last_zip: str = ""
    last_count: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: bool = False
    finished: bool = False
    status: str = "idle"


class ScraperRunner:
    def __init__(
        self,
        *,
        search_term: str,
        countries: list[str] | None = None,
        states: list[str] | None = None,
        cities: list[str] | None = None,
        limit: int = 0,
        per_zip_cap: int = DEFAULT_PER_ZIP_CAP,
        delay_min: float = DEFAULT_DELAY_MIN,
        delay_max: float = DEFAULT_DELAY_MAX,
        proxy_urls: list[str] | None = None,
        use_proxies: bool = False,
        on_progress: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.search_term = search_term.strip()
        self.countries = countries or []
        self.states = states or []
        self.cities = cities or []
        self.limit = max(0, int(limit or 0))
        self.per_zip_cap = max(1, int(per_zip_cap or DEFAULT_PER_ZIP_CAP))
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.on_progress = on_progress
        self.should_stop = should_stop or (lambda: False)

        self.proxy_manager = ProxyManager(
            proxies=proxy_urls or [],
            enable_rotation=use_proxies and bool(proxy_urls),
        )
        self.client = GMapsClient(proxy_manager=self.proxy_manager)
        self.store = DedupeStore()
        self.stats = RunStats()
        ensure_dirs()

    def run(self) -> list[Company]:
        if not self.search_term:
            raise ValueError("search_term is required")

        pool = build_zip_pool(
            countries=self.countries or None,
            states=self.states or None,
            cities=self.cities or None,
            shuffle=True,
        )
        self.stats.zips_total = len(pool)
        self.stats.status = "running"
        self._emit()

        if not pool:
            self.stats.status = "no_zips"
            self.stats.finished = True
            self._emit()
            return []

        try:
            for loc in pool:
                if self.should_stop():
                    self.stats.stopped = True
                    self.stats.status = "stopped"
                    break
                if self.limit and len(self.store) >= self.limit:
                    self.stats.status = "limit_reached"
                    break

                self._scrape_zip(loc)

                # Delay between zips
                if self.should_stop():
                    self.stats.stopped = True
                    self.stats.status = "stopped"
                    break
                time.sleep(random_delay(self.delay_min, self.delay_max))
            else:
                self.stats.status = "completed"
        finally:
            self.stats.finished = True
            self.stats.companies_found = len(self.store)
            self._emit()
            self.client.close()

        return list(self.store.companies)

    def _scrape_zip(self, loc: ZipLocation) -> None:
        query = f"{self.search_term} {loc.zip_code}"
        self.stats.zips_tried += 1
        self.stats.last_zip = loc.zip_code
        self.stats.status = f"searching {loc.city}, {loc.state_abbr or loc.state} {loc.zip_code}"
        self._emit()

        try:
            raw = self.client.search(query)
            companies = parse_response(
                raw,
                search_term=self.search_term,
                search_zip=loc.zip_code,
                search_city=loc.city,
                search_state=loc.state_abbr or loc.state,
                per_zip_cap=self.per_zip_cap,
                debug_dir=DEBUG_DIR,
            )
            # Cap remaining if limit set
            if self.limit:
                remaining = self.limit - len(self.store)
                companies = companies[: max(0, remaining)]

            added = self.store.add_many(companies)
            self.stats.last_count = added
            self.stats.companies_found = len(self.store)
            self._emit(
                {
                    "event": "zip_done",
                    "zip": loc.zip_code,
                    "city": loc.city,
                    "returned": len(companies),
                    "added": added,
                }
            )
        except BlockedError as exc:
            msg = f"Blocked on zip {loc.zip_code}: {exc}"
            self.stats.errors.append(msg)
            self._emit({"event": "error", "message": msg})
            # Longer cool-down
            time.sleep(random_delay(8, 15))
        except Exception as exc:
            msg = f"Error on zip {loc.zip_code}: {exc}"
            self.stats.errors.append(msg)
            # Dump raw if available is handled in parser; log here
            self._emit({"event": "error", "message": msg})

    def _emit(self, extra: dict | None = None) -> None:
        if not self.on_progress:
            return
        payload = {
            "zips_tried": self.stats.zips_tried,
            "zips_total": self.stats.zips_total,
            "companies_found": len(self.store),
            "last_zip": self.stats.last_zip,
            "last_count": self.stats.last_count,
            "status": self.stats.status,
            "errors": list(self.stats.errors[-10:]),
            "stopped": self.stats.stopped,
            "finished": self.stats.finished,
            "companies": list(self.store.companies),
        }
        if extra:
            payload.update(extra)
        self.on_progress(payload)

    def export_csv(self, path: Path | None = None) -> Path:
        ensure_dirs()
        path = path or (OUTPUT_DIR / f"results_{int(time.time())}.csv")
        rows = [c.to_dict() for c in self.store.companies]
        if not rows:
            path.write_text("", encoding="utf-8")
            return path
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def export_excel(self, path: Path | None = None) -> Path:
        ensure_dirs()
        path = path or (OUTPUT_DIR / f"results_{int(time.time())}.xlsx")
        rows = [c.to_dict() for c in self.store.companies]
        df = pd.DataFrame(rows)
        if not df.empty:
            for col in ("zip_code", "search_zip", "phone", "phone_e164"):
                if col in df.columns:
                    df[col] = df[col].astype(str).replace({"nan": "", "None": ""})
        df.to_excel(path, index=False)
        return path
