"""Streamlit dashboard for the concurrent Google Maps company scraper."""

from __future__ import annotations

import time
from io import BytesIO

import pandas as pd
import streamlit as st

from scraper.config import (
    DEFAULT_DELAY_MAX,
    DEFAULT_DELAY_MIN,
    DEFAULT_PER_ZIP_CAP,
    MAX_PAGES_PER_ZIP,
    PAGE_SIZE,
    ensure_dirs,
)
from scraper.locations import list_cities, list_countries, list_states
from scraper.runner import ScraperRunner


st.set_page_config(
    page_title="Maps company scraper",
    page_icon=":material/travel_explore:",
    layout="wide",
)
ensure_dirs()

st.session_state.setdefault("running", False)
st.session_state.setdefault("results", [])
st.session_state.setdefault("progress", {})
st.session_state.setdefault("logs", [])


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
    cities = st.multiselect("Cities", options=city_options, key="cities")

    st.subheader("Targets")
    limit = st.number_input(
        "Total unique companies",
        min_value=0,
        value=50,
        step=10,
        help=(
            "0 means unlimited. A positive target is divided across selected "
            "countries, states, and eligible cities. Unmet quota is transferred "
            "to pipelines that still have ZIP capacity."
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
        use_proxies = st.toggle("Use proxy rotation", value=False, key="use_proxies")
        proxy_text = st.text_area(
            "Proxy URLs",
            placeholder="http://user:pass@host:port",
            disabled=not use_proxies,
            height=110,
            help="One proxy URL per line.",
            key="proxy_text",
        )

    start = st.button(
        "Start scraping",
        type="primary",
        icon=":material/play_arrow:",
        disabled=st.session_state.running or not search_term.strip(),
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
        "Each row is an independent state/country worker. Deep scan means the "
        "normal ZIP pass was insufficient and additional pages are being fetched."
    )
    pipeline_slot = st.empty()

results_container = st.container(border=True)
with results_container:
    st.subheader("Companies")
    results_slot = st.empty()

with st.expander("Run log", icon=":material/receipt_long:"):
    log_slot = st.empty()


def render_progress(info: dict, total_target: int) -> None:
    st.session_state.progress = info
    st.session_state.results = info.get("companies") or []

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

    results_df = companies_to_df(st.session_state.results)
    if not results_df.empty:
        results_slot.dataframe(results_df, hide_index=True, width="stretch")

    event = info.get("event")
    if event == "pipeline_done":
        pipeline = info.get("pipeline") or {}
        st.session_state.logs.append(
            f"{pipeline.get('label')}: {pipeline.get('status')} — "
            f"{pipeline.get('collected')}/{pipeline.get('target')}, "
            f"{pipeline.get('zips_used')} ZIPs, {pipeline.get('pages')} pages"
        )
    elif event == "redistribution_start":
        allocations = ", ".join(
            f"{item['label']} +{item['extra']}"
            for item in info.get("allocations", [])
        )
        st.session_state.logs.append(
            f"Redistribution round {info.get('round')}: {allocations}"
        )
    elif event == "deep_scan_start":
        st.session_state.logs.append(
            f"Deep pagination started for {info.get('location')}"
        )
    elif event == "error":
        st.session_state.logs.append(f"ERROR: {info.get('message')}")

    if st.session_state.logs:
        log_slot.code("\n".join(st.session_state.logs[-300:]))


if start and search_term.strip():
    st.session_state.running = True
    st.session_state.results = []
    st.session_state.logs = []
    st.session_state.progress = {}
    proxy_urls = [
        proxy.strip()
        for proxy in (proxy_text or "").splitlines()
        if proxy.strip()
    ] if use_proxies else []

    runner = ScraperRunner(
        search_term=search_term,
        countries=countries,
        states=states,
        cities=cities,
        limit=int(limit),
        per_zip_cap=int(per_zip_cap),
        delay_min=float(delay_min),
        delay_max=float(max(delay_max, delay_min)),
        proxy_urls=proxy_urls,
        use_proxies=use_proxies,
        max_parallel_pipelines=int(max_parallel),
        on_progress=lambda info: render_progress(info, int(limit)),
    )

    try:
        with st.spinner("Running parallel pipelines…", show_time=True):
            companies = runner.run()
        st.session_state.results = companies
        if companies:
            csv_path = runner.export_csv()
            xlsx_path = runner.export_excel()
            if int(limit) and len(companies) < int(limit):
                st.warning(
                    f"All selected location capacity was exhausted. Collected "
                    f"{len(companies):,} of {int(limit):,} requested companies.",
                    icon=":material/warning:",
                )
            else:
                st.success(
                    f"Collected {len(companies):,} unique companies. "
                    f"Saved as {csv_path.name} and {xlsx_path.name}.",
                    icon=":material/check_circle:",
                )
        else:
            st.warning(
                "No companies were collected. Check the filters and run log.",
                icon=":material/warning:",
            )
    finally:
        st.session_state.running = False

results = st.session_state.results
results_df = companies_to_df(results)
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
elif not st.session_state.running:
    results_slot.info("Results will appear here during a run.")

if not st.session_state.progress:
    status_heading.markdown("**Ready**")
    kpi_companies.metric("Unique companies", "0")
    kpi_pipelines.metric("Active pipelines", "0 / 0")
    kpi_zips.metric("ZIP codes used", "0 / 0")
    kpi_pages.metric("Pages / requests", "0 / 0")
    pipeline_slot.info("Pipelines will appear after the run starts.")
