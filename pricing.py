from __future__ import annotations

import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

CATALOG_ENV_VAR = "EUROBAT_CATALOG_PATH"
CATALOG_CANDIDATES = [
    Path(os.environ[CATALOG_ENV_VAR]).expanduser()
    for _ in [0]
    if os.environ.get(CATALOG_ENV_VAR)
] + [
    Path.home() / "Downloads" / "Catalogue_Eurobat_Final (1).xlsx",
    Path.home() / "Downloads" / "Catalogue_Eurobat_Final.xlsx",
    Path(__file__).resolve().parent / "Catalogue_Eurobat_Final.xlsx",
]

COMPAT_CODE_ALIASES = {
    "cuisine_raccordement": "raccordement_cuisine",
    "demolition": "demolition_interieure",
    "electricite_normes": "mise_aux_normes",
    "enduit_preparation": "enduit_lissage",
    "renovation_haut_de_gamme": "renovation_lourde",
    "salle_de_bain_complete": "sdb_complete",
    "tableau_elec": "tableau_electrique",
}

SCOPE_TO_CODE = {
    "rafraichissement": "renovation_legere",
    "renovation_complete": "renovation_complete",
    "renovation_partielle": "renovation_legere",
    "restructuration_lourde": "renovation_lourde",
}

MANUAL_KEYWORDS = {
    "demolition_interieure": ["demolition", "demolition interieure"],
    "mise_aux_normes": ["mise aux normes", "normes elec", "normes electriques"],
    "point_lumineux": ["point lumineux", "luminaire"],
    "prise_electrique": ["prise", "prises", "prise electrique"],
    "raccordement_cuisine": ["raccordement cuisine"],
    "renovation_lourde": ["haut de gamme", "luxe", "premium", "sur mesure", "renovation lourde"],
    "sdb_complete": ["salle de bain", "sdb", "salle de bain complete"],
    "tableau_electrique": ["tableau electrique", "tableau elec"],
}


def _normalize(text: str) -> str:
    raw = (text or "").lower().strip()
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKD", raw)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _normalize_unit(value: str | None) -> str:
    unit = _normalize(value or "").replace(" ", "")
    return {
        "m²": "m2",
        "m2": "m2",
        "m3": "m3",
        "forfait": "forfait",
        "unite": "unite",
        "unit": "unite",
        "u": "unite",
        "ml": "ml",
    }.get(unit, unit)


def _parse_number(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(" ", "")
    if not raw:
        return None
    raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _candidate_catalog_paths() -> list[Path]:
    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in CATALOG_CANDIDATES:
        resolved = candidate.expanduser()
        if resolved not in seen:
            unique_paths.append(resolved)
            seen.add(resolved)
    return unique_paths


def _resolve_catalog_path() -> Path:
    for candidate in _candidate_catalog_paths():
        if candidate.exists():
            return candidate
    looked_up = ", ".join(str(path) for path in _candidate_catalog_paths())
    raise FileNotFoundError(f"Eurobat catalogue not found. Looked in: {looked_up}")


def _load_shared_strings(archive: ZipFile) -> list[str]:
    shared_path = "xl/sharedStrings.xml"
    if shared_path not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(shared_path))
    values: list[str] = []
    for item in root.findall("main:si", XML_NS):
        texts = [node.text or "" for node in item.findall(".//main:t", XML_NS)]
        values.append("".join(texts))
    return values


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return max(index - 1, 0)


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", XML_NS))
    raw_value = cell.findtext("main:v", default="", namespaces=XML_NS)
    if cell_type == "s":
        if not raw_value:
            return ""
        return shared_strings[int(raw_value)]
    return raw_value or ""


