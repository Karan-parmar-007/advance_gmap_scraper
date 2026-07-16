"""Proxy manager — stub for direct connections; ready for residential rotation."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProxyStat:
    url: str
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0


@dataclass
class ProxyManager:
    """
    V1: returns None (direct connection).
    Later: pass a list of proxy URLs like http://user:pass@host:port
    and enable_rotation=True.
    """

    proxies: list[str] = field(default_factory=list)
    enable_rotation: bool = False
    max_failures: int = 3
    cooldown_seconds: float = 300.0
    _cycle: Any = field(default=None, init=False, repr=False)
    _stats: dict[str, ProxyStat] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for p in self.proxies:
            self._stats[p] = ProxyStat(url=p)
        if self.proxies:
            self._cycle = itertools.cycle(self.proxies)

    def get_proxy(self) -> dict[str, str] | None:
        if not self.enable_rotation or not self.proxies:
            return None

        now = time.time()
        # Try up to len(proxies) candidates
        for _ in range(len(self.proxies)):
            url = next(self._cycle)
            stat = self._stats[url]
            if stat.cooldown_until > now:
                continue
            return {"http": url, "https": url}
        return None

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
