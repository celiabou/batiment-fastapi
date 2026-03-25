from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from intelligence.models import EnrichedSignal
from models import OpportunitySignal



def upsert_signal(session: Session, signal: EnrichedSignal) -> tuple[str, OpportunitySignal]:
    existing = session.execute(
        select(OpportunitySignal).where(OpportunitySignal.signature_hash == signal.signature_hash)
    ).scalar_one_or_none()

    if existing is None:
        row = OpportunitySignal(
            source_name=signal.source_name,
            source_channel=signal.source_channel,
            source_query=signal.source_query,
            title=signal.title,
            summary=signal.summary,
            url=signal.url,
            canonical_url=signal.canonical_url,
            signature_hash=signal.signature_hash,
            published_at=signal.published_at,
            location_city=signal.location_city,
            location_department_code=signal.location_department_code,
            location_department_name=signal.location_department_name,
            postal_code=signal.postal_code,
            work_types=json.dumps(signal.work_types, ensure_ascii=False),
            announcement_type=signal.announcement_type,
            budget_min=signal.budget_min,
            budget_max=signal.budget_max,
            deadline_text=signal.deadline_text,
            contact_email=signal.contact_email,
            contact_phone=signal.contact_phone,
            score=signal.score,
            raw_payload=json.dumps(signal.raw_payload, ensure_ascii=False),
            updated_at=datetime.now(UTC),
        )
        session.add(row)
        return "inserted", row

    changed = False

    if signal.score > (existing.score or 0):
        existing.score = signal.score
        changed = True

    fields = [
        "summary",
        "location_city",
        "location_department_code",
        "location_department_name",
        "postal_code",
        "announcement_type",
        "budget_min",
        "budget_max",
        "deadline_text",
        "contact_email",
        "contact_phone",
    ]

    for field in fields:
        current = getattr(existing, field)
        incoming = getattr(signal, field)
        if current in (None, "") and incoming not in (None, ""):
            setattr(existing, field, incoming)
            changed = True

    if signal.published_at and existing.published_at is None:
        existing.published_at = signal.published_at
        changed = True

    incoming_work_types = json.dumps(signal.work_types, ensure_ascii=False)
    if incoming_work_types != existing.work_types and signal.work_types:
        existing.work_types = incoming_work_types
        changed = True

    if changed:
        existing.updated_at = datetime.now(UTC)
        existing.raw_payload = json.dumps(signal.raw_payload, ensure_ascii=False)
        return "updated", existing

    return "skipped", existing



def list_signals(
    session: Session,
    limit: int = 50,
    min_score: int = 0,
    department_code: str | None = None,
    city: str | None = None,
    announcement_type: str | None = None,
) -> list[OpportunitySignal]:
    stmt = select(OpportunitySignal).where(OpportunitySignal.score >= min_score)

    if department_code:
        stmt = stmt.where(OpportunitySignal.location_department_code == department_code)
    if city:
        stmt = stmt.where(OpportunitySignal.location_city == city)
    if announcement_type:
        stmt = stmt.where(OpportunitySignal.announcement_type == announcement_type)

    stmt = stmt.order_by(OpportunitySignal.score.desc(), OpportunitySignal.updated_at.desc()).limit(max(1, min(limit, 500)))

    return list(session.execute(stmt).scalars())
