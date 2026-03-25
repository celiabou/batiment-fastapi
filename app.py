# noinspection SpellCheckingInspection
import base64
import binascii
import hashlib
import json
import os
import re
import smtplib
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import SessionLocal, engine
from db import init_db, insert_lead
from intelligence.router import router as intelligence_router
from models import Base, HandoffRequest, Lead
from pricing import estimate_from_text
from saas_ai.router import router as saas_ai_router

def _load_local_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    try:
        raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


BASE_DIR = Path(__file__).resolve().parent
_load_local_env_file(BASE_DIR / ".env")
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
CONTENT_DIR = BASE_DIR / "content"
ARCHITECTURE_UPLOAD_DIR = STATIC_DIR / "architecture" / "uploads"
ARCHITECTURE_RENDER_DIR = STATIC_DIR / "architecture" / "renders"
AGENDA_URL = os.getenv("AGENDA_URL", "").strip()
INTERNAL_REPORT_EMAIL = os.getenv("INTERNAL_REPORT_EMAIL", "celia.b@keythinkers.fr").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
VISITOR_COOKIE_NAME = "rb_vid"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 180
TRACKING_PARAM_KEYS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_file_stamp() -> str:
    return _utc_now().strftime("%Y%m%d%H%M%S")

ARCHITECTURE_DEMO_RENDERS = {
    "moderne": [
        "https://images.pexels.com/photos/323780/pexels-photo-323780.jpeg?auto=compress&cs=tinysrgb&w=1400",
        "https://images.pexels.com/photos/1396122/pexels-photo-1396122.jpeg?auto=compress&cs=tinysrgb&w=1400",
    ],
    "contemporain": [
        "https://images.pexels.com/photos/106399/pexels-photo-106399.jpeg?auto=compress&cs=tinysrgb&w=1400",
        "https://images.pexels.com/photos/280229/pexels-photo-280229.jpeg?auto=compress&cs=tinysrgb&w=1400",
    ],
    "haussmannien": [
        "https://images.pexels.com/photos/101808/pexels-photo-101808.jpeg?auto=compress&cs=tinysrgb&w=1400",
        "https://images.pexels.com/photos/1612351/pexels-photo-1612351.jpeg?auto=compress&cs=tinysrgb&w=1400",
    ],
    "minimaliste": [
        "https://images.pexels.com/photos/1571468/pexels-photo-1571468.jpeg?auto=compress&cs=tinysrgb&w=1400",
        "https://images.pexels.com/photos/1643384/pexels-photo-1643384.jpeg?auto=compress&cs=tinysrgb&w=1400",
    ],
}

SMART_SCOPE_CONFIG = {
    "rafraichissement": {"low_m2": 320, "high_m2": 620, "duration": (2, 6)},
    "renovation_partielle": {"low_m2": 700, "high_m2": 1250, "duration": (4, 10)},
    "renovation_complete": {"low_m2": 1200, "high_m2": 2100, "duration": (8, 18)},
    "restructuration_lourde": {"low_m2": 1700, "high_m2": 3000, "duration": (12, 28)},
}

SMART_SCOPE_LABELS = {
    "rafraichissement": "Rafraichissement",
    "renovation_partielle": "Renovation partielle",
    "renovation_complete": "Renovation complete",
    "restructuration_lourde": "Restructuration lourde",
}

PROJECT_TYPE_LABELS = {
    "facade": "Facade",
    "maison": "Maison",
    "appartement": "Appartement",
    "immeuble": "Immeuble",
}

PROJECT_TYPE_MULTIPLIER = {
    "facade": 1.02,
    "maison": 1.0,
    "appartement": 0.94,
    "immeuble": 1.16,
}

PROJECT_DEFAULT_SURFACE = {
    "facade": 90.0,
    "maison": 120.0,
    "appartement": 65.0,
    "immeuble": 340.0,
}

STYLE_MULTIPLIER = {
    "moderne": 1.0,
    "contemporain": 1.06,
    "haussmannien": 1.12,
    "minimaliste": 0.98,
    "industriel": 1.04,
    "scandinave": 1.01,
}

TIMELINE_COST_MULTIPLIER = {
    "urgent": 1.12,
    "3_mois": 1.05,
    "6_mois": 1.0,
    "flexible": 0.96,
}

TIMELINE_DURATION_MULTIPLIER = {
    "urgent": 0.9,
    "3_mois": 0.95,
    "6_mois": 1.0,
    "flexible": 1.08,
}

INTERIOR_STYLE_PROFILES = {
    "moderne": {
        "palette": ["Blanc casse", "Greige", "Noir graphite", "Vert sauge"],
        "materials": ["Bois clair mat", "Quartz blanc", "Metal noir satine"],
        "furniture_focus": ["Lignes droites", "Rangements invisibles", "Eclairage LED indirect"],
    },
    "contemporain": {
        "palette": ["Ivoire", "Taupe", "Bleu profond", "Laiton brosse"],
        "materials": ["Noyer naturel", "Pierre effet travertin", "Textiles boucles"],
        "furniture_focus": ["Canape module", "Table organique", "Suspensions statement"],
    },
    "haussmannien": {
        "palette": ["Blanc perle", "Vert olive", "Bordeaux", "Dorure douce"],
        "materials": ["Moulures staff", "Parquet point de Hongrie", "Marbre clair"],
        "furniture_focus": ["Boiseries conservees", "Pieces vintage", "Eclairage chaleureux"],
    },
    "minimaliste": {
        "palette": ["Blanc pur", "Sable", "Gris pierre", "Noir doux"],
        "materials": ["Micro-ciment", "Bois blond", "Verre extra-clair"],
        "furniture_focus": ["Volume epure", "Mobilier bas", "Ambiance zen"],
    },
    "industriel": {
        "palette": ["Gris acier", "Brique", "Noir carbone", "Camel"],
        "materials": ["Acier thermolaque", "Bois recycle", "Beton cire"],
        "furniture_focus": ["Elements bruts", "Cloison verriere", "Luminaires atelier"],
    },
    "scandinave": {
        "palette": ["Blanc neige", "Beige lin", "Bleu pale", "Bois miel"],
        "materials": ["Chene naturel", "Laine bouclee", "Ceramique mate"],
        "furniture_focus": ["Formes rondes", "Lumieres diffuses", "Textiles cocooning"],
    },
}

INTERIOR_ZONE_TEMPLATES = {
    "appartement": [
        ("Entree", 0.1, "Rangement colonne + assise compacte"),
        ("Piece de vie", 0.37, "Salon ouvert avec circulation fluide"),
        ("Cuisine", 0.2, "Plan en L + ilot si possible"),
        ("Chambres", 0.23, "Rangements toute hauteur"),
        ("Salle d'eau", 0.1, "Douche confort + niche integree"),
    ],
    "maison": [
        ("Entree / degagement", 0.08, "Placards caches et banc"),
        ("Sejour", 0.35, "Zone canape + bibliotheque sur mesure"),
        ("Cuisine / repas", 0.22, "Cuisine conviviale et eclairage fonctionnel"),
        ("Espace nuit", 0.25, "Tetes de lit et dressing optimise"),
        ("Salles d'eau", 0.1, "Materiaux durables anti-humidite"),
    ],
    "immeuble": [
        ("Hall d'accueil", 0.12, "Signaletique + eclairage securisant"),
        ("Circulations", 0.18, "Sols robustes et maintenance facile"),
        ("Parties privatives", 0.46, "Optimisation des surfaces utiles"),
        ("Locaux techniques", 0.12, "Acces entretien simplifie"),
        ("Espaces communs", 0.12, "Confort acoustique et convivialite"),
    ],
    "facade": [
        ("Envelope exterieure", 0.45, "Traitement materiaux + isolation"),
        ("Entree", 0.2, "Valorisation du seuil et eclairage"),
        ("Parties visibles", 0.35, "Harmonie colorimetrique et enseigne"),
    ],
}

COMPLEXITY_KEYWORDS = (
    "mur porteur",
    "structure",
    "copropriete",
    "urgence",
    "humide",
    "amiante",
    "sur mesure",
    "domotique",
    "ascenseur",
    "isolation",
)

HANDOFF_KEYWORDS = (
    "humain",
    "conseiller",
    "rappel",
    "appelez",
    "appeler",
    "urgent",
    "maintenant",
    "rdv",
    "rendez-vous",
)

ACK_KEYWORDS = (
    "merci",
    "super",
    "parfait",
    "top",
    "ok",
    "c est bon",
    "cest bon",
    "nickel",
    "genial",
)

SCHEDULE_INTENT_KEYWORDS = (
    "valide mon creneau",
    "valider mon creneau",
    "valide le creneau",
    "je veux un rdv",
    "prendre rdv",
    "prendre rendez vous",
)

IA_CHAT_AGENT_NAME = "Antoine"
IA_CHAT_AGENT_ROLE = "Conseiller renovation"
ALLOWED_CHAT_AGENTS = {
    "antoine": ("Antoine", "Conseiller renovation"),
    "kevin": ("Kevin", "Conseiller renovation"),
    "lea": ("Lea", "Conseiller renovation"),
    "alexandre": ("Antoine", "Conseiller renovation"),
    "kevien": ("Kevin", "Conseiller renovation"),
}

WORK_TYPE_PRO_GUIDE = {
    "appartement": {
        "diagnostic": "controle redistribution des pieces, etat reseaux existants et contraintes copropriete.",
        "normes": "respect interventions en copropriete, securite electrique et conformite ventilation.",
        "modern": "optimisation des volumes, rangement integre et parcours lumineux coherent.",
        "details": ["nombre de pieces", "etage/acces ascenseur", "niveau de transformation"],
    },
    "maison": {
        "diagnostic": "lecture globale structure/enveloppe, reseaux et interaction interieur-exterieur.",
        "normes": "verification des points sensibles humidite/isolation et mise en conformite par lot.",
        "modern": "renovation evolutive: confort thermique, circulation fluide et materiaux durables.",
        "details": ["presence extension/combles", "etat facade/toiture", "niveau de finition souhaite"],
    },
    "bureaux": {
        "diagnostic": "analyse flux collaborateurs, cloisonnement, acoustique et performances CVC.",
        "normes": "conformite securite incendie, electricite tertiaire et evacuation des locaux.",
        "modern": "espaces modulaires, postes ergonomiques et eclairage adapte aux usages reels.",
        "details": ["effectif vise", "contraintes d'exploitation", "date cible de livraison"],
    },
    "commerce": {
        "diagnostic": "audit facade, surface de vente, reserve et parcours client.",
        "normes": "mise en conformite ERP/PMR, securite et continuites d'exploitation.",
        "modern": "implantation orientee conversion, ambiance marque et maintenance simplifiee.",
        "details": ["type d'activite", "horaires d'ouverture", "contraintes ERP/PMR"],
    },
    "copropriete": {
        "diagnostic": "etat parties communes, reseaux collectifs et pathologies visibles.",
        "normes": "coordination reglementaire, securite des usagers et planning de nuisance maitrise.",
        "modern": "materiaux robustes, maintenance facilitee et image valorisee de l'immeuble.",
        "details": ["zones concernees", "contraintes syndic", "occupation du site pendant travaux"],
    },
    "salle de bain": {
        "diagnostic": "controle humidite supports, etat reseaux EF/ECS, pente et evacuation.",
        "normes": "etancheite SPEC + appareillages adaptes aux volumes d'eau (NFC 15-100).",
        "modern": "douche extra-plate, eclairage LED IP44, mobilier suspendu et ventilation performante.",
        "details": ["etat actuel des murs/sol", "type de douche/baignoire souhaite", "niveau de finition"],
    },
    "cuisine": {
        "diagnostic": "verification implantation, reseaux eau/electricite et ergonomie des circulations.",
        "normes": "protection circuits specialises, section des cables et securite tableau (NFC 15-100).",
        "modern": "plan de travail durable, rangements integres, eclairage technique + ambiance.",
        "details": ["implantation souhaitee", "electromenager prevu", "materiaux de facade/plan de travail"],
    },
    "plomberie": {
        "diagnostic": "diagnostic pression, reseaux existants, fuites potentielles et acces techniques.",
        "normes": "respect DTU plomberie, raccordements securises et evacuation conformes.",
        "modern": "robinetterie economique, reseaux optimises et maintenance facilitee.",
        "details": ["nature de la panne/projet", "anciennete installation", "zones impactees"],
    },
    "electricite": {
        "diagnostic": "etat du tableau, repartition des circuits et adequation puissance/usages.",
        "normes": "mise en conformite NFC 15-100, protections diff 30mA et sections adaptees.",
        "modern": "eclairage LED, pilotage intelligent et prises positionnees selon usages reels.",
        "details": ["date de la derniere renovation elec", "equipements energivores", "besoin domotique"],
    },
    "peinture": {
        "diagnostic": "analyse supports, fissures, humidite et reprises necessaires avant finition.",
        "normes": "preparation complete supports (enduit, poncage, primaire) pour tenue durable.",
        "modern": "finition mate velours lessivable et harmonisation colorimetrique contemporaine.",
        "details": ["etat des supports", "teintes souhaitees", "hauteur/plafonds speciaux"],
    },
    "isolation": {
        "diagnostic": "identification ponts thermiques, menuiseries faibles et deperditions majeures.",
        "normes": "mise en oeuvre conforme des isolants + gestion vapeur/ventilation associee.",
        "modern": "isolation performante avec confort ete/hiver et gain energetique measurable.",
        "details": ["zones a isoler", "objectif thermique", "contraintes d'epaisseur"],
    },
    "facade": {
        "diagnostic": "controle fissures, support, humidite et tenue des anciennes couches.",
        "normes": "systeme de facade adapte au support + traitement durable des desordres.",
        "modern": "finitions minces, teintes contemporaines et protection longue duree.",
        "details": ["surface facade", "nature du support", "presence fissures/infiltrations"],
    },
    "toiture": {
        "diagnostic": "etat couverture, zinguerie, points d'infiltration et ventilation en toiture.",
        "normes": "respect DTU couverture, etancheite et securisation des points singuliers.",
        "modern": "traitement global performant pour durabilite, confort et maintenance reduite.",
        "details": ["type de couverture", "age toiture", "fuites ou traces d'humidite"],
    },
    "renovation": {
        "diagnostic": "lecture TCE globale: structure, reseaux, contraintes d'acces et sequence travaux.",
        "normes": "planification lot par lot avec controles qualite et mise aux normes progressive.",
        "modern": "pilotage chantier data-driven, projection 3D et decisions rapides avant execution.",
        "details": ["pieces concernees", "niveau de transformation", "objectif design/fonctionnel"],
    },
}

