from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path("/Users/keythinkerscelia/PycharmProjects/PythonProject")


def _load_module(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CatalogEstimatorTest(unittest.TestCase):
    def test_scope_ranges_follow_catalogue(self):
        expected = {
            "rafraichissement": {"low_m2": 250, "high_m2": 750},
            "renovation_partielle": {"low_m2": 250, "high_m2": 750},
            "renovation_complete": {"low_m2": 1200, "high_m2": 2500},
            "restructuration_lourde": {"low_m2": 2000, "high_m2": 4000},
        }

        for relative_path in ("batiment_py/app.py", "batiment-fastapi-repo/app.py"):
            path = ROOT / relative_path
            import sys

            sys.path.insert(0, str(path.parent))
            try:
                spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
                module = importlib.util.module_from_spec(spec)
                assert spec.loader is not None
                spec.loader.exec_module(module)
            finally:
                sys.path.pop(0)

            for scope_key, scope_expected in expected.items():
                with self.subTest(module=relative_path, scope=scope_key):
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

        for relative_path in ("batiment_py/pricing.py", "batiment-fastapi-repo/pricing.py"):
            module = _load_module(relative_path)
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

        for relative_path in ("batiment_py/pricing.py", "batiment-fastapi-repo/pricing.py"):
            module = _load_module(relative_path)
            for payload in invalid_payloads:
                with self.subTest(module=relative_path, payload=payload):
                    self.assertEqual(
                        module.estimate_catalog_lines(payload),
                        {"error": "Invalid service code or quantity"},
                    )


if __name__ == "__main__":
    unittest.main()
