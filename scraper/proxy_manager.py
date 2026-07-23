"""DataImpulse residential proxy manager with sticky sessions + target filters."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .locations import ZipLocation

# Docs:
# - Target filters: https://docs.dataimpulse.com/proxies/targeting/target-filters
# - Sticky ports 10000-20000: https://docs.dataimpulse.com/proxies/types-of-connections
# - sessid alternative on 823: https://docs.dataimpulse.com/proxies/parameters/session-id
TARGETING_LEVELS = ("country", "state", "city", "zip")
PROXY_MODES = ("sticky", "rotating")
FALLBACK_CHAIN = ("zip", "city", "state", "country")

STICKY_PORT_MIN = 10000
STICKY_PORT_MAX = 20000
ROTATING_PORT = 823

COUNTRY_CODES = {
    "united states": "us",
    "usa": "us",
    "us": "us",
    "u.s.": "us",
    "u.s.a.": "us",
    "canada": "ca",
    "mexico": "mx",
    "united kingdom": "gb",
    "uk": "gb",
    "germany": "de",
    "france": "fr",
    "spain": "es",
    "brazil": "br",
    "australia": "au",
    "india": "in",
}


@dataclass
class ProxyStat:
    url: str
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0


@dataclass
class ProxyTarget:
    country_code: str = ""
    state: str = ""
    city: str = ""
    zip_code: str = ""

    @classmethod
    def from_location(cls, loc: ZipLocation | None) -> "ProxyTarget":
        if loc is None:
            return cls()
        return cls(
            country_code=country_to_code(loc.country),
            state=normalize_filter_value(loc.state),
            city=normalize_filter_value(loc.city),
            zip_code=str(loc.zip_code or "").strip(),
        )


@dataclass
class ProxyManager:
    """
    Build DataImpulse residential proxy URLs.

    Sticky mode (recommended for ZIP pagination):
      http://login__cr.us;sessid.zip60608:pass@gw.dataimpulse.com:12457
      Ports 10000-20000 keep the same IP for ~1-120 minutes.

    Rotating mode:
      http://login__cr.us:pass@gw.dataimpulse.com:823
    """

    login: str = ""
    password: str = ""
    host: str = "gw.dataimpulse.com"
    port: int = ROTATING_PORT
    sticky_port_min: int = STICKY_PORT_MIN
    sticky_port_max: int = STICKY_PORT_MAX
    protocol: str = "http"
    enabled: bool = False
    mode: str = "sticky"
    targeting: str = "country"
    fallback: bool = True
    use_sessid: bool = True
    max_failures: int = 3
    cooldown_seconds: float = 300.0
    proxies: list[str] = field(default_factory=list)
    enable_rotation: bool = False
    _stats: dict[str, ProxyStat] = field(default_factory=dict, init=False)
    _current_level: str = field(default="", init=False)
    _session_key: str | None = field(default=None, init=False, repr=False)
    _session_port: int | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.login = (self.login or "").strip()
        self.password = (self.password or "").strip()
        self.host = (self.host or "gw.dataimpulse.com").strip()
        self.port = int(self.port or ROTATING_PORT)
        self.sticky_port_min = int(self.sticky_port_min or STICKY_PORT_MIN)
        self.sticky_port_max = int(self.sticky_port_max or STICKY_PORT_MAX)
        if self.sticky_port_max < self.sticky_port_min:
            self.sticky_port_min, self.sticky_port_max = STICKY_PORT_MIN, STICKY_PORT_MAX
        self.protocol = (self.protocol or "http").strip().lower()
        self.mode = (self.mode or "sticky").strip().lower()
        if self.mode not in PROXY_MODES:
            self.mode = "sticky"
        self.targeting = (self.targeting or "country").strip().lower()
        if self.targeting not in TARGETING_LEVELS:
            self.targeting = "country"
        self._current_level = self.targeting
        for proxy in self.proxies:
            self._stats[proxy] = ProxyStat(url=proxy)

    @classmethod
    def from_env(
        cls,
        *,
        enabled: bool | None = None,
        targeting: str | None = None,
        fallback: bool | None = None,
        mode: str | None = None,
    ) -> "ProxyManager":
        env_enabled = _env_bool("PROXY_ENABLED", False)
        return cls(
            login=os.getenv("DATAIMPULSE_LOGIN", ""),
            password=os.getenv("DATAIMPULSE_PASSWORD", ""),
            host=os.getenv("DATAIMPULSE_HOST", "gw.dataimpulse.com"),
            port=int(os.getenv("DATAIMPULSE_PORT", str(ROTATING_PORT)) or ROTATING_PORT),
            sticky_port_min=int(
                os.getenv("DATAIMPULSE_STICKY_PORT_MIN", str(STICKY_PORT_MIN))
                or STICKY_PORT_MIN
            ),
            sticky_port_max=int(
                os.getenv("DATAIMPULSE_STICKY_PORT_MAX", str(STICKY_PORT_MAX))
                or STICKY_PORT_MAX
            ),
            protocol=os.getenv("DATAIMPULSE_PROTOCOL", "http"),
            enabled=env_enabled if enabled is None else enabled,
            mode=(mode or os.getenv("PROXY_MODE", "sticky") or "sticky"),
            targeting=(targeting or os.getenv("PROXY_TARGETING", "country") or "country"),
            fallback=_env_bool("PROXY_FALLBACK", True) if fallback is None else fallback,
            use_sessid=_env_bool("PROXY_USE_SESSID", True),
        )

    @property
    def configured(self) -> bool:
        return bool(self.login and self.password and self.host)

    def begin_sticky_session(self, session_key: str) -> int:
        """
        Bind subsequent requests to one sticky port for this ZIP/session.

        Different keys map to different ports in 10000-20000 so parallel
        ZIP scrapes do not share the same residential IP.
        """
        key = (session_key or "default").strip() or "default"
        port = self._port_for_session(key)
        with self._lock:
            self._session_key = key
            self._session_port = port
        return port

    def end_sticky_session(self) -> None:
        with self._lock:
            self._session_key = None
            self._session_port = None

    def get_proxy(
        self,
        *,
        target: ProxyTarget | ZipLocation | None = None,
        level: str | None = None,
        session_key: str | None = None,
    ) -> dict[str, str] | None:
        """Return requests-compatible proxies dict, or None for direct connection."""
        if self.enable_rotation and self.proxies:
            return self._next_static_proxy()

        if not self.enabled or not self.configured:
            return None

        proxy_target = (
            target
            if isinstance(target, ProxyTarget)
            else ProxyTarget.from_location(target)
        )
        level = (level or self._current_level or self.targeting).lower()

        if session_key and self.mode == "sticky":
            self.begin_sticky_session(session_key)

        url = self.build_proxy_url(
            target=proxy_target,
            level=level,
            session_key=self._session_key,
        )
        if url not in self._stats:
            self._stats[url] = ProxyStat(url=url)
        return {"http": url, "https": url}

    def build_proxy_url(
        self,
        *,
        target: ProxyTarget | None = None,
        level: str | None = None,
        session_key: str | None = None,
    ) -> str:
        username = self.build_username(
            target=target,
            level=level,
            session_key=session_key if session_key is not None else self._session_key,
        )
        password = quote(self.password, safe="")
        port = self.resolve_port(session_key if session_key is not None else self._session_key)
        return f"{self.protocol}://{username}:{password}@{self.host}:{port}"

    def build_username(
        self,
        *,
        target: ProxyTarget | None = None,
        level: str | None = None,
        session_key: str | None = None,
    ) -> str:
        target = target or ProxyTarget()
        level = (level or self.targeting).lower()
        filters = self._filters_for_level(target, level)

        # Keep the same IP for one ZIP's pagination via sessid (works with sticky ports too).
        if self.mode == "sticky" and self.use_sessid:
            key = session_key if session_key is not None else self._session_key
            if key:
                filters.append(f"sessid.{normalize_filter_value(key)}")

        if not filters:
            return self.login
        joined = ";".join(filters)
        return f"{self.login}__{joined}"

    def resolve_port(self, session_key: str | None = None) -> int:
        if self.mode != "sticky":
            return self.port or ROTATING_PORT
        key = session_key if session_key is not None else self._session_key
        if key:
            return self._port_for_session(key)
        if self._session_port:
            return self._session_port
        return self.sticky_port_min

    def next_fallback_level(self, current: str | None = None) -> str | None:
        if not self.fallback:
            return None
        current = (current or self._current_level or self.targeting).lower()
        try:
            index = FALLBACK_CHAIN.index(current)
        except ValueError:
            return None
        if index + 1 >= len(FALLBACK_CHAIN):
            return None
        return FALLBACK_CHAIN[index + 1]

    def set_level(self, level: str) -> None:
        level = (level or self.targeting).lower()
        if level in TARGETING_LEVELS:
            self._current_level = level

    def reset_level(self) -> None:
        self._current_level = self.targeting

    def report_success(self, proxy: dict[str, str] | None) -> None:
        if not proxy:
            return
        url = proxy.get("https") or proxy.get("http")
        if url and url in self._stats:
            self._stats[url].successes += 1
            self._stats[url].failures = 0

    def report_failure(self, proxy: dict[str, str] | None) -> None:
        if not proxy:
            return
        url = proxy.get("https") or proxy.get("http")
        if not url or url not in self._stats:
            return
        stat = self._stats[url]
        stat.failures += 1
        if stat.failures >= self.max_failures:
            stat.cooldown_until = time.time() + self.cooldown_seconds
            stat.failures = 0

    def _port_for_session(self, session_key: str) -> int:
        digest = hashlib.md5(session_key.encode("utf-8")).hexdigest()
        span = self.sticky_port_max - self.sticky_port_min + 1
        return self.sticky_port_min + (int(digest[:8], 16) % span)

    def _filters_for_level(self, target: ProxyTarget, level: str) -> list[str]:
        country = (target.country_code or "").lower()
        if not country:
            return []

        filters = [f"cr.{country}"]
        if level == "country":
            return filters

        if level == "state":
            if target.state:
                filters.append(f"state.{target.state}")
            return filters

        if level == "city":
            if target.city:
                filters.append(f"city.{target.city}")
            elif target.state:
                filters.append(f"state.{target.state}")
            return filters

        # zip
        if target.zip_code:
            filters.append(f"zip.{target.zip_code}")
            return filters
        if target.city:
            filters.append(f"city.{target.city}")
            return filters
        if target.state:
            filters.append(f"state.{target.state}")
        return filters

    def _next_static_proxy(self) -> dict[str, str] | None:
        if not self.proxies:
            return None
        now = time.time()
        start = getattr(self, "_static_index", 0)
        for offset in range(len(self.proxies)):
            index = (start + offset) % len(self.proxies)
            url = self.proxies[index]
            stat = self._stats.setdefault(url, ProxyStat(url=url))
            if stat.cooldown_until > now:
                continue
            self._static_index = index + 1
            return {"http": url, "https": url}
        return None


def country_to_code(country: str | None) -> str:
    value = (country or "").strip().lower()
    if not value:
        return "us"
    if len(value) == 2:
        return value
    return COUNTRY_CODES.get(value, value[:2])


def normalize_filter_value(value: str | None) -> str:
    """Lowercase geo/session names for DataImpulse filters."""
    text = (value or "").strip().lower()
    if not text:
        return ""
    cleaned = []
    prev_underscore = False
    for char in text:
        if char.isalnum():
            cleaned.append(char)
            prev_underscore = False
        elif char in {" ", "-", "_", ":"} and not prev_underscore:
            cleaned.append("_")
            prev_underscore = True
    return "".join(cleaned).strip("_")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
