"""Concurrent, quota-aware orchestration for ZIP-based Maps scraping."""

from __future__ import annotations

import csv
import queue
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
from .locations import ZipLocation
from .models import Company
from .parser import parse_response
from .proxy_manager import ProxyManager
from .quotas import GeoUnit, build_scrape_units, divide_evenly


ProgressCallback = Callable[[dict], None]


@dataclass
class RunStats:
    zips_total: int = 0
    zips_used: int = 0
    pages_fetched: int = 0
    requests_made: int = 0
    pipelines_total: int = 0
    pipelines_active: int = 0
    companies_found: int = 0
    last_zip: str = ""
    last_count: int = 0
    current_unit: str = ""
    errors: list[str] = field(default_factory=list)
    stopped: bool = False
    finished: bool = False
    status: str = "idle"
    redistribution_round: int = 0


@dataclass
class ZipProgress:
    loc: ZipLocation
    offset: int = 0
    pages: int = 0
    zip_added: int = 0
    pending: list[Company] = field(default_factory=list)
    no_more_pages: bool = False
    started: bool = False


@dataclass
class LocationProgress:
    unit: GeoUnit
    zips: list[ZipProgress]
    target: int
    collected: int = 0
    cursor: int = 0
    deep_scan: bool = False
    exhausted: bool = False

    def label(self) -> str:
        return self.unit.label()


