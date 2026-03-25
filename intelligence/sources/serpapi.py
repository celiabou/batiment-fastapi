from __future__ import annotations

import json
import os
import ssl
from datetime import UTC, datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from intelligence.models import QueryTask, RawSignal


class SerpAPISource:
    name = "serpapi"

    def __init__(self, timeout_seconds: int = 10):
        self.api_key = (os.getenv("SERPAPI_API_KEY") or "").strip()
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch(self, task: QueryTask, limit: int) -> list[RawSignal]:
        if not self.enabled:
            return []

        url = (
            "https://serpapi.com/search.json"
            "?engine=google"
            f"&q={quote_plus(task.query)}"
            "&gl=fr&hl=fr"
            f"&num={max(1, min(limit, 10))}"
            f"&api_key={quote_plus(self.api_key)}"
        )

        req = Request(url, headers={"User-Agent": "Mozilla/5.0 EurobatBot/1.0"})
        try:
            with urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
        except Exception:
            return []

        results = data.get("organic_results", [])
        now = datetime.now(UTC)
        signals: list[RawSignal] = []
        for result in results[:limit]:
            link = (result.get("link") or "").strip()
            title = (result.get("title") or "").strip()
            snippet = (result.get("snippet") or "").strip()
            if not link or not title:
                continue

            signals.append(
                RawSignal(
                    source_name=self.name,
                    source_channel=task.channel,
                    query=task.query,
                    title=title,
                    url=link,
                    summary=snippet,
                    published_at=now,
                    payload={
                        "position": result.get("position"),
                        "scope": task.scope.label,
                        "lot": task.lot,
                        "intent": task.intent,
                    },
                )
            )
        return signals
