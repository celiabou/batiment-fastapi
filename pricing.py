import re
import unicodedata

TARIFF_GRID = [
    {"key": "demolition", "label": "Demolition interieure", "min": 30, "max": 80, "unit": "m2"},
    {"key": "terrassement", "label": "Terrassement", "min": 25, "max": 60, "unit": "m3"},
    {"key": "fondations", "label": "Fondations", "min": 100, "max": 250, "unit": "m2"},
    {"key": "dalle_beton", "label": "Dalle beton", "min": 70, "max": 150, "unit": "m2"},
    {"key": "ouverture_mur_porteur", "label": "Ouverture mur porteur", "min": 1500, "max": 5000, "unit": "forfait"},
    {"key": "ragrage", "label": "Ragreage (preparation sol)", "min": 20, "max": 40, "unit": "m2"},
    {"key": "renovation_complete", "label": "Renovation complete", "min": 800, "max": 1500, "unit": "m2"},
    {"key": "renovation_legere", "label": "Renovation legere", "min": 300, "max": 700, "unit": "m2"},
    {"key": "renovation_haut_de_gamme", "label": "Renovation haut de gamme", "min": 1500, "max": 2500, "unit": "m2"},
    {"key": "electricite_complete", "label": "Installation complete (electricite)", "min": 80, "max": 150, "unit": "m2"},
    {"key": "electricite_normes", "label": "Mise aux normes", "min": 70, "max": 120, "unit": "m2"},
    {"key": "prise_electrique", "label": "Prise electrique", "min": 80, "max": 150, "unit": "unite"},
    {"key": "tableau_elec", "label": "Tableau electrique", "min": 800, "max": 2000, "unit": "forfait"},
    {"key": "plomberie_complete", "label": "Installation complete (plomberie)", "min": 90, "max": 180, "unit": "m2"},
    {"key": "salle_de_bain_complete", "label": "Salle de bain complete", "min": 4000, "max": 12000, "unit": "forfait"},
    {"key": "cuisine_raccordement", "label": "Cuisine (raccordement)", "min": 500, "max": 2500, "unit": "forfait"},
    {"key": "peinture_murs", "label": "Peinture murs", "min": 20, "max": 45, "unit": "m2"},
    {"key": "peinture_plafond", "label": "Peinture plafond", "min": 25, "max": 50, "unit": "m2"},
    {"key": "enduit_preparation", "label": "Enduit / preparation murs", "min": 15, "max": 35, "unit": "m2"},
    {"key": "carrelage", "label": "Carrelage", "min": 40, "max": 120, "unit": "m2"},
    {"key": "parquet", "label": "Parquet", "min": 30, "max": 80, "unit": "m2"},
    {"key": "sol_souple", "label": "Sol souple (PVC / lino)", "min": 25, "max": 60, "unit": "m2"},
    {"key": "moquette", "label": "Moquette", "min": 20, "max": 50, "unit": "m2"},
    {"key": "renovation_toiture", "label": "Renovation toiture", "min": 180, "max": 350, "unit": "m2"},
    {"key": "isolation_toiture", "label": "Isolation toiture", "min": 50, "max": 120, "unit": "m2"},
    {"key": "renovation_balcon", "label": "Renovation balcon", "min": 200, "max": 600, "unit": "m2"},
    {"key": "renovation_terrasse", "label": "Renovation terrasse", "min": 300, "max": 800, "unit": "m2"},
    {"key": "amenagement_terrasse", "label": "Amenagement terrasse", "min": 1000, "max": 10000, "unit": "forfait"},
    {"key": "optimisation_exterieur", "label": "Optimisation espace exterieur", "min": 500, "max": 5000, "unit": "forfait"},
]

TARIFF_KEYWORDS = [
    ("salle_de_bain_complete", ["salle de bain", "sdb", "douche", "baignoire"]),
    ("cuisine_raccordement", ["cuisine", "raccordement cuisine"]),
    ("renovation_balcon", ["balcon"]),
    ("renovation_toiture", ["toiture", "toit"]),
    ("isolation_toiture", ["isolation toiture"]),
    ("electricite_complete", ["electricite complete", "electricite", "electrique"]),
    ("electricite_normes", ["mise aux normes", "normes elec", "normes electriques"]),
    ("tableau_elec", ["tableau electrique", "tableau elec"]),
    ("prise_electrique", ["prise", "prises"]),
    ("plomberie_complete", ["plomberie", "plomberie complete"]),
    ("peinture_plafond", ["peinture plafond", "plafond"]),
    ("peinture_murs", ["peinture murs", "peinture mur", "peinture"]),
    ("enduit_preparation", ["enduit", "preparation mur", "preparation sol"]),
    ("carrelage", ["carrelage"]),
    ("parquet", ["parquet"]),
    ("sol_souple", ["sol souple", "vinyle", "lino", "linoleum"]),
    ("moquette", ["moquette"]),
    ("ragrage", ["ragreage"]),
    ("demolition", ["demolition"]),
    ("terrassement", ["terrassement"]),
    ("fondations", ["fondation", "fondations"]),
    ("dalle_beton", ["dalle beton", "dalle beton"]),
    ("ouverture_mur_porteur", ["ouverture mur porteur", "ouverture", "creation ouverture"]),
    ("amenagement_terrasse", ["amenagement terrasse"]),
    ("optimisation_exterieur", ["optimisation exterieur"]),
    ("renovation_haut_de_gamme", ["haut de gamme", "luxe", "premium", "sur mesure"]),
    ("renovation_complete", ["renovation complete", "renovation totale", "renovation complete", "renovation integrale"]),
    ("renovation_legere", ["renovation legere", "rafraichissement", "rafraichissement"]),
]

