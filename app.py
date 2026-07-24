"""Streamlit dashboard for the concurrent Google Maps company scraper."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from scraper.config import (
    DEFAULT_DELAY_MAX,
    DEFAULT_DELAY_MIN,
    DEFAULT_PER_ZIP_CAP,
    MAX_DISCOVERY_WORKERS,
    MAX_PAGES_PER_ZIP,
    OUTPUT_DIR,
    PAGE_SIZE,
    PROXY_ENABLED,
    PROXY_MODE,
    PROXY_TARGETING,
    UI_PREVIEW_ROWS,
    auto_discovery_workers,
    ensure_dirs,
)
from scraper.locations import list_cities, list_countries, list_states
from scraper.proxy_manager import PROXY_MODES, ProxyManager, TARGETING_LEVELS
from scraper.quotas import preview_pipeline_plan
from scraper.runner import ScraperRunner


st.set_page_config(
    page_title="Maps company scraper",
    page_icon=":material/travel_explore:",
    layout="wide",
)
ensure_dirs()


@dataclass
class RunState:
    """Thread-safe scrape state. Worker never touches st.session_state."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    running: bool = False
    progress: dict[str, Any] = field(default_factory=dict)
    results: list = field(default_factory=list)  # UI preview only
    companies_total: int = 0
    with_reviews: int = 0
    csv_path: str = ""
    xlsx_path: str = ""
    logs: list[str] = field(default_factory=list)
    run_error: str = ""
    run_message: str = ""
    worker: threading.Thread | None = None

    def append_log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-300:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "progress": dict(self.progress),
                "results": list(self.results),
                "companies_total": self.companies_total,
                "with_reviews": self.with_reviews,
                "csv_path": self.csv_path,
                "xlsx_path": self.xlsx_path,
                "logs": list(self.logs),
                "run_error": self.run_error,
                "run_message": self.run_message,
            }


def get_run_state() -> RunState:
    if "run_state" not in st.session_state:
        st.session_state.run_state = RunState()
    return st.session_state.run_state


def companies_to_df(companies: list) -> pd.DataFrame:
    if not companies:
        return pd.DataFrame()
    if hasattr(companies[0], "to_dict"):
        return pd.DataFrame([company.to_dict() for company in companies])
    return pd.DataFrame(companies)


def pipelines_to_df(pipelines: list[dict]) -> pd.DataFrame:
    rows = []
    for pipeline in pipelines:
        target = int(pipeline.get("target") or 0)
        collected = int(pipeline.get("collected") or 0)
        progress = min(100, round((collected / target) * 100)) if target else 0
        deep_locations = sum(
            1 for location in pipeline.get("locations", []) if location.get("deep_scan")
        )
        rows.append(
            {
                "Pipeline": pipeline.get("label"),
                "Status": pipeline.get("status"),
                "Target": target or "Unlimited",
                "Collected": collected,
                "Progress": progress,
                "Shortfall": pipeline.get("shortfall") or 0,
                "ZIPs used": pipeline.get("zips_used") or 0,
                "ZIPs available": pipeline.get("zips_total") or 0,
                "Pages": pipeline.get("pages") or 0,
                "Requests": pipeline.get("requests") or 0,
                "Current ZIP": pipeline.get("current_zip") or "—",
                "Deep scans": deep_locations,
                "Reallocated": pipeline.get("redistribution_added") or 0,
            }
        )
    return pd.DataFrame(rows)


