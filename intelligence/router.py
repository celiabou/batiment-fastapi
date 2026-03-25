from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from intelligence.service import IntelligenceService


router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])
_service: IntelligenceService | None = None


def _get_service() -> IntelligenceService:
    global _service
    if _service is None:
        _service = IntelligenceService()
    return _service


class RunCaptureRequest(BaseModel):
    department_codes: list[str] | None = Field(default=None)
    cities: list[str] | None = Field(default=None)
    max_queries: int = Field(default=80, ge=1, le=1000)
    min_score: int | None = Field(default=None, ge=0, le=100)
    dry_run: bool = Field(default=False)


class QueryPreviewRequest(BaseModel):
    department_codes: list[str] | None = Field(default=None)
    cities: list[str] | None = Field(default=None)
    max_queries: int = Field(default=30, ge=1, le=300)


@router.get("/health")
def intelligence_health() -> dict:
    service = _get_service()
    return {
        "ok": True,
        "sources": [source.name for source in service.sources],
        "min_score": service.config.scoring.get("min_score", 35),
    }


@router.post("/queries/preview")
def preview_queries(payload: QueryPreviewRequest) -> dict:
    service = _get_service()
    preview = service.preview_queries(
        department_codes=payload.department_codes,
        cities=payload.cities,
        max_queries=payload.max_queries,
    )
    return {
        "count": len(preview),
        "queries": preview,
    }


@router.post("/run")
def run_capture(payload: RunCaptureRequest) -> dict:
    service = _get_service()
    result = service.run_capture(
        department_codes=payload.department_codes,
        cities=payload.cities,
        max_queries=payload.max_queries,
        dry_run=payload.dry_run,
        min_score=payload.min_score,
    )

    return {
        "stats": {
            "scopes": result.stats.scopes,
            "queries": result.stats.queries,
            "fetched": result.stats.fetched,
            "deduped": result.stats.deduped,
            "inserted": result.stats.inserted,
            "updated": result.stats.updated,
            "errors": result.stats.errors,
        },
        "sample_queries": result.sample_queries,
        "sample_urls": result.sample_urls,
        "top_signals": result.top_signals,
    }


@router.get("/signals")
def read_signals(
    limit: int = 50,
    min_score: int = 0,
    department_code: str | None = None,
    city: str | None = None,
    announcement_type: str | None = None,
) -> dict:
    service = _get_service()
    signals = service.get_signals(
        limit=limit,
        min_score=min_score,
        department_code=department_code,
        city=city,
        announcement_type=announcement_type,
    )
    return {"count": len(signals), "signals": signals}