TARIFF_BY_KEY = {item["key"]: item for item in TARIFF_GRID}


def _normalize(text: str) -> str:
    raw = (text or "").lower().strip()
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKD", raw)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _extract_surface_m2(text: str) -> float | None:
    match = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(m2|m²)\b", text)
    if not match:
        return None
    value = match.group(1).replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def _extract_volume_m3(text: str) -> float | None:
    match = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(m3|m³)\b", text)
    if not match:
        return None
    value = match.group(1).replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def _extract_quantity(text: str) -> int | None:
    numbers = re.findall(r"\b(\d{1,3})\b", text)
    if not numbers:
        return None
    for raw in numbers:
        if re.search(rf"{re.escape(raw)}\s*(m2|m²)\b", text):
            continue
        return int(raw)
    return None


def _match_tariff_item(text: str) -> dict | None:
    normalized = _normalize(text)
    for key, keywords in TARIFF_KEYWORDS:
        for kw in keywords:
            if _normalize(kw) in normalized:
                return TARIFF_BY_KEY.get(key)
    return None


def get_tariff_item(text: str) -> dict | None:
    if not text:
        return None
    return _match_tariff_item(text)


def has_required_quantity(text: str, item: dict | None) -> bool:
    if not text or not item:
        return False
    unit = item.get("unit")
    if unit == "forfait":
        return True
    if unit == "m2":
        return _extract_surface_m2(text) is not None
    if unit == "m3":
        return _extract_volume_m3(text) is not None
    if unit == "unite":
        return _extract_quantity(text) is not None
    if unit == "ml":
        return _extract_quantity(text) is not None
    return False


def estimate_from_item_key(item_key: str, quantity: float | int | None, unit_override: str | None = None) -> dict | None:
    item = TARIFF_BY_KEY.get((item_key or "").strip())
    if not item:
        return None

    unit = unit_override or item["unit"]
    if unit == "forfait":
        return {"low": item["min"], "high": item["max"], "unit": unit, "label": item["label"]}

    if quantity is None:
        return None

    qty = float(quantity)
    if qty <= 0:
        return None

    if unit in {"m2", "m3"}:
        low = item["min"] * qty
        high = item["max"] * qty
        return {"low": low, "high": high, "unit": unit, "label": item["label"]}

    if unit in {"unite", "ml"}:
        qty_int = int(round(qty))
        if qty_int <= 0:
            return None
        low = item["min"] * qty_int
        high = item["max"] * qty_int
        return {"low": low, "high": high, "unit": unit, "label": item["label"]}

    return None


def estimate_from_text(text: str, default_surface: float | None = None) -> dict:
    if not text:
        return {"confidence": 0.2}

    item = _match_tariff_item(text)
    if not item:
        return {"confidence": 0.3}

    unit = item["unit"]
    if unit == "forfait":
        return {"confidence": 0.78, "low": item["min"], "high": item["max"]}

    if unit == "unite":
        qty = _extract_quantity(text) or 1
        return {
            "confidence": 0.62,
            "low": int(round(item["min"] * qty)),
            "high": int(round(item["max"] * qty)),
        }

    if unit == "m2":
        surface = _extract_surface_m2(text) or default_surface
        if not surface:
            return {"confidence": 0.35}
        return {
            "confidence": 0.7,
            "low": int(round(item["min"] * surface)),
            "high": int(round(item["max"] * surface)),
        }

    if unit == "m3":
        volume = _extract_volume_m3(text)
        if not volume:
            return {"confidence": 0.35}
        return {
            "confidence": 0.7,
            "low": int(round(item["min"] * volume)),
            "high": int(round(item["max"] * volume)),
        }

    if unit == "ml":
        qty = _extract_quantity(text) or 1
        return {
            "confidence": 0.55,
            "low": int(round(item["min"] * qty)),
            "high": int(round(item["max"] * qty)),
        }

    return {"confidence": 0.3}


def estimate_from_scope(
    scope_key: str,
    project_type: str,
    surface_m2: float | None,
    finishing_level: str | None = None,
) -> dict | None:
    if not surface_m2:
        return None

    if scope_key == "renovation_complete":
        item = TARIFF_BY_KEY.get("renovation_complete")
    elif scope_key == "renovation_partielle":
        item = TARIFF_BY_KEY.get("renovation_legere")
    elif scope_key == "rafraichissement":
        item = TARIFF_BY_KEY.get("renovation_legere")
    elif scope_key == "restructuration_lourde":
        item = TARIFF_BY_KEY.get("renovation_haut_de_gamme")
    else:
        item = None

    if not item:
        return None

    low = surface_m2 * item["min"]
    high = surface_m2 * item["max"]

    if finishing_level == "premium":
        low *= 1.05
        high *= 1.08
    elif finishing_level == "haut_de_gamme":
        low *= 1.12
        high *= 1.18
    elif finishing_level == "sur_mesure":
        low *= 1.18
        high *= 1.28

    return {"low": low, "high": high, "label": item["label"], "unit": item["unit"]}
