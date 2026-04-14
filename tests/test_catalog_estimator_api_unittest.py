from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path("/Users/keythinkerscelia/PycharmProjects/PythonProject/batiment-fastapi-repo")


def _load_app_module():
    path = ROOT / "app.py"
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location("batiment_fastapi_app", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class CatalogEstimatorApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_app_module()

    def test_catalog_estimate_endpoint_success(self):
        response = self.module.catalog_estimate(
            {
                "lines": [
                    {"code": "renovation_complete", "quantity": 50},
                    {"code": "tableau_electrique"},
                    {"code": "depose_cuisine", "quantity": 1},
                ],
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {
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
                        "quantity": None,
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
            },
        )

    def test_catalog_estimate_endpoint_rejects_unknown_code(self):
        response = self.module.catalog_estimate({"lines": [{"code": "tableau_elec", "quantity": 1}]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "Invalid service code or quantity"})

    def test_catalog_estimate_endpoint_rejects_missing_quantity_for_m2(self):
        response = self.module.catalog_estimate({"lines": [{"code": "carrelage"}]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"error": "Invalid service code or quantity"})


if __name__ == "__main__":
    unittest.main()
