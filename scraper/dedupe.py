"""Deduplicate company results."""

from __future__ import annotations

from .models import Company


class DedupeStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self.companies: list[Company] = []

    def add(self, company: Company) -> bool:
        key = company.dedupe_key()
        if not key or key in self._seen:
            return False
        # Also block empty-name
        if not company.name.strip():
            return False
        self._seen.add(key)
        self.companies.append(company)
        return True

    def add_many(self, companies: list[Company]) -> int:
        added = 0
        for c in companies:
            if self.add(c):
                added += 1
        return added

    def __len__(self) -> int:
        return len(self.companies)
