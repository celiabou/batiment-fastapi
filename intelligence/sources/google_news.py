from __future__ import annotations

import ssl
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from intelligence.models import QueryTask, RawSignal


class GoogleNewsRSSSource:
    name = "google_news_rss"

    def __init__(self, timeout_seconds: int = 10):
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl.create_default_context()

    def fetch(self, task: QueryTask, limit: int) -> list[RawSignal]:
        url = (
            "https://news.google.com/rss/search"
            f"?q={quote_plus(task.query)}&hl=fr&gl=FR&ceid=FR:fr"
        )

        req = Request(url, headers={"User-Agent": "Mozilla/5.0 EurobatBot/1.0"})

        try:
            with urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                payload = response.read()
        except Exception:
            return []

        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            return []

        signals: list[RawSignal] = []
        for item in root.findall("./channel/item")[:limit]:
            title = _text(item.find("title"))
            link = _text(item.find("link"))
            description = _text(item.find("description"))
            pub_date_raw = _text(item.find("pubDate"))

            published_at = None
            if pub_date_raw:
                try:
                    published_at = parsedate_to_datetime(pub_date_raw)
                except Exception:
                    published_at = None

            if not link or not title:
                continue

            signals.append(
                RawSignal(
                    source_name=self.name,
                    source_channel=task.channel,
                    query=task.query,
                    title=title,
                    url=link,
                    summary=description,
                    published_at=published_at,
                    payload={
                        "feed": "google_news",
                        "scope": task.scope.label,
                        "lot": task.lot,
                        "intent": task.intent,
                    },
                )
            )
        return signals



def _text(node) -> str:
    if node is None or node.text is None:
        return ""
    return unescape(node.text.strip())
