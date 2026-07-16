# Google Maps Company Scraper

HTTP scraper for Google Maps local results (no browser). Searches by ZIP from `data/All Segment Zip Combinations.xlsx`, extracts company fields, and exposes a Streamlit UI + CLI.

## Features

- Search by term + country / state(s) / city(cities)
- Always queries as `{term} {zip}` against random matching ZIPs
- One request per ZIP (keeps up to N results from that response; default 20)
- Dedupes by Maps place id / name+phone
- Extracts: name, phone, website, rating, reviews (when present), address, categories, lat/lng, place id
- Proxy-ready (`ProxyManager`) for residential rotation later

## Setup

```bash
pip install -r requirements.txt
```

## Streamlit UI

```bash
streamlit run app.py
```

## CLI

```bash
python cli.py plumber --state Illinois --city Chicago --limit 50
python cli.py "web design" --state TX --limit 100 --per-zip 20
python cli.py plumber --country "United States" --limit 200
```

## Project layout

```
app.py                 # Streamlit UI
cli.py                 # Command-line runner
scraper/
  config.py            # pb template, headers, delays
  gmaps_client.py      # HTTP client (tbm=map)
  parser.py            # Unwrap + extract companies
  locations.py         # Excel ZIP pool filters
  runner.py            # Zip loop + limits + export
  proxy_manager.py     # Direct now; rotation later
  models.py / dedupe.py
data/                  # ZIP Excel
output/                # CSV / XLSX exports
debug/                 # Failed/raw payloads
```

## Notes

- Google’s internal JSON layout can change; the parser prefers `payload[64]` place cards and falls back to a recursive scan.
- Use delays (2–6s) to reduce blocks. Wire residential proxies in the UI when ready.
- Review counts are not always present in every response variant; rating/phone/website/address usually are.