def _first_sheet_path(archive: ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find("main:sheets/main:sheet", XML_NS)
    if sheet is None:
        raise ValueError("Workbook does not contain any sheet")
    rel_id = sheet.attrib.get(f"{{{XML_NS['rel']}}}id")
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("pkgrel:Relationship", XML_NS):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError("Cannot resolve first worksheet path")


def _read_catalog_rows(catalog_path: Path) -> list[dict[str, object]]:
    with ZipFile(catalog_path) as archive:
        shared_strings = _load_shared_strings(archive)
        sheet_root = ET.fromstring(archive.read(_first_sheet_path(archive)))

    rows: list[list[str]] = []
    for row in sheet_root.findall("main:sheetData/main:row", XML_NS):
        cells: dict[int, str] = {}
        for cell in row.findall("main:c", XML_NS):
            ref = cell.attrib.get("r", "")
            cells[_column_index(ref)] = _cell_text(cell, shared_strings)
        if not cells:
            continue
        max_index = max(cells)
        rows.append([cells.get(index, "") for index in range(max_index + 1)])

    if not rows:
        return []

    header = [str(value).strip() for value in rows[0]]
    parsed_rows: list[dict[str, object]] = []
    for raw_row in rows[1:]:
        if not any((value or "").strip() for value in raw_row):
            continue
        row = {
            header[index]: raw_row[index].strip() if index < len(raw_row) else ""
            for index in range(len(header))
            if header[index]
        }
        code = (row.get("code") or "").strip()
        unit = _normalize_unit(row.get("unite"))
        min_price = _parse_number(row.get("prix_min_ht"))
        max_price = _parse_number(row.get("prix_max_ht"))
        if not code or not unit or min_price is None or max_price is None:
            continue
        parsed_rows.append(
            {
                "key": code,
                "label": (row.get("prestation") or code).strip(),
                "lot": (row.get("lot") or "").strip(),
                "mode_calcul": _normalize_unit(row.get("mode_calcul")),
                "min": min_price,
                "max": max_price,
                "unit": unit,
            }
        )
    return parsed_rows


CATALOG_PATH = _resolve_catalog_path()
TARIFF_GRID = _read_catalog_rows(CATALOG_PATH)
TARIFF_BY_KEY = {item["key"]: item for item in TARIFF_GRID}


def _humanize_lot_label(value: str) -> str:
    words = str(value or "").replace("_", " ").split()
    if not words:
        return "Autres"
    return " ".join(word.capitalize() for word in words)


def _build_template_groups(items: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    groups_by_key: dict[str, dict[str, object]] = {}

    for item in items:
        lot_key = str(item.get("lot") or "autres")
        group = groups_by_key.get(lot_key)
        if group is None:
            group = {"key": lot_key, "label": _humanize_lot_label(lot_key), "items": []}
            groups_by_key[lot_key] = group
            groups.append(group)
        group["items"].append(
            {
                "key": str(item["key"]),
                "label": str(item["label"]),
                "unit": str(item["unit"]),
            }
        )

    return groups


ESTIMATE_WORK_ITEM_GROUPS = _build_template_groups(list(TARIFF_BY_KEY.values()))


def _keyword_candidates(item: dict[str, object]) -> list[str]:
    code = str(item["key"])
    label = str(item["label"])
    lot = str(item.get("lot", ""))
    values = {
        _normalize(code),
        _normalize(code.replace("_", " ")),
        _normalize(label),
        _normalize(lot),
    }
    values.update(_normalize(keyword) for keyword in MANUAL_KEYWORDS.get(code, []))
    alias = next((legacy for legacy, current in COMPAT_CODE_ALIASES.items() if current == code), None)
    if alias:
        values.add(_normalize(alias))
        values.add(_normalize(alias.replace("_", " ")))
    return sorted((value for value in values if value), key=len, reverse=True)


SEARCH_INDEX = {
    code: _keyword_candidates(item)
    for code, item in TARIFF_BY_KEY.items()
}

CATALOG_ESTIMATE_ERROR = {"error": "Invalid service code or quantity"}


def _resolve_item_key(item_key: str | None) -> str:
    normalized_key = (item_key or "").strip()
    return COMPAT_CODE_ALIASES.get(normalized_key, normalized_key)


def _extract_surface_m2(text: str) -> float | None:
    match = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(m2|m²)\b", text)
    if not match:
        return None
    return _parse_number(match.group(1))


def _extract_volume_m3(text: str) -> float | None:
    match = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(m3|m³)\b", text)
    if not match:
        return None
    return _parse_number(match.group(1))


def _extract_quantity(text: str) -> int | None:
    numbers = re.findall(r"\b(\d{1,5})\b", text)
    if not numbers:
        return None
    normalized = _normalize(text)
    for raw in numbers:
        if re.search(rf"\b{re.escape(raw)}\s*m[23]\b", normalized):
            continue
        return int(raw)
    return None


def _match_tariff_item(text: str) -> dict[str, object] | None:
    normalized = _normalize(text)
    best_code = None
    best_score = -1
    for code, keywords in SEARCH_INDEX.items():
        for keyword in keywords:
            if keyword and keyword in normalized and len(keyword) > best_score:
                best_code = code
                best_score = len(keyword)
    if not best_code:
        return None
    return TARIFF_BY_KEY.get(best_code)


def get_tariff_item(text: str) -> dict[str, object] | None:
    if not text:
        return None
    return _match_tariff_item(text)


def has_required_quantity(text: str, item: dict[str, object] | None) -> bool:
    if not text or not item:
        return False
    unit = str(item.get("unit", ""))
    if unit == "forfait":
        return True
    if unit == "m2":
        return _extract_surface_m2(text) is not None
    if unit == "m3":
        return _extract_volume_m3(text) is not None
    if unit in {"unite", "ml"}:
        return _extract_quantity(text) is not None
    return False


def _json_number(value: float | int | None) -> int | float | None:
    if value is None:
        return None
    numeric = float(value)
    if numeric.is_integer():
        return int(numeric)
    return round(numeric, 2)


def estimate_catalog_lines(lines: list[dict[str, object]] | None) -> dict[str, object]:
    if not isinstance(lines, list) or not lines:
        return dict(CATALOG_ESTIMATE_ERROR)

    response_lines: list[dict[str, object]] = []
    total_min = 0.0
    total_max = 0.0

    for raw_line in lines:
        if not isinstance(raw_line, dict):
            return dict(CATALOG_ESTIMATE_ERROR)

        code = str(raw_line.get("code") or "").strip()
        if not code:
            return dict(CATALOG_ESTIMATE_ERROR)

        item = TARIFF_BY_KEY.get(code)
        if not item:
            return dict(CATALOG_ESTIMATE_ERROR)

        unit = str(item["unit"])
        quantity = raw_line.get("quantity")

        if unit == "forfait":
            line_total_min = float(item["min"])
            line_total_max = float(item["max"])
        else:
            qty = _parse_number(quantity)
            if qty is None or qty <= 0:
                return dict(CATALOG_ESTIMATE_ERROR)
            line_total_min = float(item["min"]) * qty
            line_total_max = float(item["max"]) * qty

        total_min += line_total_min
        total_max += line_total_max
        response_lines.append(
            {
                "code": code,
                "quantity": _json_number(quantity) if quantity is not None else None,
                "unit": unit,
                "unit_price_min": _json_number(item["min"]),
                "unit_price_max": _json_number(item["max"]),
                "line_total_min": _json_number(line_total_min),
                "line_total_max": _json_number(line_total_max),
            }
        )

    return {
        "lines": response_lines,
        "total_min_ht": _json_number(total_min),
        "total_max_ht": _json_number(total_max),
    }


def estimate_from_item_key(
    item_key: str,
    quantity: float | int | None,
    unit_override: str | None = None,
) -> dict[str, object] | None:
    resolved_key = _resolve_item_key(item_key)
    item = TARIFF_BY_KEY.get(resolved_key)
    if not item:
        return None

    unit = str(item["unit"])
    if unit_override and _normalize_unit(unit_override) not in {"", unit}:
        return None
    if unit == "forfait":
        return {"low": item["min"], "high": item["max"], "unit": unit, "label": item["label"]}
    if quantity is None:
        return None

    qty = _parse_number(quantity)
    if qty is None or qty <= 0:
        return None

    if unit in {"m2", "m3"}:
        return {"low": item["min"] * qty, "high": item["max"] * qty, "unit": unit, "label": item["label"]}

    if unit in {"unite", "ml"}:
        qty_int = int(round(qty))
        if qty_int <= 0:
            return None
        return {"low": item["min"] * qty_int, "high": item["max"] * qty_int, "unit": unit, "label": item["label"]}

    return None


def estimate_from_text(text: str, default_surface: float | None = None) -> dict[str, object]:
    if not text:
        return {"confidence": 0.2}

    item = _match_tariff_item(text)
    if not item:
        return {"confidence": 0.3}

    unit = str(item["unit"])
    if unit == "forfait":
        return {"confidence": 0.78, "low": item["min"], "high": item["max"]}

    if unit == "unite":
        qty = _extract_quantity(text)
        if qty is None:
            return {"confidence": 0.35}
        return {"confidence": 0.62, "low": int(round(item["min"] * qty)), "high": int(round(item["max"] * qty))}

    if unit == "m2":
        surface = _extract_surface_m2(text) or default_surface
        if not surface:
            return {"confidence": 0.35}
        return {"confidence": 0.7, "low": int(round(item["min"] * surface)), "high": int(round(item["max"] * surface))}

    if unit == "m3":
        volume = _extract_volume_m3(text)
        if not volume:
            return {"confidence": 0.35}
        return {"confidence": 0.7, "low": int(round(item["min"] * volume)), "high": int(round(item["max"] * volume))}

    if unit == "ml":
        qty = _extract_quantity(text)
        if qty is None:
            return {"confidence": 0.35}
        return {"confidence": 0.55, "low": int(round(item["min"] * qty)), "high": int(round(item["max"] * qty))}

    return {"confidence": 0.3}


def estimate_from_scope(
    scope_key: str,
    project_type: str,
    surface_m2: float | None,
    finishing_level: str | None = None,
) -> dict[str, object] | None:
    if not surface_m2 or surface_m2 <= 0:
        return None

    catalog_code = SCOPE_TO_CODE.get(scope_key)
    item = TARIFF_BY_KEY.get(catalog_code or "")
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
