"""HTTP client for Google Maps search endpoint (no browser)."""

from __future__ import annotations

import random
import time
from typing import Any
from urllib.parse import urlencode

import requests

from .config import (
    DEFAULT_COOKIES,
    DEFAULT_GL,
    DEFAULT_HEADERS,
    DEFAULT_HL,
    DEFAULT_LAT,
    DEFAULT_LNG,
    DEFAULT_SPAN,
    GMAPS_SEARCH_URL,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    build_pb,
)
from .proxy_manager import ProxyManager


class BlockedError(RuntimeError):
    """Raised when Google returns a consent/CAPTCHA/block page."""


class GMapsClient:
    def __init__(
        self,
        *,
        hl: str = DEFAULT_HL,
        gl: str = DEFAULT_GL,
        proxy_manager: ProxyManager | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.hl = hl
        self.gl = gl
        self.proxy_manager = proxy_manager or ProxyManager()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.cookies.update(DEFAULT_COOKIES)
        self._zip_psi: str | None = None
        self._zip_ech = 0

    def close(self) -> None:
        self.session.close()

    def begin_zip_session(self) -> None:
        """Start a new Maps session for paginated requests within one ZIP."""
        self._zip_psi = self._make_psi()
        self._zip_ech = 0

    def end_zip_session(self) -> None:
        self._zip_psi = None
        self._zip_ech = 0

    def search(
        self,
        query: str,
        *,
        lat: float = DEFAULT_LAT,
        lng: float = DEFAULT_LNG,
        span: float = DEFAULT_SPAN,
        offset: int = 0,
        use_zip_session: bool = False,
    ) -> str:
        """
        Perform a Maps search and return raw response text.
        Raises BlockedError / requests.HTTPError on failure after retries.
        """
        if use_zip_session:
            if not self._zip_psi:
                self.begin_zip_session()
            self._zip_ech += 1
            psi = self._zip_psi
            ech = self._zip_ech
        else:
            psi = self._make_psi()
            ech = 1

        params = {
            "tbm": "map",
            "authuser": "0",
            "hl": self.hl,
            "gl": self.gl,
            "pb": build_pb(lat=lat, lng=lng, span=span, offset=offset),
            "q": query,
            "oq": query,
            "tch": "1",
            "ech": str(ech),
            "psi": psi,
        }

        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            proxy = self.proxy_manager.get_proxy()
            try:
                resp = self.session.get(
                    GMAPS_SEARCH_URL,
                    params=params,
                    proxies=proxy,
                    timeout=self.timeout,
                )
                if resp.status_code in (429, 503):
                    self.proxy_manager.report_failure(proxy)
                    last_err = BlockedError(f"HTTP {resp.status_code}")
                    time.sleep(2 ** attempt + random.random())
                    continue

                resp.raise_for_status()
                text = self._decode_body(resp)

                if self._looks_blocked(text):
                    self.proxy_manager.report_failure(proxy)
                    last_err = BlockedError("Consent/CAPTCHA/block page detected")
                    time.sleep(2 ** attempt + random.random())
                    continue

                self.proxy_manager.report_success(proxy)
                return text
            except requests.RequestException as exc:
                self.proxy_manager.report_failure(proxy)
                last_err = exc
                time.sleep(2 ** attempt + random.random())

        raise last_err or RuntimeError("Search failed")

    @staticmethod
    def _decode_body(resp: requests.Response) -> str:
        """Return decoded text, handling gzip/brotli edge cases."""
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        content = resp.content or b""

        # If requests already decoded (text looks like JSON), use it
        preview = content[:20]
        if preview.startswith(b"{") or preview.startswith(b")]}'") or preview.startswith(b"[["):
            return content.decode(resp.encoding or "utf-8", errors="replace")

        if "br" in encoding or (content[:1] == b"\x1b" or content[:4] == b"W\x00"):
            try:
                import brotli  # type: ignore

                content = brotli.decompress(content)
            except Exception:
                # Fall through — may already be decoded by urllib3
                pass

        if resp.encoding:
            return content.decode(resp.encoding, errors="replace")
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def _make_psi() -> str:
        # Session-ish token; Google accepts arbitrary-looking values
        stamp = int(time.time() * 1000)
        rand = "".join(random.choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_", k=22))
        return f"{rand}.{stamp}.1"

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        if not text or len(text) < 50:
            return True
        low = text[:2000].lower()
        # Healthy responses are JSON wrappers or )]}' arrays
        stripped = text.lstrip()
        if stripped.startswith("{") and '"d"' in stripped[:500]:
            return False
        if stripped.startswith(")]}'") or stripped.startswith("[["):
            return False
        # Block / consent signals
        markers = (
            "unusual traffic",
            "detected unusual traffic",
            "our systems have detected",
            "g-recaptcha",
            "consent.google.com",
            "before you continue",
            "captcha",
        )
        return any(m in low for m in markers)

    def build_url(self, query: str, lat: float = DEFAULT_LAT, lng: float = DEFAULT_LNG) -> str:
        params = {
            "tbm": "map",
            "hl": self.hl,
            "gl": self.gl,
            "pb": build_pb(lat=lat, lng=lng, offset=0),
            "q": query,
            "tch": "1",
        }
        return f"{GMAPS_SEARCH_URL}?{urlencode(params)}"
