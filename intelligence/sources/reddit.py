from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from intelligence.models import QueryTask, RawSignal


class RedditSource:
    name = "reddit"

    def __init__(self, timeout_seconds: int = 10):
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context()

    def fetch(self, task: QueryTask, limit: int) -> list[RawSignal]:
        # Keep Reddit search focused on social tasks and French BTP signals.
        if task.channel != "social":
            return []

        url = (
            "https://www.reddit.com/search.json"
            f"?q={quote_plus(task.query)}&sort=new&limit={max(1, min(limit, 25))}"
        )
        req = Request(
            url,
            headers={
                "User-Agent": "EurobatSocialCollector/1.0",
                "Accept": "application/json",
            },
        )

        try:
            with urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
        except Exception:
            return []

        children = data.get("data", {}).get("children", [])
        signals: list[RawSignal] = []
        for child in children[:limit]:
            post = child.get("data", {})
            title = (post.get("title") or "").strip()
            permalink = post.get("permalink")
            selftext = (post.get("selftext") or "").strip()
            created_utc = post.get("created_utc")

            if not title or not permalink:
                continue

            published_at = None
            if created_utc:
                try:
                    published_at = datetime.fromtimestamp(float(created_utc), UTC)
                except Exception:
                    published_at = None

            signals.append(
                RawSignal(
                    source_name=self.name,
                    source_channel=task.channel,
                    query=task.query,
                    title=title,
                    url=f"https://www.reddit.com{permalink}",
                    summary=selftext,
                    published_at=published_at,
                    payload={
                        "score": post.get("score"),
                        "subreddit": post.get("subreddit"),
                        "scope": task.scope.label,
                        "lot": task.lot,
                        "intent": task.intent,
                    },
                )
            )

        return signals
