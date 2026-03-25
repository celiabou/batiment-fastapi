from __future__ import annotations

import unittest
from datetime import UTC, datetime

from intelligence.extractors import (
    classify_announcement,
    extract_budget_range,
    extract_contact,
    extract_work_types,
    enrich_signal,
)
from intelligence.models import RawSignal, SearchScope
from intelligence.service import IntelligenceService


class DummySource:
    name = "dummy"

    def fetch(self, task, limit):
        now = datetime.now(UTC)
        return [
            RawSignal(
                source_name="dummy",
                source_channel=task.channel,
                query=task.query,
                title="Appel d'offres renovation Paris 75011",
                url="https://example.com/offre?id=1&utm_source=test",
                summary="Budget 45000 euros, contact: achats@example.com, chantier urgent",
                published_at=now,
                payload={"origin": "dummy"},
            ),
            RawSignal(
                source_name="dummy",
                source_channel=task.channel,
                query=task.query,
                title="Appel d'offres renovation Paris 75011",
                url="https://example.com/offre?id=1&utm_medium=test",
                summary="Budget 45000 euros, contact: achats@example.com, chantier urgent",
                published_at=now,
                payload={"origin": "dummy"},
            ),
        ]


class IntelligencePipelineTest(unittest.TestCase):
    def test_preview_queries_contains_location(self):
        service = IntelligenceService(sources=[])
        preview = service.preview_queries(department_codes=["93"], cities=["Saint-Denis"], max_queries=12)

        self.assertGreater(len(preview), 0)
        flattened = " ".join(item["query"] for item in preview)
        self.assertIn("Saint-Denis", flattened)

    def test_preview_queries_distributes_interior_lots(self):
        service = IntelligenceService(sources=[])
        preview = service.preview_queries(department_codes=["75"], cities=["Paris"], max_queries=10)
        lots = {item["lot"] for item in preview}

        self.assertIn("peinture", lots)
        self.assertIn("plomberie", lots)
        self.assertIn("electricite", lots)
        self.assertIn("maconnerie", lots)
        self.assertIn("sols", lots)

    def test_extractor_budget_contact_and_type(self):
        scope = SearchScope(department_code="75", department_name="Paris", city="Paris")
        raw = RawSignal(
            source_name="google_news_rss",
            source_channel="platform",
            query="test",
            title="Recherche entreprise pour chantier facade Paris 75015",
            url="https://example.com/projet?utm_source=abc",
            summary="Budget entre 30000 et 55000 euros, mail contact@btp.fr, delai sous 3 semaines.",
            published_at=datetime.now(UTC),
            payload={},
        )
        signal = enrich_signal(raw, scope, score=40)

        self.assertEqual(signal.location_city, "Paris")
        self.assertEqual(signal.location_department_code, "75")
        self.assertEqual(signal.budget_min, 30000)
        self.assertEqual(signal.budget_max, 55000)
        self.assertEqual(signal.contact_email, "contact@btp.fr")
        self.assertEqual(classify_announcement(signal.title + " " + signal.summary), "chantier")

    def test_service_deduplicates_by_canonical_url(self):
        service = IntelligenceService(sources=[DummySource()])
        result = service.run_capture(
            department_codes=["75"],
            cities=["Paris"],
            max_queries=1,
            dry_run=True,
            min_score=0,
        )

        self.assertEqual(result.stats.fetched, 2)
        self.assertEqual(result.stats.deduped, 1)
        self.assertEqual(len(result.top_signals), 1)


class ExtractionHelpersTest(unittest.TestCase):
    def test_budget_range_helper(self):
        low, high = extract_budget_range("Budget 28 000 a 32 000 euros")
        self.assertEqual((low, high), (28000, 32000))

    def test_contact_helper(self):
        email, phone = extract_contact("Contact: +33 6 12 34 56 78, mail chantier@idf.fr")
        self.assertEqual(email, "chantier@idf.fr")
        self.assertTrue(phone.startswith("+33"))

    def test_work_types_interior_focus(self):
        work_types = extract_work_types(
            "Renovation interieure avec peinture, plomberie, electricite, maconnerie et pose de parquet."
        )
        self.assertIn("peinture", work_types)
        self.assertIn("plomberie", work_types)
        self.assertIn("electricite", work_types)
        self.assertIn("maconnerie", work_types)
        self.assertIn("sols", work_types)


if __name__ == "__main__":
    unittest.main()
