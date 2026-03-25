from __future__ import annotations

import re
from dataclasses import asdict

from intelligence.dedupe import build_signature, canonicalize_url
from intelligence.idf import detect_city, detect_department, detect_postal_code, get_department_by_city
from intelligence.models import EnrichedSignal, RawSignal, SearchScope


WORK_TYPE_KEYWORDS = {
    "gros oeuvre": ["gros oeuvre", "maconnerie", "beton", "structure"],
    "renovation": ["renovation", "rehabilitation", "rafraichissement"],
    "electricite": ["electricite", "electrique", "courant fort", "courant faible"],
    "plomberie": ["plomberie", "chauffage", "sanitaire", "cvc"],
    "peinture": ["peinture", "revetement", "enduit"],
    "maconnerie": ["maconnerie", "mur porteur", "cloison", "parpaing", "mortier"],
    "sols": [
        "sol",
        "sols",
        "carrelage",
        "parquet",
        "moquette",
        "lino",
        "revetement de sol",
    ],
    "isolation": ["isolation", "ite", "iti", "thermique"],
    "toiture": ["toiture", "couverture", "zinguerie"],
    "facade": ["facade", "ravalement"],
}

ANNOUNCEMENT_KEYWORDS = {
    "appel_offres": ["appel d'offres", "consultation", "dce", "marche public"],
    "chantier": ["chantier", "demarrage des travaux", "phase travaux"],
    "besoin": ["besoin", "recherche entreprise", "cherche artisan", "devis travaux"],
    "annonce": ["annonce", "publication", "avis"],
}

MONTH_PATTERN = (
    "janvier|fevrier|mars|avril|mai|juin|juillet|aout|"
    "septembre|octobre|novembre|decembre"
)



def enrich_signal(raw: RawSignal, scope: SearchScope, score: int) -> EnrichedSignal:
    combined_text = f"{raw.title} {raw.summary}".strip()
    location_city = detect_city(combined_text) or scope.city

    department = None
    if location_city:
        department = get_department_by_city(location_city)
    if not department:
        department = detect_department(combined_text)

    postal_code = detect_postal_code(combined_text)
    work_types = extract_work_types(combined_text)
    announcement_type = classify_announcement(combined_text)
    budget_min, budget_max = extract_budget_range(combined_text)
    deadline_text = extract_deadline(combined_text)
    email, phone = extract_contact(combined_text)

    canonical_url = canonicalize_url(raw.url)
    signature_hash = build_signature(raw.title, raw.summary, canonical_url)

    return EnrichedSignal(
        source_name=raw.source_name,
        source_channel=raw.source_channel,
        source_query=raw.query,
        title=raw.title,
        url=raw.url,
        canonical_url=canonical_url,
        summary=raw.summary,
        published_at=raw.published_at,
        location_city=location_city,
        location_department_code=department.code if department else scope.department_code,
        location_department_name=department.name if department else scope.department_name,
        postal_code=postal_code,
        work_types=work_types,
        announcement_type=announcement_type,
        budget_min=budget_min,
        budget_max=budget_max,
        deadline_text=deadline_text,
        contact_email=email,
        contact_phone=phone,
        score=score,
        signature_hash=signature_hash,
        raw_payload=asdict(raw),
    )



def extract_work_types(text: str) -> list[str]:
    lowered = (text or "").lower()
    hits = [work_type for work_type, keywords in WORK_TYPE_KEYWORDS.items() if any(k in lowered for k in keywords)]
    return hits[:8] if hits else ["non_classe"]



def classify_announcement(text: str) -> str:
    lowered = (text or "").lower()
    for announcement_type, keywords in ANNOUNCEMENT_KEYWORDS.items():
        if any(k in lowered for k in keywords):
            return announcement_type
    return "non_classe"



def extract_budget_range(text: str) -> tuple[int | None, int | None]:
    cleaned = re.sub(r"\s+", " ", text or "")

    range_match = re.search(
        r"(\d{2,3}(?:[\s.]\d{3})+|\d{4,7})\s*(?:€|eur|euros|k)?"
        r"\s*(?:a|-|a\s+|et|jusqu'?a)\s*"
        r"(\d{2,3}(?:[\s.]\d{3})+|\d{4,7})\s*(?:€|eur|euros|k)?",
        cleaned,
        flags=re.I,
    )
    if range_match:
        low = _parse_money(range_match.group(1))
        high = _parse_money(range_match.group(2))
        if low and high:
            return (min(low, high), max(low, high))

    single = re.search(r"(\d{2,3}(?:[\s.]\d{3})+|\d{4,7})\s*(?:€|eur|euros|k)\b", cleaned, flags=re.I)
    if single:
        value = _parse_money(single.group(1))
        if value:
            return value, value

    return None, None



def extract_deadline(text: str) -> str | None:
    value = text or ""

    ddmmyyyy = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", value)
    if ddmmyyyy:
        return ddmmyyyy.group(1)

    month_expr = re.search(rf"\b(\d{{1,2}}\s+(?:{MONTH_PATTERN})\s+\d{{4}})\b", value, flags=re.I)
    if month_expr:
        return month_expr.group(1)

    relative = re.search(r"\b(sous\s+\d+\s+(?:jours?|semaines?|mois)|urgent|asap)\b", value, flags=re.I)
    if relative:
        return relative.group(1)

    return None



def extract_contact(text: str) -> tuple[str | None, str | None]:
    email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text or "", flags=re.I)

    phone = None
    phone_match = re.search(r"(?:\+33[\s.\-]?|0)[1-9](?:[\s.\-]?\d{2}){4}", text or "")
    if phone_match:
        phone = phone_match.group(0)

    return (email_match.group(0).lower() if email_match else None, phone)



def _parse_money(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None

    value = int(digits)
    if value < 100:
        return None

    if value < 1500:
        # Heuristic for shorthand like 50k when only '50' captured.
        return value * 1000

    if value > 20_000_000:
        return None

    return value