def make_progress_callback(run_state: RunState):
    def on_progress(info: dict) -> None:
        with run_state.lock:
            preview = info.get("companies_preview") or []
            if preview and hasattr(preview[0], "to_dict"):
                preview = [company.to_dict() for company in preview]
            payload = {k: v for k, v in info.items() if k != "companies_preview"}
            run_state.progress = payload
            run_state.results = list(preview)[-UI_PREVIEW_ROWS:]
            run_state.companies_total = int(info.get("companies_found") or 0)
            run_state.with_reviews = int(info.get("with_reviews") or 0)

            event = info.get("event")
            if event == "pipeline_done":
                pipeline = info.get("pipeline") or {}
                run_state.logs.append(
                    f"{pipeline.get('label')}: {pipeline.get('status')} — "
                    f"{pipeline.get('collected')}/{pipeline.get('target')}, "
                    f"{pipeline.get('zips_used')} ZIPs, {pipeline.get('pages')} pages"
                )
            elif event == "redistribution_start":
                allocations = ", ".join(
                    f"{item['label']} +{item['extra']}"
                    for item in info.get("allocations", [])
                )
                run_state.logs.append(
                    f"Redistribution round {info.get('round')}: {allocations}"
                )
            elif event == "deep_scan_start":
                run_state.logs.append(
                    f"Deep pagination started for {info.get('location')}"
                )
            elif event == "error":
                run_state.logs.append(f"ERROR: {info.get('message')}")
            elif event == "run_start":
                pipelines = info.get("pipelines") or []
                run_state.logs.append(
                    f"Starting {len(pipelines)} pipeline(s) · "
                    f"discovery×{info.get('discovery_workers')}: "
                    + ", ".join(
                        f"{p.get('label')}={p.get('target')}" for p in pipelines[:12]
                    )
                    + ("…" if len(pipelines) > 12 else "")
                )
            run_state.logs = run_state.logs[-300:]

    return on_progress


def render_progress(
    info: dict,
    *,
    total_target: int,
    results: list,
    logs: list[str],
    companies_total: int = 0,
    with_reviews: int = 0,
) -> None:
    found = int(info.get("companies_found") or companies_total or 0)
    active = int(info.get("pipelines_active") or 0)
    pipeline_total = int(info.get("pipelines_total") or 0)
    zips_used = int(info.get("zips_used") or 0)
    zips_total = int(info.get("zips_total") or 0)
    pages = int(info.get("pages_fetched") or 0)
    requests = int(info.get("requests_made") or 0)
    round_number = int(info.get("redistribution_round") or 0)
    reviews = int(info.get("with_reviews") or with_reviews or 0)

    if total_target:
        fraction = min(found / total_target, 1.0)
        progress_text = f"{found:,} / {total_target:,} unique companies"
    else:
        fraction = min(zips_used / max(zips_total, 1), 1.0)
        progress_text = f"{zips_used:,} / {zips_total:,} ZIPs used"
    overall_progress.progress(fraction, text=progress_text)

    status = str(info.get("status") or "running").replace("_", " ").capitalize()
    redistribution = (
        f" · Redistribution round {round_number}" if round_number else ""
    )
    status_heading.markdown(f"**{status}**{redistribution}")

    kpi_companies.metric("Unique companies", f"{found:,}")
    kpi_pipelines.metric("Active pipelines", f"{active} / {pipeline_total}")
    kpi_zips.metric("ZIP codes used", f"{zips_used:,} / {zips_total:,}")
    kpi_pages.metric("Pages / requests", f"{pages:,} / {requests:,}")
    kpi_reviews.metric("With review count", f"{reviews:,}")

    pipeline_df = pipelines_to_df(info.get("pipelines") or [])
    if pipeline_df.empty:
        pipeline_slot.info("Pipelines will appear after the run starts.")
    else:
        pipeline_slot.dataframe(
            pipeline_df,
            hide_index=True,
            width="stretch",
            column_config={
                "Progress": st.column_config.ProgressColumn(
                    "Progress",
                    min_value=0,
                    max_value=100,
                    format="%d%%",
                ),
            },
        )

    if logs:
        log_slot.code("\n".join(logs[-300:]))


