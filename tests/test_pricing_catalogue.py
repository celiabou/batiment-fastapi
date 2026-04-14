from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path("/Users/keythinkerscelia/PycharmProjects/PythonProject")


def _load_module(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pricing_modules_load_catalogue_from_xlsx():
    expected_codes = {
        "amenagement_terrasse",
        "carrelage",
        "depose_cuisine",
        "ouverture_mur_porteur",
        "renovation_complete",
        "sdb_complete",
        "tableau_electrique",
    }

    for relative_path in ("batiment_py/pricing.py", "batiment-fastapi-repo/pricing.py"):
        module = _load_module(relative_path)
        assert module.CATALOG_PATH.name in {
            "Catalogue_Eurobat_Final (1).xlsx",
            "Catalogue_Eurobat_Final.xlsx",
        }
        assert len(module.TARIFF_BY_KEY) == 57
        assert expected_codes.issubset(module.TARIFF_BY_KEY)
        assert sum(len(group["items"]) for group in module.ESTIMATE_WORK_ITEM_GROUPS) == 57


def test_pricing_values_and_aliases_match_catalogue():
    for relative_path in ("batiment_py/pricing.py", "batiment-fastapi-repo/pricing.py"):
        module = _load_module(relative_path)

        renovation = module.estimate_from_item_key("renovation_complete", 50)
        assert renovation == {
            "high": 125000.0,
            "label": "Rénovation complète",
            "low": 60000.0,
            "unit": "m2",
        }

        ouverture = module.estimate_from_item_key("ouverture_mur_porteur", 99)
        assert ouverture == {
            "high": 9000.0,
            "label": "Ouverture mur porteur",
            "low": 2500.0,
            "unit": "forfait",
        }

        tableau = module.estimate_from_item_key("tableau_electrique", None)
        assert tableau == {
            "high": 2000.0,
            "label": "Tableau électrique",
            "low": 800.0,
            "unit": "forfait",
        }

        assert module.estimate_from_item_key("carrelage", None) is None
        assert module.estimate_from_item_key("carrelage", 0) is None
