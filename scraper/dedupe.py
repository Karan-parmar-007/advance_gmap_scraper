"""Deduplicate company results."""

from __future__ import annotations

from threading import Lock

from .models import Company


class DedupeStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.companies: list[Company] = []
        self._with_reviews: int = 0
        self._lock = Lock()

    def add(self, company: Company) -> bool:
        with self._lock:
            return self._add_unlocked(company)

    def add_many(self, companies: list[Company], max_total: int = 0) -> list[Company]:
        """Add unique companies; return the ones newly accepted."""
        accepted: list[Company] = []
        with self._lock:
            for company in companies:
                if max_total and len(self.companies) >= max_total:
                    break
                if self._add_unlocked(company):
                    accepted.append(company)
        return accepted

    def __len__(self) -> int:
        with self._lock:
            return len(self.companies)

    def snapshot(self) -> list[Company]:
        with self._lock:
            return list(self.companies)

    def _add_unlocked(self, company: Company) -> bool:
        key = company.dedupe_key()
        if not key or key in self._seen:
            return False
        if not company.name.strip():
            return False
        self._seen.add(key)
        self.companies.append(company)
        if company.review_count is not None:
            self._with_reviews += 1
        return True

    def with_reviews_count(self) -> int:
        with self._lock:
            return self._with_reviews