DEFAULT_PRO_GUIDE = {
    "diagnostic": "analyse complete des lots TCE, contraintes techniques et risques chantier.",
    "normes": "methodologie de controle qualite + conformite des interventions par lot.",
    "modern": "approche renovation moderne: projection, pilotage centralise et execution coordonnee.",
    "details": ["type de travaux", "surface en m2", "ville", "niveau de finition"],
}

IDF_SECTORS = [
    {
        "code": "75",
        "name": "Paris",
        "cities": ["Paris"],
    },
    {
        "code": "77",
        "name": "Seine-et-Marne",
        "cities": [
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
        ],
    },
    {
        "code": "78",
        "name": "Yvelines",
        "cities": [
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
        ],
    },
    {
        "code": "91",
        "name": "Essonne",
        "cities": [
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
        ],
    },
    {
        "code": "92",
        "name": "Hauts-de-Seine",
        "cities": [
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
        ],
    },
    {
        "code": "93",
        "name": "Seine-Saint-Denis",
        "cities": [
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
        ],
    },
    {
        "code": "94",
        "name": "Val-de-Marne",
        "cities": [
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
        ],
    },
    {
        "code": "95",
        "name": "Val-d'Oise",
        "cities": [
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
        ],
    },
]


def wants_human_help(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in HANDOFF_KEYWORDS)


def has_contact_info(text: str) -> bool:
    email_re = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    for match in re.finditer(r"(?:\+?\d[\d\s().-]{6,}\d)", text):
        digits = re.sub(r"\D", "", match.group(0))
        if 8 <= len(digits) <= 15:
            return True
    return bool(email_re.search(text))


def _resolve_chat_agent(agent_name: str | None, agent_role: str | None) -> tuple[str, str]:
    key = (agent_name or "").strip().lower()
    if key in ALLOWED_CHAT_AGENTS:
        return ALLOWED_CHAT_AGENTS[key]

    role_key = (agent_role or "").strip().lower()
    if role_key == "conseiller renovation":
        return IA_CHAT_AGENT_NAME, IA_CHAT_AGENT_ROLE

    return IA_CHAT_AGENT_NAME, IA_CHAT_AGENT_ROLE


def _detect_work_type(text: str) -> str | None:
    t = (text or "").lower()
    if "copropriete" in t or "parties communes" in t:
        return "copropriete"
    if "commerce" in t or "boutique" in t or "local commercial" in t or "erp" in t:
        return "commerce"
    if "bureaux" in t or "bureau" in t or "tertiaire" in t:
        return "bureaux"
    if "appartement" in t:
        return "appartement"
    if "maison" in t or "pavillon" in t:
        return "maison"
    if "salle de bain" in t:
        return "salle de bain"
    if "cuisine" in t:
        return "cuisine"
    if "plomberie" in t:
        return "plomberie"
    if "electricite" in t or "electrique" in t or "electric" in t:
        return "electricite"
    if "peinture" in t:
        return "peinture"
    if "maconnerie" in t:
        return "maconnerie"
    if "sol " in t or "sols" in t or "carrelage" in t or "parquet" in t:
        return "sols"
    if "isolation" in t:
        return "isolation"
    if "facade" in t:
        return "facade"
    if "toiture" in t:
        return "toiture"
    if "renov" in t:
        return "renovation"
    return None


def _extract_surface_m2(text: str) -> float | None:
    m = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:m[2²]|metres?\s*carres?|m[eè]tres?\s*carr[eé]s?|carr[eé]s?)\b",
        text or "",
        re.I,
    )
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    no_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", no_accents).strip().lower()


def _is_ack_message(text: str) -> bool:
    folded = _fold_text(text)
    if not folded or len(folded) > 80:
        return False
    return any(token in folded for token in ACK_KEYWORDS)


def _is_schedule_intent(text: str) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    return any(token in folded for token in SCHEDULE_INTENT_KEYWORDS)


def _find_known_city_in_text(text: str) -> str | None:
    folded_text = _fold_text(text)
    if not folded_text:
        return None

    for sector in IDF_SECTORS:
        for city in sector["cities"]:
            folded_city = _fold_text(city)
            if re.search(rf"\b{re.escape(folded_city)}\b", folded_text):
                return city
    return None


