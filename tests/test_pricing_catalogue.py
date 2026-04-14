from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path("/Users/keythinkerscelia/PycharmProjects/PythonProject/batiment_py")


def _load_module(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PricingCatalogueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module("pricing.py")

    def test_pricing_module_loads_catalogue_from_xlsx(self):
        expected_codes = {
            "amenagement_terrasse",
            "carrelage",
            "depose_cuisine",
            "ouverture_mur_porteur",
            "renovation_complete",
            "sdb_complete",
            "tableau_electrique",
        }

        self.assertIn(
            self.module.CATALOG_PATH.name,
            {"Catalogue_Eurobat_Final (1).xlsx", "Catalogue_Eurobat_Final.xlsx"},
        )
        self.assertEqual(len(self.module.TARIFF_BY_KEY), 57)
        self.assertTrue(expected_codes.issubset(self.module.TARIFF_BY_KEY))
        self.assertEqual(sum(len(group["items"]) for group in self.module.ESTIMATE_WORK_ITEM_GROUPS), 57)

    def test_pricing_values_match_catalogue(self):
        renovation = self.module.estimate_from_item_key("renovation_complete", 50)
        self.assertEqual(
            renovation,
            {
                "high": 125000.0,
                "label": "Rénovation complète",
                "low": 60000.0,
                "unit": "m2",
            },
        )

        ouverture = self.module.estimate_from_item_key("ouverture_mur_porteur", 99)
        self.assertEqual(
            ouverture,
            {
                "high": 9000.0,
                "label": "Ouverture mur porteur",
                "low": 2500.0,
                "unit": "forfait",
            },
        )

        tableau = self.module.estimate_from_item_key("tableau_electrique", None)
        self.assertEqual(
            tableau,
            {
                "high": 2000.0,
                "label": "Tableau électrique",
                "low": 800.0,
                "unit": "forfait",
            },
        )

        self.assertIsNone(self.module.estimate_from_item_key("carrelage", None))
        self.assertIsNone(self.module.estimate_from_item_key("carrelage", 0))


if __name__ == "__main__":
    unittest.main()
