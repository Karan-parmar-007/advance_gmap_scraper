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
    INTRA_ZIP_DELAY_MAX,
    INTRA_ZIP_DELAY_MIN,
    MAX_PAGES_PER_ZIP,
    OUTPUT_DIR,
    PAGE_SIZE,
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

        self.client.begin_zip_session()
        offset = 0
        zip_added = 0
        pages = 0

        try:
            while zip_added < self.per_zip_cap:
                if self.should_stop():
                    break
                if self.limit and len(self.store) >= self.limit:
                    break
                if pages >= MAX_PAGES_PER_ZIP:
                    break

                remaining_zip = self.per_zip_cap - zip_added
                remaining_global = self.limit - len(self.store) if self.limit else remaining_zip
                keep_cap = min(remaining_zip, remaining_global)

                self.stats.status = (
                    f"{loc.zip_code} page {pages + 1} (offset {offset})"
                )
                self._emit()

                raw = self.client.search(
                    query,
                    offset=offset,
                    use_zip_session=True,
                )
                companies = parse_response(
                    raw,
                    search_term=self.search_term,
                    search_zip=loc.zip_code,
                    search_city=loc.city,
                    search_state=loc.state_abbr or loc.state,
                    per_zip_cap=PAGE_SIZE,
                    debug_dir=DEBUG_DIR,
                )
                page_count = len(companies)
                companies = companies[:keep_cap]

                added = self.store.add_many(companies)
                zip_added += added
                pages += 1
                self.stats.last_count = zip_added
                self.stats.companies_found = len(self.store)
                self._emit(
                    {
                        "event": "page_done",
                        "zip": loc.zip_code,
                        "city": loc.city,
                        "page": pages,
                        "offset": offset,
                        "returned": page_count,
                        "added": added,
                        "zip_total": zip_added,
                    }
                )

                if page_count < PAGE_SIZE:
                    break
                if added == 0:
                    break
                if zip_added >= self.per_zip_cap:
                    break
                if self.limit and len(self.store) >= self.limit:
                    break

                offset += PAGE_SIZE
                time.sleep(random_delay(INTRA_ZIP_DELAY_MIN, INTRA_ZIP_DELAY_MAX))

            self._emit(
                {
                    "event": "zip_done",
                    "zip": loc.zip_code,
                    "city": loc.city,
                    "pages": pages,
                    "returned": zip_added,
                    "added": zip_added,
                }
            )
        except BlockedError as exc:
            msg = f"Blocked on zip {loc.zip_code}: {exc}"
            self.stats.errors.append(msg)
            self._emit({"event": "error", "message": msg})
            time.sleep(random_delay(8, 15))
        except Exception as exc:
            msg = f"Error on zip {loc.zip_code}: {exc}"
            self.stats.errors.append(msg)
            self._emit({"event": "error", "message": msg})
        finally:
            self.client.end_zip_session()

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