def _extract_city_hint(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None

    m = re.search(r"\b[àa]\b\s+([A-Za-zÀ-ÖØ-öø-ÿ' -]{2,})", raw, re.I)
    if m:
        city_fragment = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
        known_city = _find_known_city_in_text(city_fragment)
        if known_city:
            return known_city

        city_candidate = re.split(
            r"\b(?:avec|sans|pour|et|devis|budget|sous|travaux)\b",
            city_fragment,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" .,-")
        if len(city_candidate) >= 2:
            return city_candidate[:60]

    known_city = _find_known_city_in_text(raw)
    if known_city:
        return known_city

    tail_caps = re.search(r"\b([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ' -]{2,})\b$", raw)
    if tail_caps:
        city_guess = tail_caps.group(1).strip(" .,-").title()
        if len(city_guess) >= 2:
            return city_guess[:60]

    return None


def _extract_budget_hint(text: str) -> int | None:
    t = text or ""
    m = re.search(r"(\d{2,3}(?:[ .]\d{3})+|\d{4,6})\s*(?:€|eur|euro|euros)\b", t, re.I)
    if not m:
        m = re.search(r"\bbudget\b[^0-9]{0,8}(\d{2,3}(?:[ .]\d{3})+|\d{4,6})\b", t, re.I)
    if not m:
        return None
    raw = re.sub(r"\D", "", m.group(1))
    if not raw:
        return None
    value = int(raw)
    if 500 <= value <= 2_000_000:
        return value
    return None


def _extract_timeline_hint(text: str) -> str | None:
    t = (text or "").lower()
    if any(token in t for token in ("urgent", "au plus vite", "des que possible", "asap")):
        return "urgent"
    m = re.search(r"sous\s+(\d+)\s*(jour|jours|semaine|semaines|mois)", t)
    if not m:
        return None
    qty = m.group(1)
    unit = m.group(2)
    return f"sous {qty} {unit}"


def _detect_client_mood(text: str) -> str:
    folded = _fold_text(text)
    if not folded:
        return "neutral"

    stress_markers = (
        "stress",
        "stresse",
        "angoisse",
        "peur",
        "perdu",
        "inquiet",
        "galere",
        "urgent",
        "panique",
    )
    hesitation_markers = ("hesite", "hesitation", "doute", "pas sur", "incertain")
    positive_markers = ("merci", "super", "parfait", "top", "genial", "nickel", "cool")

    if any(token in folded for token in stress_markers):
        return "stress"
    if any(token in folded for token in hesitation_markers):
        return "hesitation"
    if any(token in folded for token in positive_markers):
        return "positive"
    return "neutral"


def _extract_chat_context(user_messages: list[str]) -> dict:
    messages = [str(m or "").strip() for m in (user_messages or []) if str(m or "").strip()]
    if not messages:
        return {
            "work_type": None,
            "surface_m2": None,
            "city_hint": None,
            "budget_hint": None,
            "timeline_hint": None,
            "client_mood": "neutral",
        }

    work_type = None
    surface_m2 = None
    city_hint = None
    budget_hint = None
    timeline_hint = None

    for text in messages:
        if work_type is None:
            work_type = _detect_work_type(text)
        if surface_m2 is None:
            surface_m2 = _extract_surface_m2(text)
        if city_hint is None:
            city_hint = _extract_city_hint(text)
        if budget_hint is None:
            budget_hint = _extract_budget_hint(text)
        if timeline_hint is None:
            timeline_hint = _extract_timeline_hint(text)

    full_text = " ".join(messages)
    if work_type is None:
        work_type = _detect_work_type(full_text)
    if surface_m2 is None:
        surface_m2 = _extract_surface_m2(full_text)
    if city_hint is None:
        city_hint = _extract_city_hint(full_text)
    if budget_hint is None:
        budget_hint = _extract_budget_hint(full_text)
    if timeline_hint is None:
        timeline_hint = _extract_timeline_hint(full_text)

    return {
        "work_type": work_type,
        "surface_m2": surface_m2,
        "city_hint": city_hint,
        "budget_hint": budget_hint,
        "timeline_hint": timeline_hint,
        "client_mood": _detect_client_mood(full_text),
    }


def _human_eur(value: str | int | float | None) -> str:
    if value is None:
        return ""
    raw = str(value).strip().lower().replace(",", ".")
    raw = re.sub(r"[^0-9.]", "", raw)
    if not raw:
        return str(value)
    try:
        amount = int(round(float(raw)))
    except ValueError:
        return str(value)
    return f"{amount:,}".replace(",", " ") + " EUR"


def _pro_guide_for_work(work_type: str | None) -> dict:
    return WORK_TYPE_PRO_GUIDE.get((work_type or "").strip().lower(), DEFAULT_PRO_GUIDE)


def _build_professional_chat_reply(
    agent_name: str,
    agent_role: str,
    estimate: dict | None,
    contact_detected: bool,
    contact_just_provided: bool,
    is_first_turn: bool,
    work_type: str | None,
    missing_fields: list[str],
    surface_m2: float | None = None,
    city_hint: str | None = None,
    client_mood: str = "neutral",
    budget_hint: int | None = None,
    timeline_hint: str | None = None,
) -> str:
    guide = _pro_guide_for_work(work_type)
    details_needed = list(guide.get("details", []))

    core_missing: list[str] = []
    for item in missing_fields:
        if item and item not in core_missing:
            core_missing.append(item)

    detail_missing: list[str] = []
    for item in details_needed:
        if item and item not in core_missing and item not in detail_missing:
            detail_missing.append(item)

    work_label_map = {
        "appartement": "appartement",
        "maison": "maison",
        "bureaux": "bureaux",
        "commerce": "commerce",
        "copropriete": "copropriete",
        "salle de bain": "salle de bain",
        "cuisine": "cuisine",
        "plomberie": "plomberie",
        "electricite": "electricite",
        "peinture": "peinture",
        "isolation": "isolation",
        "facade": "facade",
        "toiture": "toiture",
        "renovation": "renovation",
    }
    work_label = work_label_map.get((work_type or "").strip().lower(), "projet de renovation")

    if is_first_turn:
        intro = f"Bonjour, je suis {agent_name}, {agent_role.lower()} et conducteur de projet batiment."
    else:
        intro = "Bien recu, je garde le fil de votre dossier chantier."

    if client_mood == "stress":
        empathy_line = "Je comprends que ce soit stressant: on va cadrer le projet simplement, etape par etape."
    elif client_mood == "hesitation":
        empathy_line = "Vous avez raison d'etre prudent: l'objectif est de securiser cout, delai et qualite avant decision."
    elif contact_just_provided:
        empathy_line = "Merci pour vos coordonnees, je garde votre contexte pour un rappel utile et concret."
    elif client_mood == "positive":
        empathy_line = "Parfait, on avance bien: je reste sur du concret terrain."
    else:
        empathy_line = "Je vous reponds comme sur chantier: clair, technique et sans blabla commercial."

    context_bits: list[str] = []
    if work_type:
        context_bits.append(work_label)
    if surface_m2:
        context_bits.append(f"{int(round(surface_m2))} m2")
    if city_hint:
        context_bits.append(city_hint)
    context_line = f"J'ai bien note: {', '.join(context_bits)}." if context_bits else None

    diagnostic_line = f"Diagnostic prioritaire: {guide['diagnostic']}"
    norms_line = f"Qualite et conformite: {guide['normes']}"
    conductor_line = "Pilotage conducteur de projet: sequence des lots, controle qualite et point d'avancement chaque semaine."

    project_frame_bits: list[str] = []
    if budget_hint is not None:
        project_frame_bits.append(f"budget cible {_human_eur(budget_hint)}")
    if timeline_hint:
        project_frame_bits.append(f"echeance {timeline_hint}")
    project_frame_line = f"Cadrage actuel: {', '.join(project_frame_bits)}." if project_frame_bits else None

    estimate_line = None
    if estimate and estimate.get("min") and estimate.get("max"):
        estimate_line = (
            f"Premiere fourchette: {_human_eur(estimate['min'])} - {_human_eur(estimate['max'])} "
            "(a confirmer apres visite technique et releve)."
        )

    lines: list[str] = [intro, empathy_line]
    if context_line:
        lines.append(context_line)
    if estimate_line:
        lines.append(estimate_line)
    lines.extend([diagnostic_line, norms_line, conductor_line])
    if project_frame_line:
        lines.append(project_frame_line)

    if core_missing:
        lines.append(f"Pour verrouiller le devis, il me manque encore: {', '.join(core_missing[:3])}.")
    elif detail_missing:
        lines.append(f"Pour affiner la strategie chantier: {', '.join(detail_missing[:2])}.")

    if contact_detected:
        lines.append("Suite concrete: on bloque un creneau, puis visite technique et devis detaille lot par lot.")
    else:
        lines.append("Si vous voulez, laissez telephone ou email et je vous cale la suite avec un conducteur de projet.")

    return "\n".join(lines)


def _build_contextual_short_reply(
    agent_name: str,
    contact_detected: bool,
    work_type: str | None,
    surface_m2: float | None,
    city_hint: str | None,
    client_mood: str,
    budget_hint: int | None,
    timeline_hint: str | None,
    estimate: dict | None,
) -> str:
    missing: list[str] = []
    if not work_type:
        missing.append("type de travaux")
    if not surface_m2:
        missing.append("surface en m2")
    if not city_hint:
        missing.append("ville")
    if budget_hint is None:
        missing.append("budget cible")
    if not timeline_hint:
        missing.append("echeance souhaitee")

    context_bits: list[str] = []
    if work_type:
        context_bits.append(work_type)
    if surface_m2:
        context_bits.append(f"{int(round(surface_m2))} m2")
    if city_hint:
        context_bits.append(city_hint)
    context_line = f"J'ai bien note: {', '.join(context_bits)}." if context_bits else "Je garde bien le fil de votre dossier."

    if client_mood == "stress":
        mood_line = "Je comprends, on va rester simple et concret pour vous enlever la charge."
    elif client_mood == "hesitation":
        mood_line = "C'est normal d'hesiter, l'important est de valider les points techniques avant de s'engager."
    else:
        mood_line = "On avance proprement, avec une logique chantier."

    if not contact_detected:
        if missing:
            return (
                f"{context_line} {mood_line} "
                f"Pour avancer, il me manque: {', '.join(missing[:3])}. "
                "Ajoutez aussi votre telephone ou email pour que je lance la suite."
            )
        return (
            f"{context_line} {mood_line} "
            "Laissez votre telephone ou email et je vous cale la suite devis + visite technique."
        )

    if missing:
        return (
            f"{context_line} Merci, j'ai bien vos coordonnees. "
            f"Pour finaliser proprement, il me manque: {', '.join(missing[:3])}."
        )

    if estimate and estimate.get("min") and estimate.get("max"):
        return (
            f"{context_line} "
            f"Premiere fourchette: {_human_eur(estimate['min'])} - {_human_eur(estimate['max'])}. "
            "Prochaine etape: visite technique, releve et devis detaille."
        )

    return (
        f"{context_line} "
        f"On est cadres sur l'essentiel, je vous propose la suite operationnelle avec {agent_name}."
    )


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return suffix
    return ".jpg"


def _public_static_url(path: Path) -> str:
    rel = path.relative_to(STATIC_DIR).as_posix()
    return f"/static/{rel}"


def _compose_architecture_prompt(
    project_type: str,
    style: str,
    city: str,
    surface: str,
    notes: str,
    scope: str,
    timeline: str,
    want_free_interior: bool,
) -> str:
    return (
        "Architectural interior 3D renovation render, photorealistic, high-end proposal, "
        "use the uploaded client photos as reference for geometry, openings, proportions and existing constraints, "
        "do not invent another room layout unrelated to the source photos, "
        f"project type: {project_type}, style: {style}, city: {city or 'Ile-de-France'}, "
        f"scope: {scope or 'renovation_complete'}, timeline: {timeline or '6_mois'}, "
        f"surface: {surface or 'unknown'}, interior-design-pack: {'yes' if want_free_interior else 'no'}, "
        f"client notes: {notes or 'none'}, "
        "clean daylight, realistic materials, coherent scale, construction-feasible details, "
        "show a modern renovation result grounded in the original photo context, "
        "single final render."
    )


def _image_mime_from_suffix(suffix: str) -> str:
    key = (suffix or "").lower()
    if key in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if key == ".png":
        return "image/png"
    if key == ".webp":
        return "image/webp"
    return "image/jpeg"


def _build_multipart_form_data(
    fields: dict[str, str], files: list[tuple[str, str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = f"----rbia{uuid.uuid4().hex}"
    body = bytearray()

    for key, value in (fields or {}).items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for field_name, filename, content, content_type in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), boundary


def _extract_openai_image_bytes(data: dict) -> tuple[bytes | None, str | None]:
    item = (data.get("data") or [{}])[0]
    b64 = item.get("b64_json")
    if b64:
        try:
            return base64.b64decode(b64), None
        except (ValueError, binascii.Error, TypeError) as exc:
            return None, f"Invalid b64 image: {exc}"

    image_url = item.get("url")
    if image_url:
        try:
            with urlrequest.urlopen(image_url, timeout=60) as resp:
                return resp.read(), None
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            return None, f"Image URL download failed: {exc}"

    return None, "No image returned by OpenAI"


def _generate_render_with_openai(
    prompt: str, reference_images: list[tuple[str, bytes]] | None = None
) -> tuple[bytes | None, str | None]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY missing"

    model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    size = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")

    try:
        files: list[tuple[str, str, bytes, str]] = []
        for idx, item in enumerate((reference_images or [])[:3]):
            filename_raw, content = item
            if not content:
                continue
            suffix = _safe_suffix(filename_raw)
            files.append(
                (
                    "image",
                    f"source_{idx + 1}{suffix}",
                    content,
                    _image_mime_from_suffix(suffix),
                )
            )

        if files:
            fields = {
                "model": model,
                "prompt": prompt,
                "size": size,
                "response_format": "b64_json",
            }
            body, boundary = _build_multipart_form_data(fields, files)
            req = urlrequest.Request(
                "https://api.openai.com/v1/images/edits",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )
        else:
            payload = {
                "model": model,
                "prompt": prompt,
                "size": size,
                "response_format": "b64_json",
            }
            req = urlrequest.Request(
                "https://api.openai.com/v1/images/generations",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )

        with urlrequest.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _extract_openai_image_bytes(data)
    except urllib_error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="ignore")
        return None, f"HTTP {exc.code}: {details[:300]}"
    except (urllib_error.URLError, TimeoutError, OSError, ValueError) as exc:  # pragma: no cover - runtime networking guard
        return None, str(exc)


def _normalise_chat_messages(messages: list[dict]) -> list[dict[str, str]]:
    safe_messages: list[dict[str, str]] = []
    for item in (messages or [])[-10:]:
        role = "assistant" if (item or {}).get("role") == "assistant" else "user"
        content = str((item or {}).get("content", "")).strip()
        if not content:
            continue
        safe_messages.append({"role": role, "content": content[:1200]})
    return safe_messages


def _generate_chat_reply_with_openai(
    messages: list[dict],
    agent_name: str,
    agent_role: str,
    estimate: dict | None,
    contact_detected: bool,
    is_first_turn: bool,
    work_type: str | None,
    surface_m2: float | None,
    city_hint: str | None,
    budget_hint: int | None,
    timeline_hint: str | None,
    client_mood: str,
    suggest_handoff: bool,
) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    conversation = _normalise_chat_messages(messages)
    if not conversation:
        return None

    estimate_text = "none"
    if estimate:
        estimate_text = f"{estimate.get('min', '?')}EUR - {estimate.get('max', '?')}EUR"

    system_prompt = (
        f"Tu es {agent_name}, {agent_role}. "
        "Tu es un vrai professionnel terrain du batiment (renovation moderne TCE) et conducteur de projet, pas un assistant generique. "
        "Reponds uniquement en francais, avec un ton humain, naturel, professionnel et rassurant. "
        "Tu gardes aussi une casquette humaine: empathie, ecoute, et reformulation utile quand le client est stressé ou hesitant. "
        "Ecris comme un vrai conseiller qui comprend le client, pas comme un robot. "
        "Evite les formulations froides ou mecaniques. "
        "Ne repete pas la salutation 'Bonjour' a chaque message. "
        "Si ce n'est pas le premier tour, ne te represents pas (pas de 'ici Antoine...'). "
        "Structure en 4 a 8 lignes courtes. "
        "Integre systematiquement: 1) un angle diagnostic chantier, 2) un point normes/qualite, 3) une suite operationnelle. "
        "Si infos manquantes, demande uniquement les donnees critiques pour chiffrer serieusement. "
        "Ne promets jamais un prix ferme sans visite technique ni releve precis."
    )

    context_prompt = (
        "Contexte metier:\n"
        f"- estimation_initiale: {estimate_text}\n"
        f"- contact_detecte: {'oui' if contact_detected else 'non'}\n"
        f"- premier_tour: {'oui' if is_first_turn else 'non'}\n"
        f"- type_travaux: {work_type or 'inconnu'}\n"
        f"- surface_m2: {surface_m2 if surface_m2 is not None else 'inconnue'}\n"
        f"- ville: {city_hint or 'inconnue'}\n"
        f"- budget_cible: {_human_eur(budget_hint) if budget_hint is not None else 'inconnu'}\n"
        f"- echeance: {timeline_hint or 'inconnue'}\n"
        f"- humeur_client: {client_mood or 'neutral'}\n"
        f"- demande_humain: {'oui' if suggest_handoff else 'non'}\n"
        "Objectif: qualifier vite, rassurer techniquement, faire avancer vers devis et RDV."
    )

    payload = {
        "model": os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
        "temperature": 0.25,
        "max_tokens": 280,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": context_prompt},
            *conversation,
        ],
    }

    req = urlrequest.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        reply = (
            (data.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        return reply or None
    except (
        urllib_error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):  # pragma: no cover - external API guard
        return None


def _is_professional_reply(reply: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return False

    t = text.lower()
    has_diagnostic = any(
        key in t
        for key in (
            "diagnostic",
            "analyse chantier",
            "chantier",
            "technique",
            "supports",
            "reseaux",
            "implantation",
            "structure",
        )
    )
    has_normes = any(
        key in t
        for key in ("norme", "nfc", "dtu", "qualite", "conforme", "etancheite", "securite")
    )
    has_next_step = any(
        key in t
        for key in (
            "etape suivante",
            "prochaine etape",
            "rdv",
            "rendez",
            "visite",
            "devis",
            "releve",
            "planning",
            "il me faut",
            "j'ai besoin",
            "envoyez",
            "transmettez",
        )
    )

    return has_diagnostic and has_normes and has_next_step


def _is_human_tone_reply(reply: str, is_first_turn: bool) -> bool:
    t = (reply or "").strip().lower()
    if not t:
        return False

    if not is_first_turn and t.startswith("bonjour"):
        return False
    if not is_first_turn and "ici " in t[:80] and "conseiller renovation" in t:
        return False

    robotic_markers = (
        "je vous reponds avec une expertise batiment terrain",
        "pour produire un chiffrage exploitable",
        "coordonnees recues:",
        "point technique:",
        "nos conseillers sont des professionnels travaux",
    )
    return not any(marker in t for marker in robotic_markers)


def _parse_number(value: object) -> float | None:
    if value is None:
        return None
    cleaned = str(value).strip().lower().replace(",", ".")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _as_bool(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "oui", "on"}


def _format_eur(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " EUR"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "oui"}


def _is_placeholder_secret(value: str) -> bool:
    t = (value or "").strip()
    if not t:
        return True
    lowered = t.lower()
    return any(
        marker in lowered
        for marker in (
            "votre_",
            "your_",
            "change_me",
            "placeholder",
            "example",
            "mot_de_passe",
            "openai_api_key",
        )
    )


def _clean_text(value: object, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _normalize_visitor_id(value: object) -> str | None:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", _clean_text(value, limit=80))
    if len(cleaned) < 8:
        return None
    return cleaned[:64]


def _parse_json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    raw = value.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_tracking_from_url(raw_url: str) -> dict:
    url_value = _clean_text(raw_url, limit=1000)
    if not url_value:
        return {}
    try:
        query = parse_qs(urlparse(url_value).query, keep_blank_values=False)
    except ValueError:
        return {}

    out: dict[str, str] = {}
    for key in TRACKING_PARAM_KEYS:
        values = query.get(key)
        if not values:
            continue
        val = _clean_text(values[0], limit=240)
        if val:
            out[key] = val
    return out


def _extract_tracking_context(request: Request, payload: dict | None = None) -> dict:
    data = payload or {}
    nested = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}

    visitor_id = _normalize_visitor_id(
        data.get("visitor_id")
        or nested.get("visitor_id")
        or getattr(getattr(request, "state", object()), "visitor_id", None)
        or request.cookies.get(VISITOR_COOKIE_NAME)
    )
    if not visitor_id:
        visitor_id = uuid.uuid4().hex

    landing = _clean_text(
        data.get("visitor_landing")
        or nested.get("landing")
        or nested.get("visitor_landing"),
        limit=800,
    )
    referrer = _clean_text(
        data.get("visitor_referrer")
        or nested.get("referrer")
        or nested.get("visitor_referrer")
        or request.headers.get("referer", ""),
        limit=800,
    )

    utm: dict[str, str] = {}
    utm.update(_parse_json_dict(data.get("visitor_utm")))
    utm.update(_parse_json_dict(nested.get("utm")))
    for key in TRACKING_PARAM_KEYS:
        candidate = data.get(key)
        if candidate is None:
            candidate = nested.get(key)
        candidate_val = _clean_text(candidate, limit=240)
        if candidate_val:
            utm[key] = candidate_val

    if referrer:
        referrer_utm = _extract_tracking_from_url(referrer)
        for key, value in referrer_utm.items():
            utm.setdefault(key, value)

    normalized_utm: dict[str, str] = {}
    for key in TRACKING_PARAM_KEYS:
        value = utm.get(key)
        if value is None:
            continue
        normalized_value = _clean_text(value, limit=240)
        if normalized_value:
            normalized_utm[key] = normalized_value

    return {
        "visitor_id": visitor_id,
        "landing": landing,
        "referrer": referrer,
        "utm": normalized_utm,
        "user_agent": _clean_text(request.headers.get("user-agent", ""), limit=240),
    }


def _attach_tracking_to_raw(raw_message: object, tracking: dict) -> str:
    text = _clean_text(raw_message, limit=12000)
    block = json.dumps({"tracking": tracking}, ensure_ascii=False)
    if text:
        return f"{text}\n\n[tracking]\n{block}"
    return f"[tracking]\n{block}"


def _attach_tracking_to_conversation(conversation: object, tracking: dict) -> str:
    if isinstance(conversation, dict):
        payload = dict(conversation)
        payload["tracking"] = tracking
        return json.dumps(payload, ensure_ascii=False)

    if isinstance(conversation, str):
        raw = conversation.strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    parsed["tracking"] = tracking
                    return json.dumps(parsed, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
            return json.dumps({"transcript": raw, "tracking": tracking}, ensure_ascii=False)

    return json.dumps({"tracking": tracking}, ensure_ascii=False)


def _smtp_settings() -> dict:
    default_from_email = (INTERNAL_REPORT_EMAIL or "celia.b@keythinkers.fr").strip()
    host = os.getenv("SMTP_HOST", "smtp.office365.com").strip()
    port_raw = os.getenv("SMTP_PORT", "587").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 587

    from_email = os.getenv("SMTP_FROM_EMAIL", default_from_email).strip()
    smtp_user = os.getenv("SMTP_USER", from_email).strip()

    return {
        "host": host,
        "port": port,
        "user": smtp_user,
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "from_name": os.getenv("SMTP_FROM_NAME", "Renovation Batiment IA").strip(),
        "from_email": from_email,
        "starttls": _env_bool("SMTP_STARTTLS", True),
    }


def _smtp_ready(cfg: dict | None = None) -> bool:
    current = cfg or _smtp_settings()
    password = str(current.get("password") or "")
    return bool(
        current.get("host")
        and current.get("from_email")
        and current.get("user")
        and not _is_placeholder_secret(password)
    )


def _send_email_message(
    to_email: str | None,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> tuple[bool, str | None]:
    cfg = _smtp_settings()
    recipient = (to_email or "").strip()
    if not cfg["host"] or not cfg["from_email"]:
        return False, "smtp_not_configured"
    if not recipient:
        return False, "missing_recipient"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["from_name"], cfg["from_email"]))
    msg["To"] = recipient
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=25) as server:
            if cfg["starttls"]:
                server.starttls()
            if cfg["user"] and cfg["password"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return True, None
    except (smtplib.SMTPException, OSError) as exc:
        return False, str(exc)


def _to_public_link(path_or_url: str) -> str:
    raw = (path_or_url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return raw
    if not PUBLIC_BASE_URL:
        if raw.startswith("/"):
            return raw
        return f"/{raw.lstrip('/')}"
    if raw.startswith("/"):
        return f"{PUBLIC_BASE_URL}{raw}"
    return f"{PUBLIC_BASE_URL}/{raw.lstrip('/')}"


def _openai_key_ready() -> bool:
    key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
    return bool(key and not _is_placeholder_secret(key))


def _compose_client_quote_email(
    *,
    name: str,
    city: str,
    project_type: str,
    scope: str,
    style: str,
    quote: dict,
    renders: list[str],
    source_photos: list[str],
) -> tuple[str, str]:
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "projet")
    scope_label = SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"])
    duration = (quote or {}).get("duration_weeks", {}) or {}
    duration_label = f"{duration.get('min', '?')} a {duration.get('max', '?')} semaines"
    client_name = (name or "").strip() or "Bonjour"

    subject = f"Votre devis intelligent + rendu 3D - {project_label}"
    lines = [
        f"{client_name},",
        "",
        "Merci pour votre demande.",
        "Voici votre devis intelligent et votre rendu 3D.",
        "",
        "Synthese projet",
        f"- Type: {project_label}",
        f"- Perimetre: {scope_label}",
        f"- Style: {(style or 'moderne').capitalize()}",
        f"- Ville: {city or 'A confirmer'}",
        f"- Budget estime: {(quote or {}).get('low_label', 'A confirmer')} - {(quote or {}).get('high_label', 'A confirmer')}",
        f"- Delai indicatif: {duration_label}",
        f"- Confiance estimation: {int(round(float((quote or {}).get('confidence', 0)) * 100))}%",
        "",
        "Postes budgetaires",
    ]

    for item in (quote or {}).get("breakdown", [])[:8]:
        lines.append(f"- {item.get('label', 'Poste')}: {item.get('low_label', '?')} - {item.get('high_label', '?')}")

    lines.extend(["", "Rendus 3D (liens)"])
    if renders:
        for render in renders:
            lines.append(f"- {_to_public_link(render)}")
    else:
        lines.append("- Rendu 3D indisponible")

    lines.extend(["", "Photos source du projet"])
    for src in (source_photos or [])[:6]:
        lines.append(f"- {_to_public_link(src)}")

    lines.extend(
        [
            "",
            "Prochaine etape",
            "- Un conducteur de projet vous contacte pour valider les points techniques.",
            "- Puis visite technique et devis detaille lot par lot.",
            "",
            f"Equipe Renovation Batiment IA\n{PUBLIC_BASE_URL}",
        ]
    )

    return subject, "\n".join(lines)


def _compose_client_devis_email(
    *,
    name: str,
    city: str,
    project_type: str,
    scope: str,
    style: str,
    quote: dict,
) -> tuple[str, str]:
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "projet")
    scope_label = SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"])
    duration = (quote or {}).get("duration_weeks", {}) or {}
    duration_label = f"{duration.get('min', '?')} a {duration.get('max', '?')} semaines"
    client_name = (name or "").strip() or "Bonjour"

    subject = f"Votre devis intelligent - {project_label}"
    lines = [
        f"{client_name},",
        "",
        "Merci pour votre demande.",
        "Votre devis intelligent est pret.",
        "",
        "Synthese projet",
        f"- Type: {project_label}",
        f"- Perimetre: {scope_label}",
        f"- Style: {(style or 'moderne').capitalize()}",
        f"- Ville: {city or 'A confirmer'}",
        f"- Budget estime: {(quote or {}).get('low_label', 'A confirmer')} - {(quote or {}).get('high_label', 'A confirmer')}",
        f"- Delai indicatif: {duration_label}",
        f"- Confiance estimation: {int(round(float((quote or {}).get('confidence', 0)) * 100))}%",
        "",
        "Postes budgetaires",
    ]

    for item in (quote or {}).get("breakdown", [])[:8]:
        lines.append(
            f"- {item.get('label', 'Poste')}: {item.get('low_label', '?')} - {item.get('high_label', '?')}"
        )

    lines.extend(
        [
            "",
            "Etape suivante",
            "- Vous pouvez demander le rendu 3D sur simple demande apres devis.",
            "- Notre equipe vous accompagne sur les points techniques avant appel.",
            "",
            f"Equipe Renovation Batiment IA\n{PUBLIC_BASE_URL}",
        ]
    )

    return subject, "\n".join(lines)


def _compose_client_render_email(
    *,
    name: str,
    city: str,
    project_type: str,
    scope: str,
    style: str,
    quote: dict,
    renders: list[str],
    source_photos: list[str],
) -> tuple[str, str]:
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "projet")
    scope_label = SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"])
    client_name = (name or "").strip() or "Bonjour"

    subject = f"Votre rendu 3D sur demande - {project_label}"
    lines = [
        f"{client_name},",
        "",
        "Voici vos rendus 3D generes sur demande apres devis.",
        "",
        "Rappel projet",
        f"- Type: {project_label}",
        f"- Perimetre: {scope_label}",
        f"- Style: {(style or 'moderne').capitalize()}",
        f"- Ville: {city or 'A confirmer'}",
        f"- Fourchette devis: {(quote or {}).get('low_label', 'A confirmer')} - {(quote or {}).get('high_label', 'A confirmer')}",
        "",
        "Rendus 3D (liens)",
    ]

    for render in (renders or []):
        lines.append(f"- {_to_public_link(render)}")

    lines.extend(["", "Photos source utilisees"])
    for src in (source_photos or [])[:10]:
        lines.append(f"- {_to_public_link(src)}")

    lines.extend(
        [
            "",
            "Prochaine etape",
            "- Validation des choix techniques et esthetiques.",
            "- Cadrage final avant lancement chantier.",
            "",
            f"Equipe Renovation Batiment IA\n{PUBLIC_BASE_URL}",
        ]
    )

    return subject, "\n".join(lines)


def _extract_handoff_conversation_payload(handoff: HandoffRequest | None) -> dict:
    if not handoff or not handoff.conversation:
        return {}
    try:
        payload = json.loads(handoff.conversation)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _compose_internal_report_email(
    *,
    name: str,
    phone: str,
    email: str,
    city: str,
    project_type: str,
    scope: str,
    style: str,
    timeline: str,
    surface: str,
    rooms: str,
    budget: str,
    notes: str,
    quote: dict,
    interior_request_status: str,
    precall_report: dict,
    source_photos: list[str],
    renders: list[str],
    mode: str,
    tracking_context: dict,
    client_quote_subject: str,
    client_quote_body: str,
) -> tuple[str, str]:
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "Projet")
    scope_label = SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"])

    subject = f"[Nouveau dossier IA] {project_label} - {city or 'ville a confirmer'}"

    lines = [
        "Nouveau dossier devis intelligent + projection 3D",
        "",
        "Contact client",
        f"- Nom: {name or 'A confirmer'}",
        f"- Telephone: {phone or 'A confirmer'}",
        f"- Email: {email or 'A confirmer'}",
        "",
        "Tracking lead",
        f"- Visitor ID: {(tracking_context or {}).get('visitor_id', 'N/A')}",
        f"- Landing: {(tracking_context or {}).get('landing', 'N/A') or 'N/A'}",
        f"- Referrer: {(tracking_context or {}).get('referrer', 'N/A') or 'N/A'}",
        f"- UTM: {json.dumps((tracking_context or {}).get('utm', {}), ensure_ascii=False)}",
        "",
        "Contexte projet",
        f"- Type: {project_label}",
        f"- Perimetre: {scope_label}",
        f"- Style: {style or 'moderne'}",
        f"- Echeance: {timeline or 'A confirmer'}",
        f"- Ville: {city or 'A confirmer'}",
        f"- Surface: {surface or 'A confirmer'}",
        f"- Pieces: {rooms or 'A confirmer'}",
        f"- Budget client: {budget or 'A confirmer'}",
        f"- Mode visuel: {mode}",
        "",
        "Devis intelligent",
        f"- Fourchette: {(quote or {}).get('low_label', 'A confirmer')} - {(quote or {}).get('high_label', 'A confirmer')}",
        f"- Delai: {(quote or {}).get('duration_weeks', {}).get('min', '?')} a {(quote or {}).get('duration_weeks', {}).get('max', '?')} semaines",
        f"- Confiance: {int(round(float((quote or {}).get('confidence', 0)) * 100))}%",
        f"- Budget fit: {(quote or {}).get('budget_fit', {}).get('message', 'A confirmer')}",
        "",
        "Compte rendu avant appel",
        f"- Score potentiel: {(precall_report or {}).get('conversion_score', 'N/A')}/100",
        f"- Niveau: {(precall_report or {}).get('conversion_band', 'N/A')}",
        "",
        "Points avantageux",
        *[f"- {x}" for x in (precall_report or {}).get("advantages", [])],
        "",
        "Points de vigilance",
        *[f"- {x}" for x in (precall_report or {}).get("inconvenients", [])],
        "",
        "A valider pendant l'appel",
        *[f"- {x}" for x in (precall_report or {}).get("points_to_validate", [])],
        "",
        "Plan d'appel recommande",
        *[f"- {x}" for x in (precall_report or {}).get("call_plan", [])],
        "",
        f"Demande prestation offerte IA 3D: {interior_request_status}",
        "",
        "Notes client",
        notes or "Aucune note",
        "",
        "Photos sources",
        *[f"- {PUBLIC_BASE_URL}{p}" for p in (source_photos or [])],
        "",
        "Rendus",
        *[f"- {PUBLIC_BASE_URL}{p}" for p in (renders or [])],
        "",
        "Copie du devis client envoye",
        f"- Objet: {client_quote_subject}",
        "",
        client_quote_body or "Aucune copie disponible.",
        "",
        f"Lien plateforme: {PUBLIC_BASE_URL}",
    ]

    return subject, "\n".join(lines)

def _estimate_complexity(notes: str) -> float:
    score = 1.0
    note = (notes or "").lower()
    for keyword in COMPLEXITY_KEYWORDS:
        if keyword in note:
            score += 0.025
    return min(1.2, score)


def _scope_breakdown_weights(scope: str) -> list[tuple[str, float]]:
    if scope == "rafraichissement":
        return [
            ("Preparation et protections", 0.08),
            ("Reprise supports", 0.16),
            ("Peinture / finitions", 0.34),
            ("Electricite legere", 0.12),
            ("Menuiseries et mobilier", 0.15),
            ("Pilotage et imprevus", 0.15),
        ]
    if scope == "renovation_partielle":
        return [
            ("Etudes et preparation", 0.08),
            ("Demolition / evacuation", 0.14),
            ("Lots techniques", 0.24),
            ("Revements et finitions", 0.26),
            ("Agencement interieur", 0.13),
            ("Coordination chantier", 0.15),
        ]
    if scope == "restructuration_lourde":
        return [
            ("Conception / diagnostics", 0.09),
            ("Gros oeuvre", 0.22),
            ("Lots techniques lourds", 0.25),
            ("Second oeuvre", 0.22),
            ("Agencement / design", 0.1),
            ("Coordination et reserves", 0.12),
        ]
    return [
        ("Etudes et preparation", 0.08),
        ("Demolition / reprises", 0.17),
        ("Electricite / plomberie / CVC", 0.24),
        ("Revements et finitions", 0.24),
        ("Agencement interieur", 0.12),
        ("Suivi chantier et imprevus", 0.15),
    ]


def _build_smart_quote(
    project_type: str,
    style: str,
    scope: str,
    timeline: str,
    surface: str,
    rooms: str,
    budget: str,
    city: str,
    notes: str,
) -> dict:
    scope_key = scope if scope in SMART_SCOPE_CONFIG else "renovation_complete"
    style_key = style if style in STYLE_MULTIPLIER else "moderne"
    project_key = project_type if project_type in PROJECT_TYPE_MULTIPLIER else "maison"
    timeline_key = timeline if timeline in TIMELINE_COST_MULTIPLIER else "6_mois"

    config = SMART_SCOPE_CONFIG[scope_key]
    surface_value = _parse_number(surface) or PROJECT_DEFAULT_SURFACE[project_key]
    surface_value = max(18.0, min(5000.0, surface_value))

    room_count = int(_parse_number(rooms) or 0)
    room_count = max(0, min(60, room_count))
    budget_value = _parse_number(budget)
    complexity = _estimate_complexity(notes)

    room_factor = 1.0
    if room_count:
        room_factor += min(0.2, (room_count - 2) * 0.02)
    if room_count == 1 and surface_value > 60:
        room_factor += 0.05

    low_raw = (
        surface_value
        * config["low_m2"]
        * PROJECT_TYPE_MULTIPLIER[project_key]
        * STYLE_MULTIPLIER[style_key]
        * TIMELINE_COST_MULTIPLIER[timeline_key]
        * complexity
        * room_factor
    )
    high_raw = (
        surface_value
        * config["high_m2"]
        * PROJECT_TYPE_MULTIPLIER[project_key]
        * STYLE_MULTIPLIER[style_key]
        * TIMELINE_COST_MULTIPLIER[timeline_key]
        * complexity
        * room_factor
    )

    if high_raw <= low_raw:
        high_raw = low_raw * 1.2

    low = int(round(low_raw / 100.0) * 100)
    high = int(round(high_raw / 100.0) * 100)

    base_min_weeks, base_max_weeks = config["duration"]
    duration_surface_factor = max(0.7, min(2.4, surface_value / 90.0))
    duration_room_factor = 1.0 + min(0.25, room_count * 0.015)
    duration_complexity = 0.97 + (complexity - 1.0) * 1.3
    duration_timeline = TIMELINE_DURATION_MULTIPLIER[timeline_key]

    duration_min = int(
        max(
            2,
            round(
                base_min_weeks
                * duration_surface_factor
                * duration_room_factor
                * duration_complexity
                * duration_timeline
            ),
        )
    )
    duration_max = int(
        max(
            duration_min + 1,
            round(
                base_max_weeks
                * duration_surface_factor
                * duration_room_factor
                * duration_complexity
                * duration_timeline
            ),
        )
    )

    breakdown = []
    for label, weight in _scope_breakdown_weights(scope_key):
        part_low = int(round(low * weight / 100.0) * 100)
        part_high = int(round(high * weight / 100.0) * 100)
        breakdown.append(
            {
                "label": label,
                "share_percent": round(weight * 100),
                "low": part_low,
                "high": part_high,
                "low_label": _format_eur(part_low),
                "high_label": _format_eur(part_high),
            }
        )

    budget_fit = {"status": "unknown", "message": "Budget non renseigne."}
    if budget_value and budget_value > 0:
        budget_amount = int(round(budget_value))
        if budget_amount < low * 0.85:
            budget_fit = {
                "status": "under_budget",
                "message": "Budget probablement trop serre: prioriser les postes essentiels.",
            }
        elif budget_amount > high * 1.3:
            budget_fit = {
                "status": "over_budget",
                "message": "Budget confortable: possibilite de finitions premium.",
            }
        else:
            budget_fit = {
                "status": "aligned",
                "message": "Budget coherent avec l'estimation actuelle.",
            }

    confidence = 0.52
    if surface:
        confidence += 0.14
    if budget:
        confidence += 0.08
    if city:
        confidence += 0.06
    if notes and len(notes.strip()) >= 24:
        confidence += 0.08
    if room_count:
        confidence += 0.06
    confidence = round(min(0.92, confidence), 2)

    assumptions = [
        f"Perimetre: {SMART_SCOPE_LABELS.get(scope_key, SMART_SCOPE_LABELS['renovation_complete'])}.",
        f"Style vise: {style_key}.",
        "Hors contraintes administratives exceptionnelles et diagnostics destructifs.",
    ]
    if not surface:
        assumptions.append(
            f"Surface par defaut appliquee ({int(surface_value)} m2) selon type de bien."
        )

    return {
        "project_type_label": PROJECT_TYPE_LABELS.get(project_key, project_key),
        "scope_label": SMART_SCOPE_LABELS.get(scope_key, scope_key),
        "surface_m2": round(surface_value, 1),
        "rooms": room_count or None,
        "confidence": confidence,
        "low": low,
        "high": high,
        "low_label": _format_eur(low),
        "high_label": _format_eur(high),
        "budget_fit": budget_fit,
        "duration_weeks": {"min": duration_min, "max": duration_max},
        "breakdown": breakdown,
        "assumptions": assumptions,
    }


def _build_interior_offer(
    project_type: str,
    style: str,
    scope: str,
    surface_m2: float,
    room_count: int | None,
    notes: str,
    enabled: bool,
) -> dict:
    if not enabled:
        return {
            "enabled": False,
            "title": "Pack agencement et deco IA",
            "message": "Option non activee par le client.",
        }

    project_key = project_type if project_type in INTERIOR_ZONE_TEMPLATES else "maison"
    style_key = style if style in INTERIOR_STYLE_PROFILES else "moderne"
    profile = INTERIOR_STYLE_PROFILES[style_key]

    raw_zones = INTERIOR_ZONE_TEMPLATES[project_key]
    zones = []
    for name, ratio, intent in raw_zones:
        approx_surface = max(4, int(round(surface_m2 * ratio)))
        zones.append(
            {
                "name": name,
                "surface": f"{approx_surface} m2",
                "intent": intent,
            }
        )

    service_steps = [
        "Zonage optimise avec circulation et rangements.",
        "Palette couleurs + materiaux compatibles au style choisi.",
        "Selection mobilier prioritaire pour le confort quotidien.",
        "Checklist de mise en oeuvre transmise avec le devis final.",
    ]

    summary = (
        f"Proposition gratuite d'agencement pour un(e) {PROJECT_TYPE_LABELS.get(project_key, project_key).lower()} "
        f"en style {style_key}, adaptee a {int(round(surface_m2))} m2."
    )
    if room_count:
        summary += f" Scenario base sur {room_count} piece(s) declaree(s)."
    if notes and len(notes.strip()) >= 20:
        summary += " Les contraintes clients decrites sont integrees au concept."

    return {
        "enabled": True,
        "title": "Prestation gratuite agencement interieur + deco IA",
        "summary": summary,
        "scope_label": SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"]),
        "palette": profile["palette"],
        "materials": profile["materials"],
        "furniture_focus": profile["furniture_focus"],
        "zones": zones,
        "service_steps": service_steps,
    }


def _build_precall_report(
    project_type: str,
    scope: str,
    style: str,
    timeline: str,
    city: str,
    surface: str,
    rooms: str,
    budget: str,
    notes: str,
    quote: dict,
    has_contact: bool,
    interior_request_status: str,
    mode: str,
    photo_count: int,
) -> dict:
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "Projet")
    scope_label = SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"])
    style_label = (style or "moderne").capitalize()
    timeline_map = {
        "urgent": "Urgent",
        "3_mois": "Sous 3 mois",
        "6_mois": "Sous 6 mois",
        "flexible": "Flexible",
    }
    timeline_label = timeline_map.get(timeline, "A confirmer")

    surface_value = _parse_number(surface)
    room_value = int(_parse_number(rooms) or 0)
    budget_value = _parse_number(budget)
    budget_fit = (quote or {}).get("budget_fit", {}) or {}
    confidence = int(round(float((quote or {}).get("confidence", 0.55)) * 100))

    advantages: list[str] = []
    inconvenients: list[str] = []
    points_to_validate: list[str] = []
    call_plan: list[str] = []

    if city:
        advantages.append(f"Localisation renseignee ({city}), intervention possible en priorite.")
    else:
        inconvenients.append("Ville non confirmee, validation geographique a faire des le debut de l'appel.")
        points_to_validate.append("Confirmer la ville exacte du chantier.")

    if surface_value:
        advantages.append(f"Surface declaree ({int(round(surface_value))} m2), chiffrage plus fiable.")
    else:
        inconvenients.append("Surface non renseignee, fourchette budget encore trop large.")
        points_to_validate.append("Confirmer la surface exacte en m2.")

    if room_value > 0:
        advantages.append(f"Nombre de pieces renseigne ({room_value}), meilleur cadrage des lots.")
    else:
        points_to_validate.append("Confirmer le nombre de pieces impactees.")

    if budget_value:
        advantages.append(f"Budget client declare ({_format_eur(int(round(budget_value)))}).")
    else:
        inconvenients.append("Budget non renseigne, risque de decalage entre ambition et enveloppe financiere.")
        points_to_validate.append("Fixer une enveloppe budgetaire cible.")

    if timeline in {"urgent", "3_mois"}:
        inconvenients.append("Delai serre, necessite arbitrages rapides et planning de lots strict.")
        points_to_validate.append("Verifier les contraintes de delai incompressibles.")
    elif timeline == "flexible":
        advantages.append("Echeance flexible, possibilite d'optimiser couts et sequencing chantier.")
    else:
        advantages.append("Echeance standard compatible avec un pilotage qualite.")

    if mode == "ai":
        advantages.append("Projection 3D personnalisee disponible pour accelerer la decision client.")
    elif mode == "photo_preview":
        inconvenients.append("Projection IA 3D personnalisee non encore disponible, previsualisation basee sur photos.")
        points_to_validate.append("Proposer un rendu IA personnalise en phase suivante.")
    else:
        inconvenients.append("Projection en mode previsualisation, moins impactante qu'un rendu IA finalise.")

    if photo_count >= 3:
        advantages.append("Jeu photo complet (plusieurs angles), bonne base pour qualifier le chantier.")
    else:
        inconvenients.append("Photos limitees, certains points techniques peuvent rester invisibles.")
        points_to_validate.append("Demander 1 a 2 photos complementaires (angle opposé / points techniques).")

    if notes and len(notes.strip()) >= 30:
        advantages.append("Besoins client explicites dans le brief, gain de temps en qualification.")
    else:
        points_to_validate.append("Clarifier objectifs, niveau de finition et priorites client.")

    if interior_request_status == "requested":
        advantages.append("Client deja engage sur la prestation offerte IA 3D apres devis.")
    elif interior_request_status == "contact_required":
        inconvenients.append("Demande IA 3D initiee mais contact incomplet pour finaliser.")

    if not has_contact:
        inconvenients.append("Coordonnees non completees, risque de non-transformation apres simulation.")
        points_to_validate.append("Obtenir telephone et email valides en fin d'appel.")

    if budget_fit.get("status") == "under_budget":
        inconvenients.append("Budget potentiellement sous-dimensionne vs perimetre demande.")
        points_to_validate.append("Arbitrer priorites: travaux indispensables vs options.")
    elif budget_fit.get("status") == "aligned":
        advantages.append("Budget coherent avec la fourchette estimee.")
    elif budget_fit.get("status") == "over_budget":
        advantages.append("Marge budgetaire permettant une finition premium.")

    if len(points_to_validate) < 3:
        fallback_points = [
            "Verifier acces chantier (etage, stationnement, copropriete).",
            "Valider contraintes techniques deja connues (reseaux, humidite, electricite).",
            "Confirmer date cible de demarrage et disponibilite client.",
        ]
        for point in fallback_points:
            if point not in points_to_validate:
                points_to_validate.append(point)
            if len(points_to_validate) >= 4:
                break

    call_plan.extend(
        [
            "1) Requalifier le besoin client en 2 minutes (objectif, priorite, urgence).",
            "2) Verifier faisabilite terrain (surface, acces, contraintes techniques).",
            "3) Aligner budget et niveau de finition puis proposer scenario de travaux.",
            "4) Valider prochaine action: visite technique + devis detaille + projection IA.",
        ]
    )

    conversion_score = 50
    conversion_score += min(12, confidence // 8)
    conversion_score += 8 if has_contact else -8
    conversion_score += 6 if city else -4
    conversion_score += 6 if surface_value else -6
    conversion_score += 5 if budget_value else -6
    conversion_score += 6 if interior_request_status == "requested" else 0
    conversion_score += 4 if mode == "ai" else -2
    conversion_score -= min(10, max(0, len(inconvenients) - 2) * 2)
    conversion_score = max(35, min(95, conversion_score))

    if conversion_score >= 78:
        conversion_band = "Fort potentiel de signature"
    elif conversion_score >= 62:
        conversion_band = "Potentiel de signature solide"
    else:
        conversion_band = "Potentiel a consolider pendant l'appel"

    summary = (
        f"{project_label} • {scope_label} • style {style_label}. "
        f"Priorite appel: lever les derniers freins pour enclencher visite et devis final."
    )

    return {
        "title": "Compte rendu avant rendez-vous telephonique",
        "summary": summary,
        "conversion_score": conversion_score,
        "conversion_band": conversion_band,
        "snapshot": {
            "project_type": project_label,
            "scope": scope_label,
            "style": style_label,
            "timeline": timeline_label,
            "city": city or "A confirmer",
            "surface": f"{int(round(surface_value))} m2" if surface_value else "A confirmer",
            "budget": _format_eur(int(round(budget_value))) if budget_value else "A confirmer",
            "contact_ready": "Oui" if has_contact else "Non",
        },
        "advantages": advantages[:6],
        "inconvenients": inconvenients[:6],
        "points_to_validate": points_to_validate[:6],
        "call_plan": call_plan,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    ARCHITECTURE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ARCHITECTURE_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(intelligence_router)
app.include_router(saas_ai_router)


@app.middleware("http")
async def visitor_cookie_middleware(request: Request, call_next):
    visitor_id = _normalize_visitor_id(request.cookies.get(VISITOR_COOKIE_NAME))
    cookie_missing = visitor_id is None
    if cookie_missing:
        visitor_id = uuid.uuid4().hex

    request.state.visitor_id = visitor_id
    response = await call_next(request)

    if cookie_missing:
        response.set_cookie(
            VISITOR_COOKIE_NAME,
            visitor_id,
            max_age=VISITOR_COOKIE_MAX_AGE,
            path="/",
            secure=(request.url.scheme == "https"),
            httponly=False,
            samesite="lax",
        )
    return response


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["AGENDA_URL"] = AGENDA_URL
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

with open(CONTENT_DIR / "services.json", encoding="utf-8") as f:
    services = json.load(f)

with open(CONTENT_DIR / "cities.json", encoding="utf-8") as f:
    legacy_cities = json.load(f)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "services": services,
            "idf_departments": len(IDF_SECTORS),
        },
    )


