from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG = {
    "querying": {
        "lots": [
            "peinture",
            "plomberie",
            "electricite",
            "maconnerie",
            "sols",
        ],
        "intents": [
            "appel d'offres",
            "besoin travaux",
            "annonce chantier",
            "recherche entreprise",
            "sous-traitance",
        ],
        "platform_domains": [
            "boamp.fr",
            "marchesonline.com",
            "francemarches.com",
            "batiweb.com",
            "leboncoin.fr",
        ],
        "social_domains": [
            "linkedin.com",
            "facebook.com",
            "x.com",
            "instagram.com",
        ],
        "max_queries_per_scope": 24,
    },
    "sources": {
        "google_news_enabled": True,
        "google_news_per_query": 6,
        "reddit_enabled": True,
        "reddit_per_query": 3,
        "serpapi_enabled": True,
        "serpapi_per_query": 5,
        "http_timeout_seconds": 10,
    },
    "scoring": {
        "min_score": 35,
    },
    "geo": {
        "default_department_codes": ["75", "92", "93", "94"],
        "cities_per_department": 3,
    },
}


@dataclass
class Config:
    raw: dict

    @property
    def querying(self) -> dict:
        return self.raw["querying"]

    @property
    def sources(self) -> dict:
        return self.raw["sources"]

    @property
    def scoring(self) -> dict:
        return self.raw["scoring"]

    @property
    def geo(self) -> dict:
        return self.raw["geo"]



def _deep_merge(base: dict, update: dict) -> dict:
    merged = dict(base)
    for key, value in update.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged



def load_config(config_path: str | Path | None = None) -> Config:
    if not config_path:
        return Config(raw=DEFAULT_CONFIG)

    path = Path(config_path)
    if not path.exists():
        return Config(raw=DEFAULT_CONFIG)

    try:
        user_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Config(raw=DEFAULT_CONFIG)

    merged = _deep_merge(DEFAULT_CONFIG, user_data)
    return Config(raw=merged)
