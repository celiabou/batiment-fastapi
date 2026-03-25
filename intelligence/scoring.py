from __future__ import annotations

from datetime import UTC, datetime

from intelligence.models import EnrichedSignal, RawSignal, SearchScope


SOURCE_WEIGHTS = {
    "google_news_rss": 8,
    "serpapi": 10,
    "reddit": 6,
}

ANNOUNCEMENT_WEIGHTS = {
    "appel_offres": 16,
    "chantier": 14,
    "besoin": 12,
    "annonce": 8,
    "non_classe": 4,
}



def base_score(raw: RawSignal, scope: SearchScope) -> int:
    score = 20
    score += SOURCE_WEIGHTS.get(raw.source_name, 5)

    if raw.source_channel == "platform":
        score += 10
    elif raw.source_channel == "social":
        score += 6
    else:
        score += 4

    text = f"{raw.title} {raw.summary}".lower()
    if scope.city and scope.city.lower() in text:
        score += 12
    elif scope.department_code in text or scope.department_name.lower() in text:
        score += 8

    if raw.published_at:
        now = datetime.now(UTC)
        published = raw.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_days = (now - published.astimezone(UTC)).total_seconds() / 86400
        if age_days <= 2:
            score += 12
        elif age_days <= 7:
            score += 8
        elif age_days <= 30:
            score += 4

    return max(0, min(score, 100))



def finalize_score(signal: EnrichedSignal) -> int:
    score = signal.score
    score += ANNOUNCEMENT_WEIGHTS.get(signal.announcement_type, 4)

    if signal.contact_email or signal.contact_phone:
        score += 14
    if signal.budget_min or signal.budget_max:
        score += 10
    if signal.deadline_text:
        score += 8
    if signal.work_types and signal.work_types[0] != "non_classe":
        score += 8
    if signal.location_city:
        score += 8

    return max(0, min(score, 100))