@app.get("/services", response_class=HTMLResponse)
def list_services(request: Request):
    return templates.TemplateResponse(
        request, "list_services.html", {"services": services}
    )


@app.get("/services/{slug}", response_class=HTMLResponse)
def service_page(request: Request, slug: str):
    service = next((s for s in services if s["slug"] == slug), None)
    return templates.TemplateResponse(
        request, "service.html", {"service": service}
    )


@app.get("/zones", response_class=HTMLResponse)
def list_cities(request: Request):
    total_sector_cities = sum(len(s["cities"]) for s in IDF_SECTORS)
    return templates.TemplateResponse(
        request,
        "list_cities.html",
        {
            "sectors": IDF_SECTORS,
            "total_sector_cities": total_sector_cities,
            "legacy_cities_count": len(legacy_cities),
        },
    )


@app.get("/zones/{city}", response_class=HTMLResponse)
def city_page(request: Request, city: str):
    sector = next((s for s in IDF_SECTORS if city in s["cities"]), None)
    return templates.TemplateResponse(
        request, "city.html", {"city": city, "sector": sector}
    )


@app.get("/architecture-ia")
def architecture_ai_page():
    return RedirectResponse("/#architecture-ia", status_code=307)


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(request, "contact.html")


@app.post("/api/lead")
def lead(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    message: str = Form(...),
    visitor_id: str = Form(""),
    visitor_landing: str = Form(""),
    visitor_referrer: str = Form(""),
    visitor_utm: str = Form(""),
):
    tracking_context = _extract_tracking_context(
        request,
        {
            "visitor_id": visitor_id,
            "visitor_landing": visitor_landing,
            "visitor_referrer": visitor_referrer,
            "visitor_utm": visitor_utm,
        },
    )
    insert_lead(
        name,
        phone,
        email,
        message,
        meta=json.dumps({"source": "contact_form", "tracking": tracking_context}, ensure_ascii=False),
    )
    return RedirectResponse("/contact", status_code=303)


