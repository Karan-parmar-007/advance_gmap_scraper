"""Streamlit dashboard for the concurrent Google Maps company scraper."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import pandas as pd
import streamlit as st

from scraper.config import (
    DEFAULT_DELAY_MAX,
    DEFAULT_DELAY_MIN,
    DEFAULT_PER_ZIP_CAP,
    MAX_PAGES_PER_ZIP,
    PAGE_SIZE,
    PROXY_ENABLED,
    PROXY_MODE,
    PROXY_TARGETING,
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
    results: list = field(default_factory=list)
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
            # Keep UI updates light: store company rows only as plain dicts.
            companies = info.get("companies") or []
            if companies and hasattr(companies[0], "to_dict"):
                companies = [company.to_dict() for company in companies]
            payload = dict(info)
            payload["companies"] = companies
            run_state.progress = payload
            run_state.results = companies

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
                    f"Starting {len(pipelines)} pipeline(s): "
                    + ", ".join(
                        f"{p.get('label')}={p.get('target')}" for p in pipelines
                    )
                )
            run_state.logs = run_state.logs[-300:]

    return on_progress


def render_progress(
    info: dict,
    *,
    total_target: int,
    results: list,
    logs: list[str],
) -> None:
    found = int(info.get("companies_found") or 0)
    active = int(info.get("pipelines_active") or 0)
    pipeline_total = int(info.get("pipelines_total") or 0)
    zips_used = int(info.get("zips_used") or 0)
    zips_total = int(info.get("zips_total") or 0)
    pages = int(info.get("pages_fetched") or 0)
    requests = int(info.get("requests_made") or 0)
    round_number = int(info.get("redistribution_round") or 0)

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

    results_df = companies_to_df(results)
    if not results_df.empty:
        results_slot.dataframe(results_df, hide_index=True, width="stretch")

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
            max_parallel_pipelines=config["max_parallel"],
            on_progress=make_progress_callback(run_state),
            should_stop=run_state.stop_event.is_set,
        )
        companies = runner.run()
        company_rows = [
            company.to_dict() if hasattr(company, "to_dict") else company
            for company in companies
        ]
        with run_state.lock:
            run_state.results = company_rows
            if run_state.stop_event.is_set():
                run_state.run_message = (
                    f"Stopped early with {len(company_rows):,} unique companies."
                )
            elif config["limit"] and len(company_rows) < config["limit"]:
                run_state.run_message = (
                    f"Capacity exhausted: collected {len(company_rows):,} of "
                    f"{config['limit']:,} requested companies."
                )
            elif company_rows:
                csv_path = runner.export_csv()
                xlsx_path = runner.export_excel()
                run_state.run_message = (
                    f"Collected {len(company_rows):,} unique companies. "
                    f"Saved as {csv_path.name} and {xlsx_path.name}."
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
    "Concurrent state pipelines with hierarchical quotas, global deduplication, "
    "deep ZIP pagination, and automatic shortfall redistribution."
)

with st.sidebar:
    st.header("Run configuration")
    search_term = st.text_input(
        "Search term",
        placeholder="IT companies, plumber, web design…",
        key="search_term",
    )

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
            "Optional. Cities narrow ZIPs inside a state when they match. "
            "States without a matching city still keep their full ZIP pool "
            "and equal quota share."
        ),
    )

    st.subheader("Targets")
    limit = st.number_input(
        "Total unique companies",
        min_value=0,
        value=50,
        step=10,
        help=(
            "0 means unlimited. A positive target is divided evenly across "
            "selected states. Unmet quota is transferred to pipelines that "
            "still have ZIP capacity."
        ),
        key="limit",
    )
    per_zip_cap = st.number_input(
        "Initial results per ZIP",
        min_value=1,
        max_value=PAGE_SIZE * MAX_PAGES_PER_ZIP,
        value=DEFAULT_PER_ZIP_CAP,
        help=(
            "Normal pass target per ZIP. If a location remains below quota after "
            "all ZIPs are tried, the deep pass revisits every ZIP and paginates "
            f"up to {MAX_PAGES_PER_ZIP} pages."
        ),
        key="per_zip_cap",
    )

    st.subheader("Parallelism and pacing")
    max_parallel = st.number_input(
        "Maximum parallel pipelines",
        min_value=1,
        max_value=16,
        value=4,
        help="Each selected state runs as an independent worker, up to this limit.",
        key="max_parallel",
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

    plan = preview_pipeline_plan(
        limit=int(limit),
        countries=countries or None,
        states=states or None,
        cities=cities or None,
    )
    if plan:
        st.caption(
            f"Plan: {len(plan)} state pipeline(s) · "
            + ", ".join(f"{row['state']}={row['quota']}" for row in plan)
        )
        if int(max_parallel) < len(plan):
            st.caption(
                f"Note: only {int(max_parallel)} of {len(plan)} pipelines can run "
                "at once with the current parallel limit."
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

kpi_columns = st.columns(4, border=True)
kpi_companies = kpi_columns[0].empty()
kpi_pipelines = kpi_columns[1].empty()
kpi_zips = kpi_columns[2].empty()
kpi_pages = kpi_columns[3].empty()

pipeline_container = st.container(border=True)
with pipeline_container:
    st.subheader("Parallel pipeline activity")
    st.caption(
        "Each row is an independent state worker. Deep scan means the normal ZIP "
        "pass was insufficient and additional pages are being fetched."
    )
    pipeline_slot = st.empty()

results_container = st.container(border=True)
with results_container:
    st.subheader("Companies")
    results_slot = st.empty()

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
            "max_parallel": int(max_parallel),
        }
        thread = threading.Thread(
            target=run_worker,
            args=(config, run_state),
            name="maps-scraper-ui",
            daemon=True,
        )
        run_state.worker = thread
        thread.start()
        run_state.append_log(
            f"Queued scrape for {len(plan)} state pipeline(s) "
            f"(max parallel {int(max_parallel)})."
        )
        st.rerun()

snapshot = run_state.snapshot()

if snapshot["running"]:
    info = snapshot["progress"]
    if info:
        render_progress(
            info,
            total_target=int(limit),
            results=snapshot["results"],
            logs=snapshot["logs"],
        )
    else:
        status_heading.markdown("**Starting pipelines…**")
        if snapshot["logs"]:
            log_slot.code("\n".join(snapshot["logs"][-300:]))
    time.sleep(0.8)
    st.rerun()

info = snapshot["progress"]
if info:
    render_progress(
        info,
        total_target=int(limit),
        results=snapshot["results"],
        logs=snapshot["logs"],
    )
else:
    status_heading.markdown("**Ready**")
    kpi_companies.metric("Unique companies", "0")
    kpi_pipelines.metric("Active pipelines", "0 / 0")
    kpi_zips.metric("ZIP codes used", "0 / 0")
    kpi_pages.metric("Pages / requests", "0 / 0")
    pipeline_slot.info("Pipelines will appear after the run starts.")

results_df = companies_to_df(snapshot["results"])
if not results_df.empty:
    results_slot.dataframe(results_df, hide_index=True, width="stretch")
    download_row = st.container(horizontal=True)
    with download_row:
        st.download_button(
            "Download CSV",
            data=results_df.to_csv(index=False).encode("utf-8"),
            file_name=f"maps_results_{int(time.time())}.csv",
            mime="text/csv",
            icon=":material/download:",
        )
        excel_buffer = BytesIO()
        results_df.to_excel(excel_buffer, index=False)
        st.download_button(
            "Download Excel",
            data=excel_buffer.getvalue(),
            file_name=f"maps_results_{int(time.time())}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            icon=":material/download:",
        )
elif not snapshot["running"]:
    results_slot.info("Results will appear here during a run.")

if snapshot["run_error"]:
    st.error(snapshot["run_error"], icon=":material/error:")
elif snapshot["run_message"] and not snapshot["running"]:
    if "Stopped" in snapshot["run_message"] or "exhausted" in snapshot["run_message"]:
        st.warning(snapshot["run_message"], icon=":material/warning:")
    else:
        st.success(snapshot["run_message"], icon=":material/check_circle:")

if snapshot["logs"]:
    log_slot.code("\n".join(snapshot["logs"][-300:]))
