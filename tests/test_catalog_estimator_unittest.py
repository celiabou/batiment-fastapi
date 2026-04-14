from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path("/Users/keythinkerscelia/PycharmProjects/PythonProject/batiment-fastapi-repo")


def _load_module(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_app_module():
    path = ROOT / "app.py"
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location("app_module", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class CatalogEstimatorTest(unittest.TestCase):
    def test_scope_ranges_follow_catalogue(self):
        expected = {
            "rafraichissement": {"low_m2": 250, "high_m2": 750},
            "renovation_partielle": {"low_m2": 250, "high_m2": 750},
            "renovation_complete": {"low_m2": 1200, "high_m2": 2500},
            "restructuration_lourde": {"low_m2": 2000, "high_m2": 4000},
        }

        app_path = ROOT / "app.py"
        sys.path.insert(0, str(ROOT))
        try:
            spec = importlib.util.spec_from_file_location("app_module", app_path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)

        for scope_key, scope_expected in expected.items():
            with self.subTest(scope=scope_key):
                self.assertEqual(module.SMART_SCOPE_CONFIG[scope_key]["low_m2"], scope_expected["low_m2"])
                self.assertEqual(module.SMART_SCOPE_CONFIG[scope_key]["high_m2"], scope_expected["high_m2"])

    def test_catalog_estimate_success(self):
        payload = [
            {"code": "renovation_complete", "quantity": 50},
            {"code": "tableau_electrique", "quantity": 999},
            {"code": "depose_cuisine", "quantity": 1},
        ]
        expected = {
            "lines": [
                {
                    "code": "renovation_complete",
                    "quantity": 50,
                    "unit": "m2",
                    "unit_price_min": 1200,
                    "unit_price_max": 2500,
                    "line_total_min": 60000,
                    "line_total_max": 125000,
                },
                {
                    "code": "tableau_electrique",
                    "quantity": 999,
                    "unit": "forfait",
                    "unit_price_min": 800,
                    "unit_price_max": 2000,
                    "line_total_min": 800,
                    "line_total_max": 2000,
                },
                {
                    "code": "depose_cuisine",
                    "quantity": 1,
                    "unit": "forfait",
                    "unit_price_min": 350,
                    "unit_price_max": 1200,
                    "line_total_min": 350,
                    "line_total_max": 1200,
                },
            ],
            "total_min_ht": 61150,
            "total_max_ht": 128200,
        }

        module = _load_module("pricing.py")
        self.assertEqual(module.estimate_catalog_lines(payload), expected)

    def test_catalog_estimate_invalid_code_or_quantity(self):
        invalid_payloads = [
            None,
            [],
            [{"code": "inconnu", "quantity": 1}],
            [{"code": "carrelage"}],
            [{"code": "carrelage", "quantity": 0}],
            [{"code": "carrelage", "quantity": -1}],
            [{"code": "carrelage", "quantity": "abc"}],
            [{"quantity": 1}],
            ["bad-line"],
        ]

        module = _load_module("pricing.py")
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.assertEqual(
                    module.estimate_catalog_lines(payload),
                    {"error": "Invalid service code or quantity"},
                )

    def test_smart_quote_ignores_non_catalogue_coefficients(self):
        module = _load_app_module()
        quote = module._build_smart_quote(
            project_type="bien_professionnel",
            style="contemporain",
            scope="rafraichissement",
            timeline="urgent",
            surface="50",
            rooms="8",
            budget="100000",
            city="Paris",
            notes="Mur porteur plomberie sur mesure",
            work_item_key="",
            work_quantity="",
            work_unit="",
        )

        self.assertEqual(quote["pricing_basis"], "catalog")
        self.assertEqual(quote["low"], 12500)
        self.assertEqual(quote["high"], 37500)
        self.assertEqual(quote["pricing_context"], "Catalogue Eurobat • Rénovation légère • 50 m2")
        self.assertIn("Aucun coefficient type de bien, style, delai, nombre de pieces ou complexite n'est applique.", quote["assumptions"])

    def test_smart_quote_uses_selected_catalogue_item_only(self):
        module = _load_app_module()
        quote = module._build_smart_quote(
            project_type="bien_professionnel",
            style="contemporain",
            scope="renovation_complete",
            timeline="urgent",
            surface="250",
            rooms="12",
            budget="5000",
            city="Paris",
            notes="Tout le bureau",
            work_item_key="carrelage",
            work_quantity="10",
            work_unit="m2",
        )

        self.assertEqual(quote["low"], 600)
        self.assertEqual(quote["high"], 1900)
        self.assertEqual(quote["breakdown"][0]["code"], "carrelage")

    def test_smart_quote_requires_surface_for_scope_estimate(self):
        module = _load_app_module()
        quote = module._build_smart_quote(
            project_type="maison",
            style="moderne",
            scope="renovation_complete",
            timeline="6_mois",
            surface="",
            rooms="",
            budget="",
            city="",
            notes="",
            work_item_key="",
            work_quantity="",
            work_unit="",
        )

        self.assertEqual(quote, {"error": "Renseignez la surface en m2 pour calculer cette estimation catalogue."})

    def test_quote_pdf_attachment_is_generated(self):
        module = _load_app_module()
        quote = module._build_smart_quote(
            project_type="maison",
            style="moderne",
            scope="renovation_complete",
            timeline="6_mois",
            surface="50",
            rooms="",
            budget="",
            city="Paris",
            notes="",
            work_item_key="",
            work_quantity="",
            work_unit="",
        )

        attachment = module._build_quote_pdf_attachment(
            name="Laura Chris",
            city="Paris",
            project_type="maison",
            scope="renovation_complete",
            style="moderne",
            quote=quote,
        )

        self.assertEqual(attachment["mime_type"], "application/pdf")
        self.assertEqual(attachment["filename"], "pre_devis_laura_chris.pdf")
        self.assertTrue(bytes(attachment["content"]).startswith(b"%PDF-1.4"))
        self.assertGreater(len(bytes(attachment["content"])), 500)


if __name__ == "__main__":
    unittest.main()