def run_worker(config: dict, run_state: RunState) -> None:
    """Background worker: uses only the shared RunState object."""
    try:
        runner = ScraperRunner(
            search_term=config["search_term"],
            countries=config["countries"],
            states=config["states"],
            cities=config["cities"],
            limit=config["limit"],
            per_zip_cap=config["per_zip_cap"],
            delay_min=config["delay_min"],
            delay_max=config["delay_max"],
            proxy_urls=config["proxy_urls"],
            use_proxies=config["use_proxies"],
            proxy_targeting=config["proxy_targeting"],
            proxy_mode=config["proxy_mode"],
            max_discovery_workers=config.get("max_discovery_workers", 0),
            on_progress=make_progress_callback(run_state),
            should_stop=run_state.stop_event.is_set,
        )
        companies = runner.run()
        total = len(companies)
        preview = [
            company.to_dict() if hasattr(company, "to_dict") else company
            for company in companies[-UI_PREVIEW_ROWS:]
        ]
        csv_path = xlsx_path = None
        if companies:
            csv_path = runner.export_csv()
            xlsx_path = runner.export_excel()
        with run_state.lock:
            run_state.results = preview
            run_state.companies_total = total
            run_state.with_reviews = runner.store.with_reviews_count()
            run_state.csv_path = str(csv_path) if csv_path else ""
            run_state.xlsx_path = str(xlsx_path) if xlsx_path else ""
            files_note = ""
            if csv_path:
                files_note = f" Saved as {csv_path.name}"
                if xlsx_path:
                    files_note += f" and {xlsx_path.name}."
            if run_state.stop_event.is_set():
                run_state.run_message = (
                    f"Stopped early with {total:,} unique companies.{files_note}"
                )
            elif config["limit"] and total < config["limit"]:
                run_state.run_message = (
                    f"Capacity exhausted: collected {total:,} of "
                    f"{config['limit']:,} requested companies.{files_note}"
                )
            elif total:
                run_state.run_message = (
                    f"Collected {total:,} unique companies.{files_note}"
                )
            else:
                run_state.run_message = (
                    "No companies were collected. Check the filters and run log."
                )
    except Exception as exc:
        with run_state.lock:
            run_state.run_error = str(exc)
            run_state.logs.append(f"ERROR: {exc}")
            run_state.logs = run_state.logs[-300:]
    finally:
        with run_state.lock:
            run_state.running = False


run_state = get_run_state()

st.title("Maps company scraper")
st.caption(
    "Pipelines discover companies in parallel from the Maps search API. "
    "The table shows a preview only — full results are written to disk."
)

