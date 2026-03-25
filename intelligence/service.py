from __future__ import annotations

import json
from pathlib import Path

from database import SessionLocal
from intelligence.config import Config, load_config
from intelligence.extractors import enrich_signal
from intelligence.models import CaptureResult, CaptureStats, QueryTask
from intelligence.query_builder import QueryBuilder
from intelligence.repository import list_signals, upsert_signal
from intelligence.scoring import base_score, finalize_score
from intelligence.sources import GoogleNewsRSSSource, RedditSource, SerpAPISource


class IntelligenceService:
    def __init__(
        self,
        config_path: str | None = None,
        sources: list | None = None,
        session_factory=SessionLocal,
    ):
        base_dir = Path(__file__).resolve().parents[1]
        config_file = config_path or str(base_dir / "content" / "intelligence_config.json")

        self.config: Config = load_config(config_file)
        self.query_builder = QueryBuilder(self.config)
        self.session_factory = session_factory
        self.sources = sources if sources is not None else self._build_sources()

    def _build_sources(self) -> list:
        timeout = int(self.config.sources.get("http_timeout_seconds", 10))
        sources: list = []

        if self.config.sources.get("google_news_enabled", True):
            sources.append(GoogleNewsRSSSource(timeout_seconds=timeout))

        if self.config.sources.get("reddit_enabled", True):
            sources.append(RedditSource(timeout_seconds=timeout))

        if self.config.sources.get("serpapi_enabled", True):
            serpapi = SerpAPISource(timeout_seconds=timeout)
            if serpapi.enabled:
                sources.append(serpapi)

        return sources

    def preview_queries(
        self,
        department_codes: list[str] | None = None,
        cities: list[str] | None = None,
        max_queries: int = 30,
    ) -> list[dict]:
        scopes = self.query_builder.build_scopes(department_codes=department_codes, cities=cities)
        tasks = self.query_builder.build_queries(scopes, max_queries=max_queries)
        return [
            {
                "query": task.query,
                "channel": task.channel,
                "scope": task.scope.label,
                "lot": task.lot,
                "intent": task.intent,
            }
            for task in tasks
        ]

    def run_capture(
        self,
        department_codes: list[str] | None = None,
        cities: list[str] | None = None,
        max_queries: int = 80,
        dry_run: bool = False,
        min_score: int | None = None,
    ) -> CaptureResult:
        effective_min_score = (
            int(min_score)
            if min_score is not None
            else int(self.config.scoring.get("min_score", 35))
        )

        stats = CaptureStats()
        scopes = self.query_builder.build_scopes(department_codes=department_codes, cities=cities)
        tasks = self.query_builder.build_queries(scopes, max_queries=max_queries)

        stats.scopes = len(scopes)
        stats.queries = len(tasks)

        dedupe: set[str] = set()
        enriched_signals = []

        for task in tasks:
            for source in self.sources:
                try:
                    limit = self._per_query_limit_for_source(source.name)
                    raw_signals = source.fetch(task, limit=limit)
                except Exception:
                    stats.errors += 1
                    continue

                stats.fetched += len(raw_signals)

                for raw in raw_signals:
                    signal = self._enrich_with_score(raw, task)
                    if signal.signature_hash in dedupe:
                        continue
                    dedupe.add(signal.signature_hash)

                    if signal.score < effective_min_score:
                        continue

                    enriched_signals.append(signal)

        stats.deduped = len(enriched_signals)

        if not dry_run and enriched_signals:
            session = self.session_factory()
            try:
                for signal in enriched_signals:
                    action, _row = upsert_signal(session, signal)
                    if action == "inserted":
                        stats.inserted += 1
                    elif action == "updated":
                        stats.updated += 1
                session.commit()
            finally:
                session.close()

        top_signals = sorted(enriched_signals, key=lambda s: s.score, reverse=True)[:10]

        return CaptureResult(
            stats=stats,
            sample_queries=[t.query for t in tasks[:12]],
            sample_urls=[s.url for s in top_signals[:12]],
            top_signals=[
                {
                    "score": s.score,
                    "title": s.title,
                    "url": s.url,
                    "city": s.location_city,
                    "department_code": s.location_department_code,
                    "announcement_type": s.announcement_type,
                    "work_types": s.work_types,
                }
                for s in top_signals
            ],
        )

    def get_signals(
        self,
        limit: int = 50,
        min_score: int = 0,
        department_code: str | None = None,
        city: str | None = None,
        announcement_type: str | None = None,
    ) -> list[dict]:
        session = self.session_factory()
        try:
            rows = list_signals(
                session=session,
                limit=limit,
                min_score=min_score,
                department_code=department_code,
                city=city,
                announcement_type=announcement_type,
            )
            serialized = []
            for row in rows:
                work_types = _safe_json_loads(row.work_types, fallback=[])
                payload = _safe_json_loads(row.raw_payload, fallback={})
                serialized.append(
                    {
                        "id": row.id,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "source_name": row.source_name,
                        "source_channel": row.source_channel,
                        "source_query": row.source_query,
                        "title": row.title,
                        "summary": row.summary,
                        "url": row.url,
                        "location_city": row.location_city,
                        "location_department_code": row.location_department_code,
                        "location_department_name": row.location_department_name,
                        "postal_code": row.postal_code,
                        "work_types": work_types,
                        "announcement_type": row.announcement_type,
                        "budget_min": row.budget_min,
                        "budget_max": row.budget_max,
                        "deadline_text": row.deadline_text,
                        "contact_email": row.contact_email,
                        "contact_phone": row.contact_phone,
                        "score": row.score,
                        "raw_payload": payload,
                    }
                )
            return serialized
        finally:
            session.close()

    def _enrich_with_score(self, raw, task: QueryTask):
        score = base_score(raw, task.scope)
        signal = enrich_signal(raw, task.scope, score=score)
        signal.score = finalize_score(signal)
        return signal

    def _per_query_limit_for_source(self, source_name: str) -> int:
        if source_name == "google_news_rss":
            return int(self.config.sources.get("google_news_per_query", 6))
        if source_name == "reddit":
            return int(self.config.sources.get("reddit_per_query", 3))
        if source_name == "serpapi":
            return int(self.config.sources.get("serpapi_per_query", 5))
        return 5



def _safe_json_loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback
