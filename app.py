"""Streamlit UI for the Google Maps company scraper."""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import streamlit as st

from scraper.config import (
    DEFAULT_DELAY_MAX,
    DEFAULT_DELAY_MIN,
    DEFAULT_PER_ZIP_CAP,
    MAX_PAGES_PER_ZIP,
    OUTPUT_DIR,
    PAGE_SIZE,
    ensure_dirs,
)
from scraper.locations import list_cities, list_countries, list_states
from scraper.runner import ScraperRunner

st.set_page_config(page_title="Maps Company Scraper", page_icon="📍", layout="wide")
ensure_dirs()

st.title("Google Maps Company Scraper")
st.caption("Query Google Maps by ZIP/pincode from location_pincodes.json — no browser automation.")

# Session state
if "stop_flag" not in st.session_state:
    st.session_state.stop_flag = False
if "running" not in st.session_state:
    st.session_state.running = False
if "results" not in st.session_state:
    st.session_state.results = []
if "progress" not in st.session_state:
    st.session_state.progress = {}
if "logs" not in st.session_state:
    st.session_state.logs = []


def companies_to_df(companies: list) -> pd.DataFrame:
    if not companies:
        return pd.DataFrame()
    if hasattr(companies[0], "to_dict"):
        return pd.DataFrame([c.to_dict() for c in companies])
    return pd.DataFrame(companies)


with st.sidebar:
    st.header("Search")
    search_term = st.text_input("Search term", placeholder="plumber, web design company, …")

    st.subheader("Location filters")
    countries = st.multiselect("Country", options=list_countries(), default=["United States"])
    states = st.multiselect("State(s)", options=list_states())
    city_options = list_cities(states if states else None)
    cities = st.multiselect("City / cities", options=city_options)

    st.subheader("Limits")
    limit = st.number_input(
        "Total companies wanted (0 = no limit)",
        min_value=0,
        value=50,
        step=10,
        help="Stop once this many unique companies are collected.",
    )
    per_zip_cap = st.number_input(
        "Results per ZIP",
        min_value=1,
        max_value=PAGE_SIZE * MAX_PAGES_PER_ZIP,
        value=DEFAULT_PER_ZIP_CAP,
        help=(
            "How many unique companies to collect from each ZIP. "
            "Requests 20 results per page and paginates until this target is met, "
            "results are exhausted, or no new companies appear."
        ),
    )

    st.subheader("Request pacing")
    delay_min = st.number_input("Min delay (s)", min_value=0.5, value=DEFAULT_DELAY_MIN, step=0.5)
    delay_max = st.number_input("Max delay (s)", min_value=1.0, value=DEFAULT_DELAY_MAX, step=0.5)

    st.subheader("Proxies (future)")
    use_proxies = st.checkbox("Use residential proxies", value=False, disabled=False)
    proxy_text = st.text_area(
        "Proxy list (one per line)",
        placeholder="http://user:pass@host:port",
        disabled=not use_proxies,
        height=100,
    )
    proxy_urls = [p.strip() for p in (proxy_text or "").splitlines() if p.strip()] if use_proxies else []

    col_a, col_b = st.columns(2)
    start = col_a.button("Start", type="primary", disabled=st.session_state.running or not search_term)
    stop = col_b.button("Stop", disabled=not st.session_state.running)

if stop:
    st.session_state.stop_flag = True

progress_bar = st.progress(0.0, text="Idle")
status_box = st.empty()
metrics = st.columns(4)
m_found = metrics[0].empty()
m_zips = metrics[1].empty()
m_last = metrics[2].empty()
m_status = metrics[3].empty()

table_slot = st.empty()
log_slot = st.expander("Run log", expanded=False)

if start and search_term:
    st.session_state.stop_flag = False
    st.session_state.running = True
    st.session_state.results = []
    st.session_state.logs = []
    st.session_state.progress = {}

    def on_progress(info: dict) -> None:
        st.session_state.progress = info
        st.session_state.results = info.get("companies") or []
        event = info.get("event")
        if event == "zip_done":
            st.session_state.logs.append(
                f"ZIP {info.get('zip')} ({info.get('city')}): "
                f"returned {info.get('returned')}, added {info.get('added')}"
            )
        elif event == "error":
            st.session_state.logs.append(f"ERROR: {info.get('message')}")

        tried = info.get("zips_tried") or 0
        total = max(info.get("zips_total") or 1, 1)
        progress_bar.progress(min(tried / total, 1.0), text=f"ZIPs {tried}/{total}")
        status_box.info(info.get("status") or "")
        m_found.metric("Companies", info.get("companies_found") or 0)
        m_zips.metric("ZIPs tried", f"{tried}/{info.get('zips_total') or 0}")
        m_last.metric("Last ZIP added", info.get("last_count") or 0)
        m_status.metric("Status", (info.get("status") or "")[:40])
        df_live = companies_to_df(st.session_state.results)
        if not df_live.empty:
            table_slot.dataframe(df_live, use_container_width=True, hide_index=True)

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
        on_progress=on_progress,
        should_stop=lambda: st.session_state.stop_flag,
    )

    with st.spinner("Scraping…"):
        companies = runner.run()

    st.session_state.results = companies
    st.session_state.running = False
    st.session_state.stop_flag = False

    # Final exports
    if companies:
        csv_path = runner.export_csv()
        xlsx_path = runner.export_excel()
        st.success(f"Done — {len(companies)} companies. Saved to `{csv_path.name}` and `{xlsx_path.name}`.")
    else:
        st.warning("Finished with 0 companies. Check filters, delays, or debug/ dumps.")

# Always show current results / downloads
results = st.session_state.results
df = companies_to_df(results)
if not df.empty:
    table_slot.dataframe(df, use_container_width=True, hide_index=True)
    c1, c2 = st.columns(2)
    c1.download_button(
        "Download CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"maps_results_{int(time.time())}.csv",
        mime="text/csv",
    )
    # Excel via buffer
    from io import BytesIO

    buf = BytesIO()
    df.to_excel(buf, index=False)
    c2.download_button(
        "Download Excel",
        data=buf.getvalue(),
        file_name=f"maps_results_{int(time.time())}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
else:
    table_slot.info("Results will appear here after a run.")

if st.session_state.logs:
    log_slot.code("\n".join(st.session_state.logs[-200:]))

prog = st.session_state.progress
if prog:
    m_found.metric("Companies", prog.get("companies_found") or len(results))
    m_zips.metric(
        "ZIPs tried",
        f"{prog.get('zips_tried') or 0}/{prog.get('zips_total') or 0}",
    )
