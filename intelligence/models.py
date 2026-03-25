from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SearchScope:
    department_code: str
    department_name: str
    city: str | None = None

    @property
    def label(self) -> str:
        if self.city:
            return f"{self.city} ({self.department_code})"
        return f"{self.department_name} ({self.department_code})"


@dataclass(frozen=True)
class QueryTask:
    query: str
    scope: SearchScope
    channel: str
    lot: str
    intent: str


@dataclass
class RawSignal:
    source_name: str
    source_channel: str
    query: str
    title: str
    url: str
    summary: str
    published_at: datetime | None = None
    payload: dict = field(default_factory=dict)


@dataclass
class EnrichedSignal:
    source_name: str
    source_channel: str
    source_query: str
    title: str
    url: str
    canonical_url: str
    summary: str
    published_at: datetime | None

    location_city: str | None
    location_department_code: str | None
    location_department_name: str | None
    postal_code: str | None

    work_types: list[str]
    announcement_type: str
    budget_min: int | None
    budget_max: int | None
    deadline_text: str | None
    contact_email: str | None
    contact_phone: str | None

    score: int
    signature_hash: str
    raw_payload: dict = field(default_factory=dict)


@dataclass
class CaptureStats:
    scopes: int = 0
    queries: int = 0
    fetched: int = 0
    deduped: int = 0
    inserted: int = 0
    updated: int = 0
    errors: int = 0


@dataclass
class CaptureResult:
    stats: CaptureStats
    sample_queries: list[str]
    sample_urls: list[str]
    top_signals: list[dict]
