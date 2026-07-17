"""Scraper configuration and Google Maps request templates."""

from __future__ import annotations

import random
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = ROOT_DIR / "output"
DEBUG_DIR = ROOT_DIR / "debug"
LOCATION_JSON_PATH = DATA_DIR / "location_pincodes.json"

# Defaults
DEFAULT_PER_ZIP_CAP = 20
PAGE_SIZE = 20
MAX_PAGES_PER_ZIP = 10
MIN_CITY_QUOTA = 20
INTRA_ZIP_DELAY_MIN = 1.0
INTRA_ZIP_DELAY_MAX = 3.0
DEFAULT_DELAY_MIN = 2.0
DEFAULT_DELAY_MAX = 6.0
DEFAULT_HL = "en"
DEFAULT_GL = "us"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

# Chrome-like headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    # Prefer gzip/deflate — requests decodes these natively.
    # Brotli (br) needs the optional `brotli` package; we handle it in the client.
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.google.com/maps/",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Consent cookie to avoid EU consent interstitial
DEFAULT_COOKIES = {
    "CONSENT": "YES+cb.20240716-00-p0.en+FX+411",
}

# US geographic center fallback when zip has no coords
DEFAULT_LAT = 39.8283
DEFAULT_LNG = -98.5795
DEFAULT_SPAN = 50_000.0  # meters viewport span

# pb template from captured Maps search. Placeholders: {span}, {lng}, {lat}
# Most feature flags are kept verbatim so Google returns the same rich payload.
PB_TEMPLATE = (
    "!4m12!1m3!1d{span}!2d{lng}!3d{lat}"
    "!2m3!1f0!2f0!3f0!3m2!1i1024!2i768!4f13.1!7i20!8i{offset}!10b1"
    "!12m26!1m5!18b1!30b1!31m1!1b1!34e1!2m4!5m1!6e2!20e3!39b1"
    "!10b1!12b1!13b1!16b1!17m1!3e1!20m4!5e2!6b1!8b1!14b1!46m1!1b0"
    "!96b1!99b1!19m4!2m3!1i360!2i120!4i8"
    "!20m65!2m2!1i203!2i100!3m2!2i4!5b1!6m6!1m2!1i86!2i86!1m2!1i408!2i240"
    "!7m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3"
    "!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3!1m3!1e10!2b1!3e2"
    "!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!9b0"
    "!15m16!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
    "!1m7!1m2!1m1!1e2!2m2!1i195!2i195!3i20"
    "!24m109!1m27!13m9!2b1!3b1!4b1!6i1!8b1!9b1!14b1!20b1!25b1"
    "!18m16!3b1!4b1!5b1!6b1!9b1!13b1!14b1!17b1!20b1!21b1!22b1!32b1"
    "!33m1!1b1!34b1!36e2!10m1!8e3!11m1!3e1!17b1!20m2!1e3!1e6"
    "!24b1!25b1!26b1!27b1!29b1!30m1!2b1!36b1!37b1!39m3!2m2!2i1!3i1"
    "!43b1!52b1!54m1!1b1!55b1!56m1!1b1!61m2!1m1!1e1"
    "!65m5!3m4!1m3!1m2!1i224!2i298"
    "!72m22!1m8!2b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!4b1"
    "!8m10!1m6!4m1!1e1!4m1!1e3!4m1!1e4"
    "!3sother_user_google_review_posts__and__hotel_and_vr_partner_review_posts"
    "!6m1!1e1!9b1!89b1!90m2!1m1!1e2!98m3!1b1!2b1!3b1!103b1!113b1"
    "!114m3!1b1!2m1!1b1!117b1!122m1!1b1!126b1!127b1!128m1!1b0"
    "!26m4!2m3!1i80!2i92!4i8!30m0"
    "!34m19!2b1!3b1!4b1!6b1!8m6!1b1!3b1!4b1!5b1!6b1!7b1"
    "!9b1!12b1!14b1!20b1!23b1!25b1!26b1!31b1"
    "!37m1!1e81!42b1!47m0!49m10!3b1!6m2!1b1!2b1!7m2!1e3!2b1!8b1!9b1!10e2"
    "!50m4!2e2!3m2!1b1!3b1!67m5!7b1!10b1!14b1!15m1!1b0!69i786!77b1"
)

GMAPS_SEARCH_URL = "https://www.google.com/search"


def build_pb(
    lat: float = DEFAULT_LAT,
    lng: float = DEFAULT_LNG,
    span: float = DEFAULT_SPAN,
    offset: int = 0,
) -> str:
    return PB_TEMPLATE.format(span=span, lng=lng, lat=lat, offset=max(0, int(offset)))


def random_delay(min_s: float = DEFAULT_DELAY_MIN, max_s: float = DEFAULT_DELAY_MAX) -> float:
    return random.uniform(min_s, max_s)


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
