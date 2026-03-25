from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
}


def canonicalize_url(url: str) -> str:
    if not url:
        return ""

    parts = urlsplit(url.strip())
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
    clean_query = urlencode(query_pairs)
    clean_path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), clean_path, clean_query, ""))



def build_signature(title: str, summary: str, canonical_url: str) -> str:
    if canonical_url:
        source = canonical_url
    else:
        source = f"{title.strip().lower()}::{summary.strip().lower()[:500]}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
