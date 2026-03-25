#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from intelligence.service import IntelligenceService  # noqa: E402



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Eurobat: auto-requetage geolocalise IDF (plateformes, Google, social)."
    )
    parser.add_argument("--departments", default="", help="Codes departements separes par virgules, ex: 75,92,93")
    parser.add_argument("--cities", default="", help="Villes separees par virgules, ex: Paris,Nanterre")
    parser.add_argument("--max-queries", type=int, default=80, help="Nombre max de requetes sur un run")
    parser.add_argument("--min-score", type=int, default=None, help="Score minimum (0-100)")
    parser.add_argument("--dry-run", action="store_true", help="N'ecrit pas en base")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=0,
        help="Boucle automatique: relance toutes les X minutes (0 = un seul run)",
    )
    return parser.parse_args()



def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]



def run_once(service: IntelligenceService, args: argparse.Namespace) -> dict:
    result = service.run_capture(
        department_codes=_split_csv(args.departments),
        cities=_split_csv(args.cities),
        max_queries=args.max_queries,
        min_score=args.min_score,
        dry_run=args.dry_run,
    )

    payload = {
        "stats": result.stats.__dict__,
        "sample_queries": result.sample_queries,
        "sample_urls": result.sample_urls,
        "top_signals": result.top_signals,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload



def main() -> None:
    args = parse_args()
    service = IntelligenceService()

    if args.interval_minutes <= 0:
        run_once(service, args)
        return

    while True:
        run_once(service, args)
        time.sleep(max(1, args.interval_minutes) * 60)


if __name__ == "__main__":
    main()
