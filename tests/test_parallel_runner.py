"""Tests for concurrent pipelines, deep scans, and quota redistribution."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from scraper.dedupe import DedupeStore
from scraper.locations import ZipLocation
from scraper.models import Company
from scraper.quotas import GeoUnit
from scraper.runner import (
    LocationProgress,
    PipelineProgress,
    ScraperRunner,
    ZipProgress,
)


def make_pipeline(name: str, target: int) -> PipelineProgress:
    return PipelineProgress(
        pipeline_id=name,
        label=name,
        country="United States",
        state=name,
        locations=[],
        target=target,
    )


class CapacityRunner(ScraperRunner):
    """Network-free runner with deterministic per-pipeline capacities."""

    def __init__(self, capacities: dict[str, int], limit: int) -> None:
        super().__init__(search_term="test", limit=limit)
        self.capacities = capacities
        self._company_lock = threading.Lock()

    def _run_pipeline(self, pipeline: PipelineProgress, phase: str) -> int:
        capacity = self.capacities[pipeline.pipeline_id]
        count = min(
            max(0, pipeline.target - pipeline.collected),
            max(0, capacity - pipeline.collected),
            max(0, self.limit - len(self.store)),
        )
        start = pipeline.collected
        companies = [
            Company(
                name=f"{pipeline.pipeline_id}-{index}",
                place_id=f"{pipeline.pipeline_id}-{index}",
            )
            for index in range(start, start + count)
        ]
        added = self.store.add_many(companies, max_total=self.limit)
        with pipeline.lock:
            pipeline.collected += added
            pipeline.exhausted = pipeline.collected >= capacity
            pipeline.status = (
                "target reached"
                if pipeline.collected >= pipeline.target
                else "exhausted"
            )
        return added


def test_shortfall_moves_to_pipeline_with_capacity() -> None:
    runner = CapacityRunner({"state-a": 20, "state-b": 100}, limit=100)
    first = make_pipeline("state-a", 50)
    second = make_pipeline("state-b", 50)
    runner.pipelines = [first, second]

    with ThreadPoolExecutor(max_workers=2) as executor:
        runner._run_parallel_phase(executor, runner.pipelines, phase="initial")
        assert len(runner.store) == 70
        assert first.exhausted
        assert not second.exhausted

        runner._redistribute_shortfall(executor)

    assert len(runner.store) == 100
    assert first.collected == 20
    assert second.collected == 80
    assert second.target == 80


def test_deep_scan_revisits_zip_after_normal_cap() -> None:
    runner = ScraperRunner(search_term="test", limit=100, per_zip_cap=20)
    loc = ZipLocation(
        zip_code="60608",
        city="Chicago",
        state="Illinois",
        state_abbr="IL",
        country="United States",
    )
    zip_progress = ZipProgress(loc=loc, pages=1, offset=20, zip_added=20)
    unit = GeoUnit(
        country="United States",
        state="Illinois",
        state_abbr="IL",
        city="Chicago",
        quota=100,
        zips=[loc],
    )
    location = LocationProgress(
        unit=unit,
        zips=[zip_progress],
        target=100,
    )

    selected = runner._next_zip(location)

    assert location.deep_scan
    assert selected is zip_progress


def test_dedupe_store_is_thread_safe() -> None:
    store = DedupeStore()
    companies = [
        Company(name=f"Company {index}", place_id=f"place-{index}")
        for index in range(100)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(store.add_many, companies) for _ in range(8)]
        for future in futures:
            future.result()

    assert len(store) == 100


def test_units_group_into_one_pipeline_per_state() -> None:
    runner = ScraperRunner(
        search_term="test",
        countries=["United States"],
        states=["Illinois", "Massachusetts"],
        cities=["Chicago", "Boston"],
        limit=600,
    )
    from scraper.quotas import build_scrape_units

    units = build_scrape_units(
        limit=600,
        countries=runner.countries,
        states=runner.states,
        cities=runner.cities,
    )
    pipelines = runner._build_pipelines(units)

    assert len(pipelines) == 2
    assert {pipeline.state for pipeline in pipelines} == {
        "Illinois",
        "Massachusetts",
    }
    assert {pipeline.target for pipeline in pipelines} == {300}


if __name__ == "__main__":
    test_shortfall_moves_to_pipeline_with_capacity()
    test_deep_scan_revisits_zip_after_normal_cap()
    test_dedupe_store_is_thread_safe()
    test_units_group_into_one_pipeline_per_state()
    print("All parallel runner tests passed.")