with st.sidebar:
    st.header("Run configuration")
    search_term = st.text_input(
        "Search term",
        placeholder="IT companies, plumber, web design…",
        key="search_term",
    )
    st.caption("Whatever you type is sent to Maps as-is (plus the ZIP).")

    st.subheader("Location filters")
    countries = st.multiselect(
        "Countries",
        options=list_countries(),
        default=["United States"],
        key="countries",
    )
    states = st.multiselect("States", options=list_states(), key="states")
    city_options = list_cities(states if states else None)
    cities = st.multiselect(
        "Cities",
        options=city_options,
        key="cities",
        help=(
            "Optional. Leave empty to auto-split the selected state(s) into "
            "pipelines. Country-only runs auto-split by quota / per-ZIP."
        ),
    )

    st.subheader("Targets")
    limit = st.number_input(
        "Total unique companies",
        min_value=0,
        value=50,
        step=10,
        help=(
            "0 means unlimited. A positive target is divided across auto "
            "state/city pipelines (or your selected cities). Unmet quota is "
            "redistributed to pipelines that still have ZIP capacity."
        ),
        key="limit",
    )
    per_zip_cap = st.number_input(
        "Initial results per ZIP",
        min_value=1,
        max_value=PAGE_SIZE * MAX_PAGES_PER_ZIP,
        value=DEFAULT_PER_ZIP_CAP,
        help=(
            "Normal pass target per ZIP. Maps returns ~20 per page, so 50 means "
            "paginate ~3 pages on the same sticky IP before moving to the next ZIP. "
            f"Deep pass can go up to {MAX_PAGES_PER_ZIP} pages if quota is still short."
        ),
        key="per_zip_cap",
    )

    st.subheader("Parallelism and pacing")
    auto_parallel = st.toggle(
        "Auto parallelism from company target",
        value=True,
        key="auto_parallel",
        help=(
            "Plans pipelines ≈ total ÷ results-per-ZIP (same math for country "
            "or selected states). Concurrent workers follow that plan, capped at "
            f"{MAX_DISCOVERY_WORKERS}. Extra planned pipelines wait in queue. "
            "Turn off to lower concurrent workers on a weaker machine."
        ),
    )
    plan_preview = preview_pipeline_plan(
        limit=int(limit),
        countries=countries or None,
        states=states or None,
        cities=cities or None,
        per_zip_cap=int(per_zip_cap),
    )
    auto_disc = auto_discovery_workers(
        int(limit),
        len(plan_preview),
        per_zip_cap=int(per_zip_cap),
    )
    if auto_parallel:
        max_discovery = 0
        st.caption(
            f"Plan {len(plan_preview)} pipeline(s) · concurrent ×{auto_disc} "
            f"(max {MAX_DISCOVERY_WORKERS})"
        )
    else:
        max_discovery = st.number_input(
            "Max concurrent discovery workers",
            min_value=1,
            max_value=MAX_DISCOVERY_WORKERS,
            value=min(4, max(1, len(plan_preview) or 1)),
            help=(
                f"How many pipelines run at once (1–{MAX_DISCOVERY_WORKERS}). "
                f"Plan still has {len(plan_preview)} pipeline(s); extras queue."
            ),
            key="max_discovery",
        )
        st.caption(
            f"Plan {len(plan_preview)} pipeline(s) · concurrent ×{int(max_discovery)} "
            f"(max {MAX_DISCOVERY_WORKERS})"
        )
    delay_min = st.number_input(
        "Minimum delay between ZIPs (seconds)",
        min_value=0.5,
        value=DEFAULT_DELAY_MIN,
        step=0.5,
        key="delay_min",
    )
    delay_max = st.number_input(
        "Maximum delay between ZIPs (seconds)",
        min_value=1.0,
        value=DEFAULT_DELAY_MAX,
        step=0.5,
        key="delay_max",
    )

    with st.expander("Proxy settings", icon=":material/vpn_lock:"):
        proxy_from_env = ProxyManager.from_env()
        use_proxies = st.toggle(
            "Use DataImpulse residential proxies",
            value=PROXY_ENABLED and proxy_from_env.configured,
            key="use_proxies",
            help=(
                "Reads credentials from `.env`. Sticky mode keeps one IP per ZIP "
                "while paginating (ports 10000-20000)."
            ),
        )
        mode_default = PROXY_MODE if PROXY_MODE in PROXY_MODES else "sticky"
        proxy_mode = st.selectbox(
            "Proxy mode",
            options=list(PROXY_MODES),
            index=list(PROXY_MODES).index(mode_default),
            disabled=not use_proxies,
            help=(
                "sticky: same residential IP for all pages of one ZIP "
                "(port 10000-20000). rotating: IP changes often (port 823)."
            ),
            key="proxy_mode",
        )
        targeting_default = (
            PROXY_TARGETING if PROXY_TARGETING in TARGETING_LEVELS else "country"
        )
        proxy_targeting = st.selectbox(
            "Geo targeting",
            options=list(TARGETING_LEVELS),
            index=list(TARGETING_LEVELS).index(targeting_default),
            disabled=not use_proxies,
            help=(
                "country = 1x traffic (recommended). "
                "state/city/zip = Target Filters at 2x traffic."
            ),
            key="proxy_targeting",
        )
        if use_proxies:
            if proxy_from_env.configured:
                port_hint = (
                    "10000-20000 sticky"
                    if proxy_mode == "sticky"
                    else f"{proxy_from_env.port} rotating"
                )
                st.caption(
                    f"Using `{proxy_from_env.host}` · {port_hint} · "
                    f"login `{proxy_from_env.login[:6]}…`"
                )
            else:
                st.warning(
                    "Proxy credentials missing. Add DATAIMPULSE_LOGIN / "
                    "DATAIMPULSE_PASSWORD to `.env`.",
                    icon=":material/warning:",
                )
            if proxy_targeting == "country":
                st.caption("Country targeting only — standard traffic rate.")
            else:
                st.caption(
                    "Target Filters selected — DataImpulse bills this traffic at 2x."
                )
        proxy_text = st.text_area(
            "Optional static proxy URLs (override)",
            placeholder="http://user:pass@host:port",
            disabled=not use_proxies,
            height=90,
            help="Leave empty to use DataImpulse credentials from `.env`.",
            key="proxy_text",
        )

    plan = plan_preview
    if plan:
        sample = ", ".join(
            f"{row.get('label') or row.get('state')}={row['quota']}" for row in plan[:8]
        )
        st.caption(
            f"Plan: {len(plan)} city/state pipeline(s) · {sample}"
            + ("…" if len(plan) > 8 else "")
        )
        effective_disc = (
            auto_disc if auto_parallel else int(max_discovery)
        )
        if effective_disc < len(plan):
            st.caption(
                f"Note: only {effective_disc} of {len(plan)} pipelines run at once; "
                "others queue."
            )

    snapshot = run_state.snapshot()
    start_col, stop_col = st.columns(2)
    start = start_col.button(
        "Start",
        type="primary",
        icon=":material/play_arrow:",
        disabled=snapshot["running"] or not search_term.strip(),
        width="stretch",
    )
    stop = stop_col.button(
        "Stop",
        icon=":material/stop:",
        disabled=not snapshot["running"],
        width="stretch",
    )

