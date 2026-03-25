from __future__ import annotations

from typing import Protocol

from intelligence.models import QueryTask, RawSignal


class SourceConnector(Protocol):
    name: str

    def fetch(self, task: QueryTask, limit: int) -> list[RawSignal]:
        ...
