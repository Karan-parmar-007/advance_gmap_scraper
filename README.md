# Google Maps Company Scraper

HTTP scraper for Google Maps local results (no browser). Searches by ZIP/pincode from `data/location_pincodes.json`, extracts company fields, and exposes a Streamlit UI + CLI.

## Features

- Search by term + country / state(s) / city(cities)
- Always queries as `{term} {zip}` against random matching ZIPs
- When a total limit is set, it is divided evenly across selected countries, then states, then cities (city split only if each city would get at least 20)
- Runs selected states concurrently (configurable with `--max-parallel`)
- Paginates within each ZIP (`!8i{offset}` in `pb`) — 20 results per page, up to your per-ZIP target (default 20)
- If a location misses its quota, deep-scans every ZIP up to 10 pages each
- Transfers any remaining shortfall to selected pipelines that still have capacity
- Dedupes by Maps place id / name+phone
- DataImpulse residential proxies with geo target filters (`zip` / `city` / `state` / `country`)
- Extracts: name, phone, website, rating, reviews (when present), address, categories, lat/lng, place id
- Proxy-ready (`ProxyManager`) for residential rotation later

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in DataImpulse credentials
```

## Proxies (DataImpulse)

Credentials live in `.env`:

```env
DATAIMPULSE_LOGIN=...
DATAIMPULSE_PASSWORD=...
DATAIMPULSE_HOST=gw.dataimpulse.com
DATAIMPULSE_PORT=823
PROXY_ENABLED=true
PROXY_MODE=sticky
PROXY_TARGETING=country
```

| Mode | Port | Behavior |
|------|------|----------|
| `sticky` | `10000–20000` | Same IP for all pages of one ZIP |
| `rotating` | `823` | IP can change every request |

| Targeting | Traffic cost |
|-----------|--------------|
| `country` | 1x (recommended) |
| `state` / `city` / `zip` | 2x Target Filters |

Sticky URL example (country only):

```text
http://login__cr.us;sessid.united_states_10001:pass@gw.dataimpulse.com:12457
```

## Streamlit UI

```bash
streamlit run app.py
```

## CLI

```bash
python cli.py plumber --state Illinois --city Chicago --limit 50
python cli.py "web design" --state TX --limit 100 --per-zip 60
python cli.py plumber --country "United States" --limit 200
python cli.py "it companies" --state Illinois --state Massachusetts --limit 600 --max-parallel 2
python cli.py plumber --state Illinois --city Chicago --limit 50 --use-proxy --proxy-targeting zip
```

## Project layout

```
app.py                 # Streamlit UI
cli.py                 # Command-line runner
scraper/
  config.py            # pb template, headers, delays
  gmaps_client.py      # HTTP client (tbm=map)
  parser.py            # Unwrap + extract companies
  locations.py         # JSON pincode pool filters
  quotas.py            # Hierarchical limit distribution
  runner.py            # Unit/zip loop + limits + export
  proxy_manager.py     # DataImpulse residential + target filters
  runner.py            # Unit/zip loop + limits + export
  models.py / dedupe.py
.env / .env.example    # Proxy credentials (never commit .env)
data/                  # location_pincodes.json
output/                # CSV / XLSX exports
debug/                 # Failed/raw payloads
```

## Notes

- Google’s internal JSON layout can change; the parser prefers `payload[64]` place cards and falls back to a recursive scan.
- Use delays (2–6s) to reduce blocks. Wire residential proxies in the UI when ready.
- Review counts are not always present in every response variant; rating/phone/website/address usually are.