with st.container(border=True):
    status_heading = st.empty()
    overall_progress = st.progress(0.0, text="Ready")

kpi_columns = st.columns(5, border=True)
kpi_companies = kpi_columns[0].empty()
kpi_pipelines = kpi_columns[1].empty()
kpi_zips = kpi_columns[2].empty()
kpi_pages = kpi_columns[3].empty()
kpi_reviews = kpi_columns[4].empty()

pipeline_container = st.container(border=True)
with pipeline_container:
    st.subheader("Parallel pipeline activity")
    st.caption(
        "Each row is a discovery worker. Deep scan paginates further when the "
        "normal ZIP pass is short of quota."
    )
    pipeline_slot = st.empty()

results_container = st.container(border=True)
with results_container:
    st.subheader("Companies")
    results_caption = st.empty()
    results_slot = st.empty()
    download_slot = st.empty()

with st.expander("Run log", icon=":material/receipt_long:"):
    log_slot = st.empty()

if stop and snapshot["running"]:
    run_state.stop_event.set()
    run_state.append_log("Stop requested — finishing current requests…")
    st.toast("Stopping after the current requests finish.", icon=":material/stop:")

if start and search_term.strip() and not snapshot["running"]:
    worker = run_state.worker
    if worker is not None and worker.is_alive():
        st.warning("A scrape is already running.")
    else:
        with run_state.lock:
            run_state.running = True
            run_state.stop_event.clear()
            run_state.results = []
            run_state.companies_total = 0
            run_state.with_reviews = 0
            run_state.csv_path = ""
            run_state.xlsx_path = ""
            run_state.logs = []
            run_state.progress = {}
            run_state.run_error = ""
            run_state.run_message = ""

        proxy_urls = [
            proxy.strip()
            for proxy in (proxy_text or "").splitlines()
            if proxy.strip()
        ] if use_proxies else []

        config = {
            "search_term": search_term,
            "countries": countries,
            "states": states,
            "cities": cities,
            "limit": int(limit),
            "per_zip_cap": int(per_zip_cap),
            "delay_min": float(delay_min),
            "delay_max": float(max(delay_max, delay_min)),
            "proxy_urls": proxy_urls,
            "use_proxies": use_proxies,
            "proxy_targeting": proxy_targeting if use_proxies else None,
            "proxy_mode": proxy_mode if use_proxies else None,
            "max_discovery_workers": 0 if auto_parallel else int(max_discovery),
        }
        thread = threading.Thread(
            target=run_worker,
            args=(config, run_state),
            name="maps-scraper-ui",
            daemon=True,
        )
        run_state.worker = thread
        thread.start()
        disc = auto_disc if auto_parallel else int(max_discovery)
        run_state.append_log(
            f"Queued scrape for {len(plan)} pipeline(s) · discovery×{disc}."
        )
        st.rerun()