@dataclass
class PipelineProgress:
    pipeline_id: str
    label: str
    country: str
    state: str
    locations: list[LocationProgress]
    target: int
    collected: int = 0
    status: str = "queued"
    current_zip: str = ""
    zips_used: int = 0
    pages_fetched: int = 0
    requests_made: int = 0
    exhausted: bool = False
    active: bool = False
    shortfall: int = 0
    redistribution_added: int = 0
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


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
        max_parallel_pipelines: int = 4,
        on_progress: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.search_term = search_term.strip()
        self.countries = countries or []
        self.states = states or []
        self.cities = cities or []
        self.limit = max(0, int(limit or 0))
        self.per_zip_cap = max(1, int(per_zip_cap or DEFAULT_PER_ZIP_CAP))
        self.delay_min = max(0.0, float(delay_min))
        self.delay_max = max(self.delay_min, float(delay_max))
        self.proxy_urls = proxy_urls or []
        self.use_proxies = use_proxies
        self.max_parallel_pipelines = max(1, int(max_parallel_pipelines))
        self.on_progress = on_progress
        self.should_stop = should_stop or (lambda: False)

        self.store = DedupeStore()
        self.stats = RunStats()
        self.pipelines: list[PipelineProgress] = []
        self._events: queue.Queue[dict] = queue.Queue()
        self._stats_lock = threading.Lock()
        self._used_zip_keys: set[tuple[str, str, str, str]] = set()
        ensure_dirs()

    def run(self) -> list[Company]:
        if not self.search_term:
            raise ValueError("search_term is required")

        units = build_scrape_units(
            limit=self.limit,
            countries=self.countries or None,
            states=self.states or None,
            cities=self.cities or None,
        )
        self.pipelines = self._build_pipelines(units)
        self.stats.pipelines_total = len(self.pipelines)
        self.stats.zips_total = sum(
            len(location.zips)
            for pipeline in self.pipelines
            for location in pipeline.locations
        )
        self.stats.status = "running"
        self._emit({"event": "run_start"})

        if not self.pipelines:
            self.stats.status = "no_zips"
            self.stats.finished = True
            self._emit()
            return []

        worker_count = min(self.max_parallel_pipelines, len(self.pipelines))
        try:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="maps-pipeline",
            ) as executor:
                self._run_parallel_phase(
                    executor,
                    self.pipelines,
                    phase="initial",
                )
                self._redistribute_shortfall(executor)

            if self.should_stop():
                self.stats.stopped = True
                self.stats.status = "stopped"
            elif self.limit and len(self.store) >= self.limit:
                self.stats.status = "limit_reached"
            elif self.limit and len(self.store) < self.limit:
                self.stats.status = "capacity_exhausted"
            else:
                self.stats.status = "completed"
        finally:
            self._drain_events()
            self.stats.finished = True
            self.stats.companies_found = len(self.store)
            self.stats.pipelines_active = 0
            self._emit({"event": "run_done"})

        return self.store.snapshot()

    def _build_pipelines(self, units: list[GeoUnit]) -> list[PipelineProgress]:
        """Group city units into one worker pipeline per state (or country)."""
        grouped: dict[tuple[str, str], list[GeoUnit]] = {}
        for unit in units:
            state_key = unit.state or "__country__"
            grouped.setdefault((unit.country, state_key), []).append(unit)

        pipelines: list[PipelineProgress] = []
        for index, ((country, state_key), grouped_units) in enumerate(
            sorted(grouped.items(), key=lambda item: item[0])
        ):
            state = "" if state_key == "__country__" else grouped_units[0].state
            state_abbr = grouped_units[0].state_abbr
            label = (
                f"{state_abbr or state} ({country})"
                if state
                else (country or "All selected locations")
            )
            locations = [
                LocationProgress(
                    unit=unit,
                    zips=[ZipProgress(loc=loc) for loc in unit.zips],
                    target=unit.quota,
                )
                for unit in grouped_units
            ]
            pipelines.append(
                PipelineProgress(
                    pipeline_id=f"pipeline-{index + 1}",
                    label=label,
                    country=country,
                    state=state,
                    locations=locations,
                    target=sum(unit.quota for unit in grouped_units),
                )
            )
        return pipelines

    def _run_parallel_phase(
        self,
        executor: ThreadPoolExecutor,
        pipelines: list[PipelineProgress],
        *,
        phase: str,
    ) -> int:
        if not pipelines:
            return 0

        before = len(self.store)
        futures: dict[Future[int], PipelineProgress] = {
            executor.submit(self._run_pipeline, pipeline, phase): pipeline
            for pipeline in pipelines
        }
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=0.15, return_when=FIRST_COMPLETED)
            self._drain_events()
            for future in done:
                pipeline = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    message = f"{pipeline.label} pipeline failed: {exc}"
                    with pipeline.lock:
                        pipeline.status = "error"
                        pipeline.errors.append(str(exc))
                        pipeline.active = False
                    self._record_error(message)
        self._drain_events()
        return len(self.store) - before

    def _redistribute_shortfall(self, executor: ThreadPoolExecutor) -> None:
        """Move unmet quota to any selected pipeline that still has capacity."""
        if not self.limit or self.should_stop():
            return

        round_number = 0
        while len(self.store) < self.limit and not self.should_stop():
            candidates = [pipeline for pipeline in self.pipelines if not pipeline.exhausted]
            if not candidates:
                break

            needed = self.limit - len(self.store)
            allocations = divide_evenly(needed, len(candidates))
            if not any(allocations):
                break

            round_number += 1
            self.stats.redistribution_round = round_number
            allocation_payload: list[dict] = []
            for pipeline, extra in zip(candidates, allocations):
                if extra <= 0:
                    continue
                with pipeline.lock:
                    pipeline.target += extra
                    pipeline.shortfall = max(0, pipeline.target - pipeline.collected)
                allocation_payload.append(
                    {
                        "pipeline_id": pipeline.pipeline_id,
                        "label": pipeline.label,
                        "extra": extra,
                        "new_target": pipeline.target,
                    }
                )

            self.stats.status = f"redistributing {needed} remaining companies"
            self._emit(
                {
                    "event": "redistribution_start",
                    "round": round_number,
                    "remaining": needed,
                    "allocations": allocation_payload,
                }
            )
            added = self._run_parallel_phase(
                executor,
                candidates,
                phase=f"redistribution-{round_number}",
            )
            self._emit(
                {
                    "event": "redistribution_done",
                    "round": round_number,
                    "added": added,
                    "remaining": max(0, self.limit - len(self.store)),
                }
            )
            if added == 0:
                break

    def _run_pipeline(self, pipeline: PipelineProgress, phase: str) -> int:
        before = pipeline.collected
        with pipeline.lock:
            pipeline.active = True
            pipeline.status = "running" if phase == "initial" else "redistributed"
        self._queue_pipeline_event("pipeline_start", pipeline, phase=phase)

        proxy_manager = ProxyManager(
            proxies=self.proxy_urls,
            enable_rotation=self.use_proxies and bool(self.proxy_urls),
        )
        client = GMapsClient(proxy_manager=proxy_manager)
        try:
            self._fill_original_location_quotas(pipeline, client)
            self._fill_pipeline_target(pipeline, client)
        finally:
            client.close()
            with pipeline.lock:
                pipeline.active = False
                pipeline.shortfall = max(0, pipeline.target - pipeline.collected)
                if pipeline.collected >= pipeline.target:
                    pipeline.status = "target reached"
                elif pipeline.exhausted:
                    pipeline.status = "exhausted"
                else:
                    pipeline.status = "waiting"
            if phase != "initial":
                pipeline.redistribution_added += pipeline.collected - before
            self._queue_pipeline_event("pipeline_done", pipeline, phase=phase)
        return pipeline.collected - before

    def _fill_original_location_quotas(
        self,
        pipeline: PipelineProgress,
        client: GMapsClient,
    ) -> None:
        """Honor the country/state/city allocation before using spare capacity."""
        for location in pipeline.locations:
            if self._must_stop(pipeline):
                return
            if location.target <= 0 or location.collected >= location.target:
                continue
            self._scrape_location(
                pipeline,
                location,
                client,
                target=location.target,
            )

    def _fill_pipeline_target(
        self,
        pipeline: PipelineProgress,
        client: GMapsClient,
    ) -> None:
        """Use any remaining location capacity to cover local or transferred gaps."""
        while pipeline.collected < pipeline.target and not self._must_stop(pipeline):
            candidates = [location for location in pipeline.locations if not location.exhausted]
            if not candidates:
                pipeline.exhausted = True
                return

            round_before = pipeline.collected
            for location in candidates:
                if self._must_stop(pipeline) or pipeline.collected >= pipeline.target:
                    break
                self._scrape_location(
                    pipeline,
                    location,
                    client,
                    target=location.collected + (pipeline.target - pipeline.collected),
                )
            if pipeline.collected == round_before:
                if all(location.exhausted for location in pipeline.locations):
                    pipeline.exhausted = True
                return

    def _scrape_location(
        self,
        pipeline: PipelineProgress,
        location: LocationProgress,
        client: GMapsClient,
        *,
        target: int,
    ) -> None:
        while location.collected < target and not self._must_stop(pipeline):
            zip_progress = self._next_zip(location)
            if zip_progress is None:
                location.exhausted = True
                return

            normal_cap = self.per_zip_cap
            deep_cap = PAGE_SIZE * MAX_PAGES_PER_ZIP
            zip_cap = deep_cap if location.deep_scan else normal_cap
            remaining = min(
                target - location.collected,
                pipeline.target - pipeline.collected,
            )
            if remaining <= 0:
                return

            try:
                added = self._consume_or_fetch_page(
                    pipeline,
                    location,
                    zip_progress,
                    client,
                    remaining=remaining,
                    zip_cap=zip_cap,
                )
            except BlockedError as exc:
                message = f"Blocked on {pipeline.label} ZIP {zip_progress.loc.zip_code}: {exc}"
                pipeline.errors.append(str(exc))
                self._record_error(message)
                zip_progress.no_more_pages = True
                time.sleep(random_delay(8, 15))
                continue
            except Exception as exc:
                message = f"Error on {pipeline.label} ZIP {zip_progress.loc.zip_code}: {exc}"
                pipeline.errors.append(str(exc))
                self._record_error(message)
                zip_progress.no_more_pages = True
                continue

            if added == 0 and not zip_progress.pending and zip_progress.no_more_pages:
                continue
            if location.collected < target and not self._must_stop(pipeline):
                delay_min = INTRA_ZIP_DELAY_MIN if self._zip_can_continue(
                    zip_progress, zip_cap
                ) else self.delay_min
                delay_max = INTRA_ZIP_DELAY_MAX if self._zip_can_continue(
                    zip_progress, zip_cap
                ) else self.delay_max
                time.sleep(random_delay(delay_min, delay_max))

    def _next_zip(self, location: LocationProgress) -> ZipProgress | None:
        """Return the next usable ZIP; deep-scan every ZIP after normal pass."""
        normal_cap = self.per_zip_cap
        deep_cap = PAGE_SIZE * MAX_PAGES_PER_ZIP
        cap = deep_cap if location.deep_scan else normal_cap

        while location.cursor < len(location.zips):
            progress = location.zips[location.cursor]
            if self._zip_can_continue(progress, cap):
                return progress
            location.cursor += 1

        if not location.deep_scan:
            location.deep_scan = True
            location.cursor = 0
            self._queue_pipeline_event(
                "deep_scan_start",
                None,
                location=location.label(),
            )
            while location.cursor < len(location.zips):
                progress = location.zips[location.cursor]
                if self._zip_can_continue(progress, deep_cap):
                    return progress
                location.cursor += 1

        return None

    @staticmethod
    def _zip_can_continue(progress: ZipProgress, cap: int) -> bool:
        if progress.pending:
            return progress.zip_added < cap
        return (
            not progress.no_more_pages
            and progress.pages < MAX_PAGES_PER_ZIP
            and progress.zip_added < cap
        )

    def _consume_or_fetch_page(
        self,
        pipeline: PipelineProgress,
        location: LocationProgress,
        progress: ZipProgress,
        client: GMapsClient,
        *,
        remaining: int,
        zip_cap: int,
    ) -> int:
        added_total = 0
        allowed = min(remaining, zip_cap - progress.zip_added)
        if allowed <= 0:
            return 0

        while progress.pending and allowed > 0:
            batch = progress.pending[:allowed]
            del progress.pending[: len(batch)]
            added = self.store.add_many(batch, max_total=self.limit)
            progress.zip_added += added
            location.collected += added
            pipeline.collected += added
            added_total += added
            allowed -= added
            if self.limit and len(self.store) >= self.limit:
                return added_total

        if allowed <= 0 or progress.no_more_pages:
            return added_total

        loc = progress.loc
        query = f"{self.search_term} {loc.zip_code}"
        pipeline.current_zip = loc.zip_code
        if not progress.started:
            progress.started = True
            pipeline.zips_used += 1
            self._mark_zip_used(loc)

        client.begin_zip_session()
        try:
            raw = client.search(
                query,
                offset=progress.offset,
                use_zip_session=True,
            )
        finally:
            client.end_zip_session()

        companies = parse_response(
            raw,
            search_term=self.search_term,
            search_zip=loc.zip_code,
            search_city=loc.city,
            search_state=loc.state_abbr or loc.state,
            per_zip_cap=PAGE_SIZE,
            debug_dir=DEBUG_DIR,
        )
        returned = len(companies)
        page_offset = progress.offset
        progress.offset += PAGE_SIZE
        progress.pages += 1
        progress.pending.extend(companies)
        pipeline.pages_fetched += 1
        pipeline.requests_made += 1
        self._record_request(loc, returned)

        if returned < PAGE_SIZE or progress.pages >= MAX_PAGES_PER_ZIP:
            progress.no_more_pages = True

        before = added_total
        while progress.pending and allowed > 0:
            batch = progress.pending[:allowed]
            del progress.pending[: len(batch)]
            added = self.store.add_many(batch, max_total=self.limit)
            progress.zip_added += added
            location.collected += added
            pipeline.collected += added
            added_total += added
            allowed -= added
            if self.limit and len(self.store) >= self.limit:
                break

        self._queue_pipeline_event(
            "page_done",
            pipeline,
            zip=loc.zip_code,
            city=loc.city,
            page=progress.pages,
            offset=page_offset,
            returned=returned,
            added=added_total - before,
            zip_total=progress.zip_added,
            location=location.label(),
            deep_scan=location.deep_scan,
        )
        return added_total

    def _must_stop(self, pipeline: PipelineProgress) -> bool:
        return (
            self.should_stop()
            or (self.limit > 0 and len(self.store) >= self.limit)
            or pipeline.collected >= pipeline.target
        )

    def _mark_zip_used(self, loc: ZipLocation) -> None:
        key = (loc.country, loc.state, loc.city, loc.zip_code)
        with self._stats_lock:
            self._used_zip_keys.add(key)
            self.stats.zips_used = len(self._used_zip_keys)
            self.stats.last_zip = loc.zip_code

    def _record_request(self, loc: ZipLocation, returned: int) -> None:
        with self._stats_lock:
            self.stats.pages_fetched += 1
            self.stats.requests_made += 1
            self.stats.companies_found = len(self.store)
            self.stats.last_zip = loc.zip_code
            self.stats.last_count = returned

    def _record_error(self, message: str) -> None:
        with self._stats_lock:
            self.stats.errors.append(message)
        self._events.put({"event": "error", "message": message})

    def _queue_pipeline_event(
        self,
        event: str,
        pipeline: PipelineProgress | None,
        **extra: object,
    ) -> None:
        payload: dict = {"event": event, **extra}
        if pipeline is not None:
            payload["pipeline_id"] = pipeline.pipeline_id
            payload["pipeline"] = self._pipeline_info(pipeline)
        self._events.put(payload)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._emit(event)

    def _pipeline_info(self, pipeline: PipelineProgress) -> dict:
        with pipeline.lock:
            return {
                "id": pipeline.pipeline_id,
                "label": pipeline.label,
                "country": pipeline.country,
                "state": pipeline.state,
                "target": pipeline.target,
                "collected": pipeline.collected,
                "shortfall": max(0, pipeline.target - pipeline.collected),
                "status": pipeline.status,
                "active": pipeline.active,
                "exhausted": pipeline.exhausted,
                "current_zip": pipeline.current_zip,
                "zips_used": pipeline.zips_used,
                "zips_total": sum(len(location.zips) for location in pipeline.locations),
                "pages": pipeline.pages_fetched,
                "requests": pipeline.requests_made,
                "redistribution_added": pipeline.redistribution_added,
                "locations": [
                    {
                        "label": location.label(),
                        "target": location.target,
                        "collected": location.collected,
                        "zips_used": sum(1 for z in location.zips if z.started),
                        "zips_total": len(location.zips),
                        "deep_scan": location.deep_scan,
                        "exhausted": location.exhausted,
                    }
                    for location in pipeline.locations
                ],
            }

    def _emit(self, extra: dict | None = None) -> None:
        if not self.on_progress:
            return
        pipeline_info = [self._pipeline_info(p) for p in self.pipelines]
        self.stats.pipelines_active = sum(1 for p in pipeline_info if p["active"])
        payload = {
            "zips_used": self.stats.zips_used,
            "zips_total": self.stats.zips_total,
            "pages_fetched": self.stats.pages_fetched,
            "requests_made": self.stats.requests_made,
            "pipelines_total": self.stats.pipelines_total,
            "pipelines_active": self.stats.pipelines_active,
            "companies_found": len(self.store),
            "last_zip": self.stats.last_zip,
            "last_count": self.stats.last_count,
            "current_unit": self.stats.current_unit,
            "status": self.stats.status,
            "errors": list(self.stats.errors[-20:]),
            "stopped": self.stats.stopped,
            "finished": self.stats.finished,
            "redistribution_round": self.stats.redistribution_round,
            "pipelines": pipeline_info,
            "companies": self.store.snapshot(),
        }
        if extra:
            payload.update(extra)
        self.on_progress(payload)

    def export_csv(self, path: Path | None = None) -> Path:
        ensure_dirs()
        path = path or (OUTPUT_DIR / f"results_{int(time.time())}.csv")
        rows = [company.to_dict() for company in self.store.snapshot()]
        if not rows:
            path.write_text("", encoding="utf-8")
            return path
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def export_excel(self, path: Path | None = None) -> Path:
        ensure_dirs()
        path = path or (OUTPUT_DIR / f"results_{int(time.time())}.xlsx")
        rows = [company.to_dict() for company in self.store.snapshot()]
        dataframe = pd.DataFrame(rows)
        if not dataframe.empty:
            for column in ("zip_code", "search_zip", "phone", "phone_e164"):
                if column in dataframe.columns:
                    dataframe[column] = dataframe[column].astype(str).replace(
                        {"nan": "", "None": ""}
                    )
        dataframe.to_excel(path, index=False)
        return path
