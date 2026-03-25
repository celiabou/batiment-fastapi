from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Department:
    code: str
    name: str
    cities: tuple[str, ...]


IDF_DEPARTMENTS: tuple[Department, ...] = (
    Department("75", "Paris", ("Paris",)),
    Department(
        "77",
        "Seine-et-Marne",
        (
            "Meaux",
            "Melun",
            "Chelles",
            "Pontault-Combault",
            "Savigny-le-Temple",
            "Combs-la-Ville",
            "Bussy-Saint-Georges",
            "Lagny-sur-Marne",
            "Fontainebleau",
            "Serris",
        ),
    ),
    Department(
        "78",
        "Yvelines",
        (
            "Versailles",
            "Saint-Germain-en-Laye",
            "Poissy",
            "Mantes-la-Jolie",
            "Conflans-Sainte-Honorine",
            "Sartrouville",
            "Rambouillet",
            "Plaisir",
            "Les Mureaux",
            "Trappes",
        ),
    ),
    Department(
        "91",
        "Essonne",
        (
            "Evry-Courcouronnes",
            "Massy",
            "Palaiseau",
            "Corbeil-Essonnes",
            "Savigny-sur-Orge",
            "Viry-Chatillon",
            "Draveil",
            "Yerres",
            "Athis-Mons",
            "Brunoy",
        ),
    ),
    Department(
        "92",
        "Hauts-de-Seine",
        (
            "Nanterre",
            "Boulogne-Billancourt",
            "Courbevoie",
            "Colombes",
            "Asnieres-sur-Seine",
            "Rueil-Malmaison",
            "Neuilly-sur-Seine",
            "Levallois-Perret",
            "Issy-les-Moulineaux",
            "Clamart",
        ),
    ),
    Department(
        "93",
        "Seine-Saint-Denis",
        (
            "Saint-Denis",
            "Aubervilliers",
            "Montreuil",
            "Drancy",
            "Noisy-le-Grand",
            "Aulnay-sous-Bois",
            "Pantin",
            "Bobigny",
            "La Courneuve",
            "Le Blanc-Mesnil",
        ),
    ),
    Department(
        "94",
        "Val-de-Marne",
        (
            "Creteil",
            "Vitry-sur-Seine",
            "Saint-Maur-des-Fosses",
            "Maisons-Alfort",
            "Ivry-sur-Seine",
            "Villejuif",
            "Champigny-sur-Marne",
            "Vincennes",
            "Nogent-sur-Marne",
            "Choisy-le-Roi",
        ),
    ),
    Department(
        "95",
        "Val-d'Oise",
        (
            "Cergy",
            "Argenteuil",
            "Sarcelles",
            "Garges-les-Gonesse",
            "Franconville",
            "Herblay-sur-Seine",
            "Ermont",
            "Pontoise",
            "Eaubonne",
            "Deuil-la-Barre",
        ),
    ),
)


def fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    no_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", no_accents).strip().lower()


FOLDED_DEPARTMENT_BY_CODE = {d.code: d for d in IDF_DEPARTMENTS}
FOLDED_DEPARTMENT_BY_NAME = {fold_text(d.name): d for d in IDF_DEPARTMENTS}
FOLDED_CITY_TO_DEPARTMENT = {
    fold_text(city): department for department in IDF_DEPARTMENTS for city in department.cities
}


def get_department(code_or_name: str | None) -> Department | None:
    if not code_or_name:
        return None

    key = fold_text(code_or_name)
    if key in FOLDED_DEPARTMENT_BY_CODE:
        return FOLDED_DEPARTMENT_BY_CODE[key]
    return FOLDED_DEPARTMENT_BY_NAME.get(key)


def get_department_by_city(city: str | None) -> Department | None:
    if not city:
        return None
    return FOLDED_CITY_TO_DEPARTMENT.get(fold_text(city))


def detect_city(text: str) -> str | None:
    folded = fold_text(text)
    if not folded:
        return None

    for city_folded, department in FOLDED_CITY_TO_DEPARTMENT.items():
        if re.search(rf"\b{re.escape(city_folded)}\b", folded):
            for city in department.cities:
                if fold_text(city) == city_folded:
                    return city
    return None


def detect_department(text: str) -> Department | None:
    folded = fold_text(text)
    if not folded:
        return None

    for department in IDF_DEPARTMENTS:
        if re.search(rf"\b{re.escape(fold_text(department.name))}\b", folded):
            return department
        if re.search(rf"\b{re.escape(department.code)}\b", folded):
            return department
    return None


def detect_postal_code(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"\b(75\d{3}|77\d{3}|78\d{3}|91\d{3}|92\d{3}|93\d{3}|94\d{3}|95\d{3})\b", text)
    if not match:
        return None
    return match.group(1)