snapshot = run_state.snapshot()

def _show_results_preview(snap: dict) -> None:
    preview_df = companies_to_df(snap["results"])
    total = int(snap["companies_total"] or len(preview_df))
    if preview_df.empty:
        if not snap["running"]:
            results_caption.empty()
            results_slot.info("Results will appear here during a run.")
        return
    results_caption.caption(
        f"Showing latest {len(preview_df):,} of {total:,} "
        f"(full export under `{OUTPUT_DIR.name}/`)"
    )
    results_slot.dataframe(preview_df, hide_index=True, width="stretch")


if snapshot["running"]:
    info = snapshot["progress"]
    if info:
        render_progress(
            info,
            total_target=int(limit),
            results=snapshot["results"],
            logs=snapshot["logs"],
            companies_total=snapshot["companies_total"],
            with_reviews=snapshot["with_reviews"],
        )
    else:
        status_heading.markdown("**Starting pipelines…**")
        if snapshot["logs"]:
            log_slot.code("\n".join(snapshot["logs"][-300:]))
    _show_results_preview(snapshot)
    time.sleep(1.2)
    st.rerun()

info = snapshot["progress"]
if info:
    render_progress(
        info,
        total_target=int(limit),
        results=snapshot["results"],
        logs=snapshot["logs"],
        companies_total=snapshot["companies_total"],
        with_reviews=snapshot["with_reviews"],
    )
else:
    status_heading.markdown("**Ready**")
    kpi_companies.metric("Unique companies", "0")
    kpi_pipelines.metric("Active pipelines", "0 / 0")
    kpi_zips.metric("ZIP codes used", "0 / 0")
    kpi_pages.metric("Pages / requests", "0 / 0")
    kpi_reviews.metric("With review count", "0")
    pipeline_slot.info("Pipelines will appear after the run starts.")

_show_results_preview(snapshot)

csv_path = Path(snapshot["csv_path"]) if snapshot["csv_path"] else None
xlsx_path = Path(snapshot["xlsx_path"]) if snapshot["xlsx_path"] else None
if not snapshot["running"] and (csv_path or xlsx_path):
    with download_slot.container(horizontal=True):
        if csv_path and csv_path.is_file():
            st.download_button(
                "Download CSV",
                data=csv_path.read_bytes(),
                file_name=csv_path.name,
                mime="text/csv",
                icon=":material/download:",
            )
        if xlsx_path and xlsx_path.is_file():
            st.download_button(
                "Download Excel",
                data=xlsx_path.read_bytes(),
                file_name=xlsx_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                icon=":material/download:",
            )

if snapshot["run_error"]:
    st.error(snapshot["run_error"], icon=":material/error:")
elif snapshot["run_message"] and not snapshot["running"]:
    if "Stopped" in snapshot["run_message"] or "exhausted" in snapshot["run_message"]:
        st.warning(snapshot["run_message"], icon=":material/warning:")
    else:
        st.success(snapshot["run_message"], icon=":material/check_circle:")

if snapshot["logs"] and not snapshot["running"]:
    log_slot.code("\n".join(snapshot["logs"][-300:]))