@app.post("/api/chat")
async def chat(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    agent_name, agent_role = _resolve_chat_agent(
        str(data.get("agent_name", "")),
        str(data.get("agent_role", "")),
    )
    user_messages = [m.get("content", "") for m in messages if m.get("role") == "user"]
    last = user_messages[-1] if user_messages else ""
    full_text = " ".join(user_messages)
    previous_text = " ".join(user_messages[:-1]) if len(user_messages) > 1 else ""
    is_first_turn = len(user_messages) <= 1
    context = _extract_chat_context(user_messages)

    est = estimate_from_text(last)
    if est.get("confidence", 0) <= 0.5 and full_text:
        est = estimate_from_text(full_text)

    contact_detected = has_contact_info(full_text)
    contact_just_provided = has_contact_info(last) and not has_contact_info(previous_text)
    work_type = context.get("work_type")
    surface_m2 = context.get("surface_m2")
    city_hint = context.get("city_hint")
    budget_hint = context.get("budget_hint")
    timeline_hint = context.get("timeline_hint")
    client_mood = context.get("client_mood", "neutral")
    missing_fields: list[str] = []
    if not work_type:
        missing_fields.append("type de travaux")
    if not surface_m2:
        missing_fields.append("surface en m2")
    if not city_hint:
        missing_fields.append("ville")
    if budget_hint is None:
        missing_fields.append("budget cible")
    if not timeline_hint:
        missing_fields.append("echeance souhaitee")

    estimate = None
    if est.get("confidence", 0) > 0.5:
        estimate = {"min": str(est.get("low", "")), "max": str(est.get("high", ""))}

    suggest_handoff = wants_human_help(full_text)

    if _is_schedule_intent(last):
        context_bits: list[str] = []
        if work_type:
            context_bits.append(str(work_type))
        if surface_m2:
            context_bits.append(f"{int(round(float(surface_m2)))} m2")
        if city_hint:
            context_bits.append(str(city_hint))
        context_line = (
            f"Je transmets deja le contexte ({', '.join(context_bits)}). "
            if context_bits
            else ""
        )
        if not contact_detected:
            return JSONResponse(
                {
                    "reply": (
                        "Je peux valider votre creneau, aucun souci. "
                        f"{context_line}Donnez-moi juste votre telephone ou email pour confirmer le RDV."
                    ),
                    "estimate": estimate,
                    "hybrid": {
                        "suggest_handoff": True,
                        "contact_detected": contact_detected,
                    },
                }
            )

        return JSONResponse(
            {
                "reply": (
                    "Parfait, creneau bien pris en compte. "
                    f"{context_line}Un conseiller renovation vous contacte rapidement pour confirmation."
                ),
                "estimate": estimate,
                "hybrid": {
                    "suggest_handoff": True,
                    "contact_detected": contact_detected,
                },
            }
        )

    if _is_ack_message(last):
        return JSONResponse(
            {
                "reply": _build_contextual_short_reply(
                    agent_name=agent_name,
                    contact_detected=contact_detected,
                    work_type=work_type,
                    surface_m2=surface_m2,
                    city_hint=city_hint,
                    client_mood=client_mood,
                    budget_hint=budget_hint,
                    timeline_hint=timeline_hint,
                    estimate=estimate,
                ),
                "estimate": estimate,
                "hybrid": {
                    "suggest_handoff": suggest_handoff,
                    "contact_detected": contact_detected,
                },
            }
        )

    reply = _build_professional_chat_reply(
        agent_name=agent_name,
        agent_role=agent_role,
        estimate=estimate,
        contact_detected=contact_detected,
        contact_just_provided=contact_just_provided,
        is_first_turn=is_first_turn,
        work_type=work_type,
        missing_fields=missing_fields,
        surface_m2=surface_m2,
        city_hint=city_hint,
        client_mood=client_mood,
        budget_hint=budget_hint,
        timeline_hint=timeline_hint,
    )

    allow_openai_chat = os.getenv("ENABLE_OPENAI_CHAT", "").strip().lower() in {"1", "true", "yes", "on"}
    if allow_openai_chat:
        ai_reply = _generate_chat_reply_with_openai(
            messages=messages,
            agent_name=agent_name,
            agent_role=agent_role,
            estimate=estimate,
            contact_detected=contact_detected,
            is_first_turn=is_first_turn,
            work_type=work_type,
            surface_m2=surface_m2,
            city_hint=city_hint,
            budget_hint=budget_hint,
            timeline_hint=timeline_hint,
            client_mood=client_mood,
            suggest_handoff=suggest_handoff,
        )
        if ai_reply and _is_professional_reply(ai_reply) and _is_human_tone_reply(ai_reply, is_first_turn):
            reply = ai_reply

    last_assistant = ""
    for message in reversed(messages):
        if (message or {}).get("role") == "assistant":
            last_assistant = str((message or {}).get("content", "")).strip()
            break
    if last_assistant and reply.strip() == last_assistant:
        if missing_fields:
            reply = (
                f"Je garde bien votre contexte chantier. "
                f"Pour avancer concretement, il me manque juste: {', '.join(missing_fields[:2])}."
            )
        else:
            reply = (
                "Je garde bien votre contexte chantier. "
                "On peut maintenant verrouiller un creneau pour la visite technique et le devis detaille."
            )

    return JSONResponse(
        {
            "reply": reply,
            "estimate": estimate,
            "hybrid": {
                "suggest_handoff": suggest_handoff,
                "contact_detected": contact_detected,
            },
        }
    )


@app.post("/api/devis-intelligent")
async def devis_intelligent(
    request: Request,
    project_type: str = Form(...),
    style: str = Form(...),
    scope: str = Form("renovation_complete"),
    timeline: str = Form("6_mois"),
    city: str = Form(""),
    surface: str = Form(""),
    rooms: str = Form(""),
    budget: str = Form(""),
    notes: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    visitor_id: str = Form(""),
    visitor_landing: str = Form(""),
    visitor_referrer: str = Form(""),
    visitor_utm: str = Form(""),
):
    project_key = project_type if project_type in PROJECT_TYPE_LABELS else "maison"
    style_key = style if style in STYLE_MULTIPLIER else "moderne"
    scope_key = scope if scope in SMART_SCOPE_CONFIG else "renovation_complete"
    timeline_key = timeline if timeline in TIMELINE_COST_MULTIPLIER else "6_mois"

    client_email = (email or "").strip()
    if not client_email:
        return JSONResponse(
            {
                "ok": False,
                "error": "Renseignez l'email du client pour recevoir le devis intelligent.",
            },
            status_code=400,
        )

    smtp_cfg = _smtp_settings()
    if not _smtp_ready(smtp_cfg):
        smtp_host = (smtp_cfg.get("host") or "").strip()
        smtp_user = (smtp_cfg.get("user") or "").strip()
        provider_hint = "Gmail" if "gmail" in smtp_host.lower() or "gmail" in smtp_user.lower() else "SMTP"
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Envoi email indisponible: configuration {provider_hint} incomplete. "
                    "Renseignez SMTP_HOST, SMTP_USER, SMTP_FROM_EMAIL et surtout SMTP_PASSWORD."
                ),
                "setup_steps": [
                    "Lancez ./run_gmail.sh si vous utilisez divclass72@gmail.com.",
                    "Saisissez le mot de passe d'application Gmail (16 caracteres).",
                    "Relancez l'envoi du devis intelligent.",
                ],
            },
            status_code=503,
        )

    internal_recipient = (INTERNAL_REPORT_EMAIL or smtp_cfg.get("from_email") or "").strip()
    if not internal_recipient:
        return JSONResponse(
            {
                "ok": False,
                "error": "Destinataire interne indisponible. Configurez INTERNAL_REPORT_EMAIL.",
            },
            status_code=503,
        )

    tracking_context = _extract_tracking_context(
        request,
        {
            "visitor_id": visitor_id,
            "visitor_landing": visitor_landing,
            "visitor_referrer": visitor_referrer,
            "visitor_utm": visitor_utm,
        },
    )

    quote = _build_smart_quote(
        project_type=project_key,
        style=style_key,
        scope=scope_key,
        timeline=timeline_key,
        surface=surface,
        rooms=rooms,
        budget=budget,
        city=city,
        notes=notes,
    )

    has_contact = bool((phone or "").strip() or client_email)
    precall_report = _build_precall_report(
        project_type=project_key,
        scope=scope_key,
        style=style_key,
        timeline=timeline_key,
        city=city,
        surface=surface,
        rooms=rooms,
        budget=budget,
        notes=notes,
        quote=quote,
        has_contact=has_contact,
        interior_request_status="pending_render_request",
        mode="devis_only",
        photo_count=0,
    )

    handoff_id = None
    if has_contact:
        db = SessionLocal()
        try:
            handoff = HandoffRequest(
                status="new",
                priority="high",
                source="devis_intelligent",
                name=name or None,
                phone=phone or None,
                email=client_email or None,
                city=city or None,
                postal_code=None,
                work_type=f"devis:{project_key}/{style_key}",
                surface=surface or None,
                estimate_min=str(quote["low"]),
                estimate_max=str(quote["high"]),
                reason="devis intelligent pose - rendu 3d sur demande",
                ip_address=(request.client.host if request.client else None),
                conversation=json.dumps(
                    {
                        "stage": "quote_sent",
                        "notes": notes,
                        "project_type": project_key,
                        "style": style_key,
                        "scope": scope_key,
                        "timeline": timeline_key,
                        "city": city,
                        "surface": surface,
                        "rooms": rooms,
                        "budget": budget,
                        "quote": quote,
                        "precall_report": precall_report,
                        "render_request_status": "awaiting_request",
                        "tracking": tracking_context,
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(handoff)
            db.commit()
            db.refresh(handoff)
            handoff_id = handoff.id
        finally:
            db.close()

    client_subject, client_body = _compose_client_devis_email(
        name=name,
        city=city,
        project_type=project_key,
        scope=scope_key,
        style=style_key,
        quote=quote,
    )
    client_email_sent, client_error = _send_email_message(
        to_email=client_email,
        subject=client_subject,
        text_body=client_body,
    )

    internal_subject, internal_body = _compose_internal_report_email(
        name=name,
        phone=phone,
        email=client_email,
        city=city,
        project_type=project_key,
        scope=scope_key,
        style=style_key,
        timeline=timeline_key,
        surface=surface,
        rooms=rooms,
        budget=budget,
        notes=notes,
        quote=quote,
        interior_request_status="pending_render_request",
        precall_report=precall_report,
        source_photos=[],
        renders=[],
        mode="devis_only",
        tracking_context=tracking_context,
        client_quote_subject=client_subject,
        client_quote_body=client_body,
    )
    internal_email_sent, internal_error = _send_email_message(
        to_email=internal_recipient,
        subject=internal_subject,
        text_body=internal_body,
    )

    if (not client_email_sent) or (not internal_email_sent):
        failures: list[str] = []
        if not client_email_sent:
            failures.append("Le devis intelligent n'a pas pu etre envoye au client.")
        if not internal_email_sent:
            failures.append("La copie interne du devis n'a pas pu etre envoyee.")
        return JSONResponse(
            {
                "ok": False,
                "error": " ".join(failures),
                "delivery": {
                    "client_email": client_email,
                    "client_email_sent": client_email_sent,
                    "client_error": client_error,
                    "internal_email": "equipe interne",
                    "internal_email_sent": internal_email_sent,
                    "internal_error": internal_error,
                },
                "quote": quote,
                "handoff_id": handoff_id,
            },
            status_code=502,
        )

    return {
        "ok": True,
        "message": "Devis intelligent envoye au client. Le rendu 3D est disponible sur demande apres devis.",
        "quote": quote,
        "handoff_id": handoff_id,
        "render_request_enabled": True,
        "delivery": {
            "client_email": client_email,
            "client_email_sent": client_email_sent,
            "internal_email": "equipe interne",
            "internal_email_sent": internal_email_sent,
        },
        "prefill": {
            "project_type": project_key,
            "style": style_key,
            "scope": scope_key,
            "timeline": timeline_key,
            "city": city,
            "surface": surface,
            "rooms": rooms,
            "budget": budget,
            "name": name,
            "phone": phone,
            "email": client_email,
            "notes": notes,
        },
    }


@app.post("/api/rendu-3d-sur-demande")
async def render_3d_on_demand(
    request: Request,
    handoff_id: str = Form(""),
    project_type: str = Form("maison"),
    style: str = Form("moderne"),
    scope: str = Form("renovation_complete"),
    timeline: str = Form("6_mois"),
    city: str = Form(""),
    surface: str = Form(""),
    rooms: str = Form(""),
    budget: str = Form(""),
    notes: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    building_photo_confirmed: str = Form(""),
    visitor_id: str = Form(""),
    visitor_landing: str = Form(""),
    visitor_referrer: str = Form(""),
    visitor_utm: str = Form(""),
    photos: list[UploadFile] = File(...),
):
    if not _as_bool(building_photo_confirmed):
        return JSONResponse(
            {
                "ok": False,
                "error": "Confirmez que les photos concernent bien le batiment a renover.",
            },
            status_code=400,
        )

    if not photos:
        return JSONResponse({"ok": False, "error": "Ajoutez au moins une photo."}, status_code=400)

    photo_urls: list[str] = []
    photo_inputs: list[tuple[str, bytes]] = []
    seen_hashes: set[str] = set()
    max_photos = 10
    for upload in photos[:max_photos]:
        content = await upload.read()
        if not content:
            continue
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)

        suffix = _safe_suffix(upload.filename or "")
        filename = f"{_utc_file_stamp()}_{uuid.uuid4().hex[:10]}{suffix}"
        dst = ARCHITECTURE_UPLOAD_DIR / filename
        dst.write_bytes(content)
        photo_urls.append(_public_static_url(dst))
        photo_inputs.append((upload.filename or filename, content))

    if not photo_urls:
        return JSONResponse(
            {
                "ok": False,
                "error": "Les photos envoyees sont en double ou invalides. Ajoutez des photos batiment differentes.",
            },
            status_code=400,
        )

    smtp_cfg = _smtp_settings()
    if not _smtp_ready(smtp_cfg):
        smtp_host = (smtp_cfg.get("host") or "").strip()
        smtp_user = (smtp_cfg.get("user") or "").strip()
        provider_hint = "Gmail" if "gmail" in smtp_host.lower() or "gmail" in smtp_user.lower() else "SMTP"
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Envoi email indisponible: configuration {provider_hint} incomplete. "
                    "Renseignez SMTP_HOST, SMTP_USER, SMTP_FROM_EMAIL et surtout SMTP_PASSWORD."
                ),
                "setup_steps": [
                    "Lancez ./run_gmail.sh si vous utilisez divclass72@gmail.com.",
                    "Saisissez le mot de passe d'application Gmail (16 caracteres).",
                    "Relancez ensuite la demande de rendu 3D.",
                ],
            },
            status_code=503,
        )

    internal_recipient = (INTERNAL_REPORT_EMAIL or smtp_cfg.get("from_email") or "").strip()
    if not internal_recipient:
        return JSONResponse(
            {
                "ok": False,
                "error": "Destinataire interne indisponible. Configurez INTERNAL_REPORT_EMAIL.",
            },
            status_code=503,
        )

    if not _openai_key_ready():
        return JSONResponse(
            {
                "ok": False,
                "error": "Generation 3D IA indisponible: OPENAI_API_KEY manquant.",
                "setup_steps": [
                    "Ajoutez une cle OPENAI_API_KEY valide (pas un placeholder).",
                    "Relancez ensuite la demande de rendu 3D.",
                ],
            },
            status_code=503,
        )

    tracking_context = _extract_tracking_context(
        request,
        {
            "visitor_id": visitor_id,
            "visitor_landing": visitor_landing,
            "visitor_referrer": visitor_referrer,
            "visitor_utm": visitor_utm,
        },
    )

    handoff_ref = int(_parse_number(handoff_id) or 0)
    db = SessionLocal()
    try:
        handoff = None
        handoff_payload: dict = {}
        if handoff_ref > 0:
            handoff = db.query(HandoffRequest).filter(HandoffRequest.id == handoff_ref).first()
            if not handoff:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"Dossier devis #{handoff_ref} introuvable. Posez d'abord un devis ou corrigez l'identifiant.",
                    },
                    status_code=404,
                )
            handoff_payload = _extract_handoff_conversation_payload(handoff)

        project_key = (project_type or handoff_payload.get("project_type") or "maison").strip().lower()
        if project_key not in PROJECT_TYPE_LABELS:
            project_key = "maison"
        style_key = (style or handoff_payload.get("style") or "moderne").strip().lower()
        if style_key not in STYLE_MULTIPLIER:
            style_key = "moderne"
        scope_key = (scope or handoff_payload.get("scope") or "renovation_complete").strip().lower()
        if scope_key not in SMART_SCOPE_CONFIG:
            scope_key = "renovation_complete"
        timeline_key = (timeline or handoff_payload.get("timeline") or "6_mois").strip()
        if timeline_key not in TIMELINE_COST_MULTIPLIER:
            timeline_key = "6_mois"

        resolved_city = (city or handoff_payload.get("city") or (handoff.city if handoff else "") or "").strip()
        resolved_surface = (surface or handoff_payload.get("surface") or (handoff.surface if handoff else "") or "").strip()
        resolved_rooms = (rooms or handoff_payload.get("rooms") or "").strip()
        resolved_budget = (budget or handoff_payload.get("budget") or "").strip()
        resolved_notes = (notes or handoff_payload.get("notes") or "").strip()
        resolved_name = (name or (handoff.name if handoff else "") or "").strip()
        resolved_phone = (phone or (handoff.phone if handoff else "") or "").strip()
        resolved_email = (email or (handoff.email if handoff else "") or "").strip()

        if not resolved_email:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Renseignez l'email du client pour envoyer le rendu 3D.",
                },
                status_code=400,
            )

        stored_quote = handoff_payload.get("quote")
        if not isinstance(stored_quote, dict) or not stored_quote.get("low_label") or not stored_quote.get("high_label"):
            stored_quote = None
        quote = stored_quote or _build_smart_quote(
            project_type=project_key,
            style=style_key,
            scope=scope_key,
            timeline=timeline_key,
            surface=resolved_surface,
            rooms=resolved_rooms,
            budget=resolved_budget,
            city=resolved_city,
            notes=resolved_notes,
        )

        prompt = _compose_architecture_prompt(
            project_type=project_key,
            style=style_key,
            city=resolved_city,
            surface=resolved_surface,
            notes=resolved_notes,
            scope=scope_key,
            timeline=timeline_key,
            want_free_interior=True,
        )
        render_urls: list[str] = []
        render_errors: list[str] = []
        for idx in range(2):
            img_bytes, err = _generate_render_with_openai(
                f"{prompt} | variation {idx + 1}",
                reference_images=photo_inputs,
            )
            if img_bytes:
                filename = f"render_{_utc_file_stamp()}_{uuid.uuid4().hex[:10]}.png"
                dst = ARCHITECTURE_RENDER_DIR / filename
                dst.write_bytes(img_bytes)
                render_urls.append(_public_static_url(dst))
            elif err:
                render_errors.append(err[:220])

        if not render_urls:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Impossible de generer un rendu 3D IA pour le moment. Reessayez dans quelques minutes.",
                    "details": render_errors[:2],
                },
                status_code=502,
            )

        has_contact = bool(resolved_phone or resolved_email)
        precall_report = _build_precall_report(
            project_type=project_key,
            scope=scope_key,
            style=style_key,
            timeline=timeline_key,
            city=resolved_city,
            surface=resolved_surface,
            rooms=resolved_rooms,
            budget=resolved_budget,
            notes=resolved_notes,
            quote=quote,
            has_contact=has_contact,
            interior_request_status="requested",
            mode="ai",
            photo_count=len(photo_urls),
        )

        handoff_id_result = None
        if handoff:
            merged_payload = dict(handoff_payload)
            merged_payload.update(
                {
                    "stage": "render_requested",
                    "project_type": project_key,
                    "style": style_key,
                    "scope": scope_key,
                    "timeline": timeline_key,
                    "city": resolved_city,
                    "surface": resolved_surface,
                    "rooms": resolved_rooms,
                    "budget": resolved_budget,
                    "notes": resolved_notes,
                    "quote": quote,
                    "precall_report": precall_report,
                    "source_photos": photo_urls,
                    "renders": render_urls,
                    "render_request_status": "done",
                    "tracking_render_request": tracking_context,
                }
            )
            handoff.reason = "rendu 3d demande apres devis"
            handoff.name = resolved_name or handoff.name
            handoff.phone = resolved_phone or handoff.phone
            handoff.email = resolved_email or handoff.email
            handoff.city = resolved_city or handoff.city
            handoff.surface = resolved_surface or handoff.surface
            handoff.estimate_min = str(quote["low"])
            handoff.estimate_max = str(quote["high"])
            handoff.work_type = f"architecture-3d:{project_key}/{style_key}"
            handoff.conversation = json.dumps(merged_payload, ensure_ascii=False)
            db.commit()
            handoff_id_result = handoff.id
        elif has_contact:
            new_handoff = HandoffRequest(
                status="new",
                priority="high",
                source="render_request",
                name=resolved_name or None,
                phone=resolved_phone or None,
                email=resolved_email or None,
                city=resolved_city or None,
                postal_code=None,
                work_type=f"architecture-3d:{project_key}/{style_key}",
                surface=resolved_surface or None,
                estimate_min=str(quote["low"]),
                estimate_max=str(quote["high"]),
                reason="rendu 3d demande apres devis",
                ip_address=(request.client.host if request.client else None),
                conversation=json.dumps(
                    {
                        "stage": "render_requested",
                        "project_type": project_key,
                        "style": style_key,
                        "scope": scope_key,
                        "timeline": timeline_key,
                        "city": resolved_city,
                        "surface": resolved_surface,
                        "rooms": resolved_rooms,
                        "budget": resolved_budget,
                        "notes": resolved_notes,
                        "quote": quote,
                        "precall_report": precall_report,
                        "source_photos": photo_urls,
                        "renders": render_urls,
                        "render_request_status": "done",
                        "tracking": tracking_context,
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(new_handoff)
            db.commit()
            db.refresh(new_handoff)
            handoff_id_result = new_handoff.id

        client_subject, client_body = _compose_client_render_email(
            name=resolved_name,
            city=resolved_city,
            project_type=project_key,
            scope=scope_key,
            style=style_key,
            quote=quote,
            renders=render_urls,
            source_photos=photo_urls,
        )
        client_email_sent, client_error = _send_email_message(
            to_email=resolved_email,
            subject=client_subject,
            text_body=client_body,
        )

        internal_subject, internal_body = _compose_internal_report_email(
            name=resolved_name,
            phone=resolved_phone,
            email=resolved_email,
            city=resolved_city,
            project_type=project_key,
            scope=scope_key,
            style=style_key,
            timeline=timeline_key,
            surface=resolved_surface,
            rooms=resolved_rooms,
            budget=resolved_budget,
            notes=resolved_notes,
            quote=quote,
            interior_request_status="requested",
            precall_report=precall_report,
            source_photos=photo_urls,
            renders=render_urls,
            mode="ai",
            tracking_context=tracking_context,
            client_quote_subject=client_subject,
            client_quote_body=client_body,
        )
        internal_email_sent, internal_error = _send_email_message(
            to_email=internal_recipient,
            subject=internal_subject,
            text_body=internal_body,
        )

        if (not client_email_sent) or (not internal_email_sent):
            failures: list[str] = []
            if not client_email_sent:
                failures.append("Le rendu 3D n'a pas pu etre envoye au client.")
            if not internal_email_sent:
                failures.append("La copie interne du rendu 3D n'a pas pu etre envoyee.")
            return JSONResponse(
                {
                    "ok": False,
                    "error": " ".join(failures),
                    "delivery": {
                        "client_email": resolved_email,
                        "client_email_sent": client_email_sent,
                        "client_error": client_error,
                        "internal_email": "equipe interne",
                        "internal_email_sent": internal_email_sent,
                        "internal_error": internal_error,
                    },
                    "source_photos": photo_urls,
                    "renders": render_urls,
                    "quote": quote,
                    "handoff_id": handoff_id_result,
                },
                status_code=502,
            )

        return {
            "ok": True,
            "mode": "ai",
            "message": "Rendu 3D sur demande genere et envoye au client. Copie interne envoyee.",
            "delivery": {
                "client_email": resolved_email,
                "client_email_sent": client_email_sent,
                "internal_email": "equipe interne",
                "internal_email_sent": internal_email_sent,
            },
            "quote": quote,
            "source_photos": photo_urls,
            "renders": render_urls,
            "handoff_id": handoff_id_result,
        }
    finally:
        db.close()


@app.post("/api/architecture-3d")
async def architecture_3d(
    request: Request,
    project_type: str = Form(...),
    style: str = Form(...),
    scope: str = Form("renovation_complete"),
    timeline: str = Form("6_mois"),
    city: str = Form(""),
    surface: str = Form(""),
    rooms: str = Form(""),
    budget: str = Form(""),
    want_free_interior: str = Form(""),
    building_photo_confirmed: str = Form(""),
    notes: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    visitor_id: str = Form(""),
    visitor_landing: str = Form(""),
    visitor_referrer: str = Form(""),
    visitor_utm: str = Form(""),
    photos: list[UploadFile] = File(...),
):
    if not _as_bool(building_photo_confirmed):
        return JSONResponse(
            {
                "ok": False,
                "error": "Confirmez que les photos concernent bien le batiment a renover.",
            },
            status_code=400,
        )

    if not photos:
        return JSONResponse({"ok": False, "error": "Ajoutez au moins une photo."}, status_code=400)

    photo_urls: list[str] = []
    photo_inputs: list[tuple[str, bytes]] = []
    seen_hashes: set[str] = set()
    for upload in photos[:6]:
        content = await upload.read()
        if not content:
            continue
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)

        suffix = _safe_suffix(upload.filename or "")
        filename = f"{_utc_file_stamp()}_{uuid.uuid4().hex[:10]}{suffix}"
        dst = ARCHITECTURE_UPLOAD_DIR / filename
        dst.write_bytes(content)
        photo_urls.append(_public_static_url(dst))
        photo_inputs.append((upload.filename or filename, content))

    if not photo_urls:
        return JSONResponse(
            {
                "ok": False,
                "error": "Les photos envoyees sont en double ou invalides. Ajoutez une photo du batiment differente.",
            },
            status_code=400,
        )

    client_email = (email or "").strip()
    if not client_email:
        return JSONResponse(
            {
                "ok": False,
                "error": "Renseignez l'email du client pour recevoir le devis intelligent.",
            },
            status_code=400,
        )

    smtp_cfg = _smtp_settings()
    if not _smtp_ready(smtp_cfg):
        smtp_host = (smtp_cfg.get("host") or "").strip()
        smtp_user = (smtp_cfg.get("user") or "").strip()
        provider_hint = "Gmail" if "gmail" in smtp_host.lower() or "gmail" in smtp_user.lower() else "SMTP"
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    f"Envoi email indisponible: configuration {provider_hint} incomplete. "
                    "Renseignez SMTP_HOST, SMTP_USER, SMTP_FROM_EMAIL et surtout SMTP_PASSWORD."
                ),
                "setup_steps": [
                    "Lancez ./run_gmail.sh si vous utilisez divclass72@gmail.com.",
                    "Saisissez le mot de passe d'application Gmail (16 caracteres).",
                    "Relancez la simulation devis + rendu 3D.",
                ],
            },
            status_code=503,
        )

    internal_recipient = (INTERNAL_REPORT_EMAIL or smtp_cfg.get("from_email") or "").strip()
    if not internal_recipient:
        return JSONResponse(
            {
                "ok": False,
                "error": "Destinataire interne indisponible. Configurez INTERNAL_REPORT_EMAIL.",
            },
            status_code=503,
        )

    if not _openai_key_ready():
        return JSONResponse(
            {
                "ok": False,
                "error": "Generation 3D IA indisponible: OPENAI_API_KEY manquant.",
                "setup_steps": [
                    "Ajoutez une cle OPENAI_API_KEY valide (pas un placeholder).",
                    "Relancez ensuite la simulation pour produire les rendus 3D.",
                ],
            },
            status_code=503,
        )

    tracking_context = _extract_tracking_context(
        request,
        {
            "visitor_id": visitor_id,
            "visitor_landing": visitor_landing,
            "visitor_referrer": visitor_referrer,
            "visitor_utm": visitor_utm,
        },
    )

    wants_free_pack = _as_bool(want_free_interior)
    has_contact = bool((phone or "").strip() or (email or "").strip())
    interior_request_status = "not_requested"
    enable_interior_offer = False
    if wants_free_pack and has_contact:
        interior_request_status = "requested"
        enable_interior_offer = True
    elif wants_free_pack and not has_contact:
        interior_request_status = "contact_required"

    quote = _build_smart_quote(
        project_type=project_type,
        style=style,
        scope=scope,
        timeline=timeline,
        surface=surface,
        rooms=rooms,
        budget=budget,
        city=city,
        notes=notes,
    )
    interior_offer = _build_interior_offer(
        project_type=project_type,
        style=style,
        scope=scope,
        surface_m2=quote["surface_m2"],
        room_count=quote.get("rooms"),
        notes=notes,
        enabled=enable_interior_offer,
    )

    prompt = _compose_architecture_prompt(
        project_type=project_type,
        style=style,
        city=city,
        surface=surface,
        notes=notes,
        scope=scope,
        timeline=timeline,
        want_free_interior=enable_interior_offer,
    )
    render_urls: list[str] = []
    render_errors: list[str] = []
    mode = "ai"

    for idx in range(2):
        img_bytes, err = _generate_render_with_openai(
            f"{prompt} | variation {idx + 1}",
            reference_images=photo_inputs,
        )
        if img_bytes:
            filename = f"render_{_utc_file_stamp()}_{uuid.uuid4().hex[:10]}.png"
            dst = ARCHITECTURE_RENDER_DIR / filename
            dst.write_bytes(img_bytes)
            render_urls.append(_public_static_url(dst))
        elif err:
            render_errors.append(err[:220])

    if not render_urls:
        return JSONResponse(
            {
                "ok": False,
                "error": "Impossible de generer un rendu 3D IA pour le moment. Reessayez dans quelques minutes.",
                "details": render_errors[:2],
            },
            status_code=502,
        )

    precall_report = _build_precall_report(
        project_type=project_type,
        scope=scope,
        style=style,
        timeline=timeline,
        city=city,
        surface=surface,
        rooms=rooms,
        budget=budget,
        notes=notes,
        quote=quote,
        has_contact=has_contact,
        interior_request_status=interior_request_status,
        mode=mode,
        photo_count=len(photo_urls),
    )

    handoff_id = None
    if has_contact:
        handoff_reason = "devis intelligent pose"
        if interior_request_status == "requested":
            handoff_reason = "demande prestation ia apres devis pose"

        db = SessionLocal()
        try:
            handoff = HandoffRequest(
                status="new",
                priority="high",
                source="architecture_ai",
                name=name or None,
                phone=phone or None,
                email=email or None,
                city=city or None,
                postal_code=None,
                work_type=f"architecture-3d:{project_type}/{style}",
                surface=surface or None,
                estimate_min=str(quote["low"]),
                estimate_max=str(quote["high"]),
                reason=handoff_reason,
                ip_address=(request.client.host if request.client else None),
                conversation=json.dumps(
                    {
                        "notes": notes,
                        "project_type": project_type,
                        "style": style,
                        "scope": scope,
                        "timeline": timeline,
                        "rooms": rooms,
                        "budget": budget,
                        "want_free_interior": wants_free_pack,
                        "interior_request_status": interior_request_status,
                        "quote": quote,
                        "interior_offer": interior_offer,
                        "precall_report": precall_report,
                        "source_photos": photo_urls,
                        "renders": render_urls,
                        "tracking": tracking_context,
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(handoff)
            db.commit()
            db.refresh(handoff)
            handoff_id = handoff.id
        finally:
            db.close()

    client_subject, client_body = _compose_client_quote_email(
        name=name,
        city=city,
        project_type=project_type,
        scope=scope,
        style=style,
        quote=quote,
        renders=render_urls,
        source_photos=photo_urls,
    )
    client_email_sent, client_error = _send_email_message(
        to_email=client_email,
        subject=client_subject,
        text_body=client_body,
    )
    internal_subject, internal_body = _compose_internal_report_email(
        name=name,
        phone=phone,
        email=client_email,
        city=city,
        project_type=project_type,
        scope=scope,
        style=style,
        timeline=timeline,
        surface=surface,
        rooms=rooms,
        budget=budget,
        notes=notes,
        quote=quote,
        interior_request_status=interior_request_status,
        precall_report=precall_report,
        source_photos=photo_urls,
        renders=render_urls,
        mode=mode,
        tracking_context=tracking_context,
        client_quote_subject=client_subject,
        client_quote_body=client_body,
    )
    internal_email_sent, internal_error = _send_email_message(
        to_email=internal_recipient,
        subject=internal_subject,
        text_body=internal_body,
    )

    if (not client_email_sent) or (not internal_email_sent):
        failures: list[str] = []
        if not client_email_sent:
            failures.append("Le devis + rendu 3D n'ont pas pu etre envoyes au client.")
        if not internal_email_sent:
            failures.append("La copie interne du devis n'a pas pu etre envoyee.")
        return JSONResponse(
            {
                "ok": False,
                "error": " ".join(failures),
                "delivery": {
                    "client_email": client_email,
                    "client_email_sent": client_email_sent,
                    "client_error": client_error,
                    "internal_email": "equipe interne",
                    "internal_email_sent": internal_email_sent,
                    "internal_error": internal_error,
                },
                "source_photos": photo_urls,
                "renders": render_urls,
                "handoff_id": handoff_id,
            },
            status_code=502,
        )

    message = "Devis intelligent et rendu 3D envoyes au client. Copie interne envoyee."
    if interior_request_status == "requested":
        message += " Demande de prestation offerte IA 3D transmise."
    elif interior_request_status == "contact_required":
        message += " Ajoutez telephone ou email pour valider la demande de prestation offerte IA 3D."

    return {
        "ok": True,
        "mode": mode,
        "message": message,
        "delivery": {
            "client_email": client_email,
            "client_email_sent": client_email_sent,
            "internal_email": "equipe interne",
            "internal_email_sent": internal_email_sent,
        },
        "interior_request_status": interior_request_status,
        "interior_request_allowed": has_contact,
        "source_photos": photo_urls,
        "renders": render_urls,
        "handoff_id": handoff_id,
    }


@app.post("/api/leads")
def create_lead(request: Request, payload: dict = Body(...)):
    tracking_context = _extract_tracking_context(request, payload)
    db = SessionLocal()
    try:
        lead_record = Lead(
            name=payload.get("name"),
            phone=payload.get("phone"),
            email=payload.get("email"),
            city=payload.get("city"),
            postal_code=payload.get("postal_code"),
            surface=payload.get("surface"),
            work_type=payload.get("work_type"),
            estimate_min=payload.get("estimate_min"),
            estimate_max=payload.get("estimate_max"),
            ip_address=(request.client.host if request.client else None),
            raw_message=_attach_tracking_to_raw(payload.get("raw_message"), tracking_context),
        )
        db.add(lead_record)
        db.commit()
        db.refresh(lead_record)
        return {"ok": True, "id": lead_record.id, "visitor_id": tracking_context.get("visitor_id")}
    finally:
        db.close()


@app.post("/api/handoff")
def create_handoff(request: Request, payload: dict = Body(...)):
    tracking_context = _extract_tracking_context(request, payload)
    db = SessionLocal()
    try:
        handoff = HandoffRequest(
            status="new",
            priority=payload.get("priority") or "high",
            source=payload.get("source") or "chat_widget",
            name=payload.get("name"),
            phone=payload.get("phone"),
            email=payload.get("email"),
            city=payload.get("city"),
            postal_code=payload.get("postal_code"),
            work_type=payload.get("work_type"),
            surface=payload.get("surface"),
            estimate_min=payload.get("estimate_min"),
            estimate_max=payload.get("estimate_max"),
            reason=payload.get("reason") or "demande conseiller humain",
            ip_address=(request.client.host if request.client else None),
            conversation=_attach_tracking_to_conversation(payload.get("conversation"), tracking_context),
        )
        db.add(handoff)
        db.commit()
        db.refresh(handoff)
        return {
            "ok": True,
            "id": handoff.id,
            "status": "new",
            "visitor_id": tracking_context.get("visitor_id"),
        }
    finally:
        db.close()


@app.get("/api/handoffs")
def list_handoffs(limit: int = 20):
    db = SessionLocal()
    try:
        rows = (
            db.query(HandoffRequest)
            .order_by(HandoffRequest.created_at.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        return {
            "items": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "status": r.status,
                    "priority": r.priority,
                    "phone": r.phone,
                    "email": r.email,
                    "city": r.city,
                    "work_type": r.work_type,
                    "estimate_min": r.estimate_min,
                    "estimate_max": r.estimate_max,
                    "reason": r.reason,
                }
                for r in rows
            ]
        }
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=10000, reload=True)
