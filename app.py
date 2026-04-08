# noinspection SpellCheckingInspection
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
from models import Base, ClientProject, HandoffRequest, Lead, PasswordResetToken, ProjectDocument, UserAccount, UserSession
from pricing import estimate_from_item_key, estimate_from_scope, estimate_from_text, get_tariff_item, has_required_quantity
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
ESTIMATE_UPLOAD_DIR = STATIC_DIR / "estimate" / "uploads"
CLIENT_DOCS_DIR = STATIC_DIR / "client-docs"
AGENDA_URL = os.getenv("AGENDA_URL", "").strip()
INTERNAL_REPORT_EMAIL = os.getenv("INTERNAL_REPORT_EMAIL", "celia.b@keythinkers.fr").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
LABOR_ONLY_MENTION = "Main-d'œuvre uniquement, hors matériaux et fournitures."
VISITOR_COOKIE_NAME = "rb_vid"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 180
SESSION_COOKIE_NAME = "rb_session"
SESSION_TTL_DAYS = 30
PASSWORD_RESET_TTL_HOURS = 2
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
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


def _as_utc(dt_value: datetime | None) -> datetime | None:
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=UTC)
    return dt_value


def _parse_project_summary(summary: str | None) -> dict:
    if not summary:
        return {}
    raw = summary.strip()
    if not raw:
        return {}
    if raw.startswith("{") and raw.endswith("}"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {"summary_text": raw}


def _is_recap_empty(recap: dict | None) -> bool:
    if not recap:
        return True
    keys = ["project_type", "scope", "surface", "city", "budget", "style", "timeline"]
    return not any(recap.get(k) for k in keys)


def _hash_password(raw_password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${derived.hex()}"


def _verify_password(raw_password: str, stored_hash: str) -> bool:
    if "$" not in stored_hash:
        return False
    salt_hex, hash_hex = stored_hash.split("$", 1)
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    derived = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, 200_000)
    return hmac.compare_digest(derived, expected)


def _create_session(db, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = _utc_now() + timedelta(days=SESSION_TTL_DAYS)
    db.add(UserSession(user_id=user_id, token=token, expires_at=expires_at))
    db.commit()
    return token


def _create_password_reset(db, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = _utc_now() + timedelta(hours=PASSWORD_RESET_TTL_HOURS)
    db.add(PasswordResetToken(user_id=user_id, token=token, expires_at=expires_at))
    db.commit()
    return token


def _consume_password_reset(db, token: str):
    reset = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token == token)
        .order_by(PasswordResetToken.id.desc())
        .first()
    )
    if not reset:
        return None
    if reset.used_at is not None:
        return None
    expires_at = _as_utc(reset.expires_at)
    if expires_at and expires_at <= _utc_now():
        return None
    reset.used_at = _utc_now()
    db.commit()
    return reset


def _get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    db = SessionLocal()
    try:
        session = (
            db.query(UserSession)
            .filter(UserSession.token == token)
            .order_by(UserSession.id.desc())
            .first()
        )
        if not session:
            return None
        expires_at = _as_utc(session.expires_at)
        if expires_at and expires_at <= _utc_now():
            db.delete(session)
            db.commit()
            return None
        user = db.query(UserAccount).filter(UserAccount.id == session.user_id).first()
        if not user:
            return None
        return {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        }
    finally:
        db.close()


def _require_user(request: Request):
    user = _get_current_user(request)
    if not user:
        return None
    return user


def _require_admin(request: Request):
    user = _get_current_user(request)
    if not user or user.get("role") != "admin":
        return None
    return user


def _ensure_admin_user():
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return
    db = SessionLocal()
    try:
        existing = db.query(UserAccount).filter(UserAccount.email == ADMIN_EMAIL).first()
        if existing:
            if existing.role != "admin":
                existing.role = "admin"
                existing.updated_at = _utc_now()
                db.commit()
            return
        admin_user = UserAccount(
            email=ADMIN_EMAIL,
            password_hash=_hash_password(ADMIN_PASSWORD),
            role="admin",
            name="Admin",
            status="actif",
        )
        db.add(admin_user)
        db.commit()
    finally:
        db.close()

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
    "bien_professionnel": "Bien professionnel",
    "autre": "Autre",
}

PROJECT_TYPE_MULTIPLIER = {
    "facade": 1.02,
    "maison": 1.0,
    "appartement": 0.94,
    "immeuble": 1.16,
    "bien_professionnel": 1.1,
    "autre": 1.0,
}

PROJECT_DEFAULT_SURFACE = {
    "facade": 90.0,
    "maison": 120.0,
    "appartement": 65.0,
    "immeuble": 340.0,
    "bien_professionnel": 180.0,
    "autre": 100.0,
}

STYLE_MULTIPLIER = {
    "moderne": 1.0,
    "contemporain": 1.06,
    "haussmannien": 1.12,
    "minimaliste": 0.98,
    "industriel": 1.04,
    "scandinave": 1.01,
    "dubai": 1.18,
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
    "dubai": {
        "palette": ["Sable dore", "Ivoire", "Bronze", "Bleu nuit"],
        "materials": ["Marbre veine or", "Bois noyer sombre", "Laiton poli"],
        "furniture_focus": ["Volumes luxueux", "Eclairage indirect chaleureux", "Touches deco Moyen-Orient"],
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
    estimate_disclaimer_line = None
    estimate_followup_line = None
    if estimate and estimate.get("min") and estimate.get("max"):
        estimate_line = (
            f"Premiere fourchette: {_human_eur(estimate['min'])} - {_human_eur(estimate['max'])} "
            "(a confirmer apres visite technique et releve)."
        )
        estimate_disclaimer_line = (
            "Cette estimation concerne la main-d'œuvre uniquement, hors matériaux, équipements et contraintes spécifiques du chantier."
        )
        estimate_followup_line = (
            "Pour obtenir un devis précis et adapté à votre projet, un échange avec un expert est nécessaire. Je peux planifier un appel."
        )

    lines: list[str] = [intro, empathy_line]
    if context_line:
        lines.append(context_line)
    if estimate_line:
        lines.append(estimate_line)
    if estimate_disclaimer_line:
        lines.append(estimate_disclaimer_line)
    if estimate_followup_line:
        lines.append(estimate_followup_line)
    lines.extend([diagnostic_line, norms_line, conductor_line])
    if project_frame_line:
        lines.append(project_frame_line)

    if core_missing:
        lines.append(
            f"Pour faire un pre-devis fiable, il me manque: {', '.join(core_missing[:3])}."
        )
        lines.append(
            "Sans ces infos, je ne peux donner qu'une estimation approximative. "
            "Le devis final se fait apres appel avec le chef de projet et visite technique."
        )
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
                f"Pour faire un pre-devis fiable, il me manque: {', '.join(missing[:3])}. "
                "Sans ces infos, je ne peux donner qu'une estimation approximative. "
                "Le devis final se fait apres appel avec le chef de projet et visite technique. "
                "Ajoutez aussi votre telephone ou email pour que je lance la suite."
            )
        return (
            f"{context_line} {mood_line} "
            "Laissez votre telephone ou email et je vous cale la suite devis + visite technique."
        )

    if missing:
        return (
            f"{context_line} Merci, j'ai bien vos coordonnees. "
            f"Pour faire un pre-devis fiable, il me manque: {', '.join(missing[:3])}. "
            "Sans ces infos, je ne peux donner qu'une estimation approximative. "
            "Le devis final se fait apres appel avec le chef de projet et visite technique."
        )

    if estimate and estimate.get("min") and estimate.get("max"):
        return (
            f"{context_line} "
            f"Premiere fourchette: {_human_eur(estimate['min'])} - {_human_eur(estimate['max'])}. "
            "Cette estimation concerne la main-d'œuvre uniquement, hors matériaux, équipements et contraintes spécifiques du chantier. "
            "Pour obtenir un devis précis et adapté à votre projet, un échange avec un expert est nécessaire. Je peux planifier un appel."
        )

    return (
        f"{context_line} "
        f"On est cadres sur l'essentiel, je vous propose la suite operationnelle avec {agent_name}."
    )


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
        return suffix
    return ".jpg"


def _safe_video_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".mp4", ".mov", ".webm"}:
        return suffix
    return ".mp4"


def _public_static_url(path: Path) -> str:
    rel = path.relative_to(STATIC_DIR).as_posix()
    return f"/static/{rel}"


def _document_public_url(stored_name: str) -> str:
    if not stored_name:
        return ""
    stored_path = Path(stored_name)
    # If stored_name already looks like a relative path inside /static, try it directly.
    if not stored_path.is_absolute() and "/" in stored_name:
        direct = STATIC_DIR / stored_path
        if direct.exists():
            return _public_static_url(direct)

    candidates = [
        STATIC_DIR / stored_path,
        ESTIMATE_UPLOAD_DIR / stored_path.name,
        CLIENT_DOCS_DIR / stored_path.name,
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return _public_static_url(candidate)
            except ValueError:
                continue
    # Fallback guess to estimate upload path
    return f"/static/estimate/uploads/{stored_path.name}"


def _resolve_document_path(stored_name: str) -> Path | None:
    if not stored_name:
        return None
    stored_path = Path(stored_name)
    candidates = [
        STATIC_DIR / stored_path,
        ESTIMATE_UPLOAD_DIR / stored_path.name,
        CLIENT_DOCS_DIR / stored_path.name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


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
        "Reponds dans la langue du client (francais ou anglais selon la derniere demande). "
        "Ton style est humain, naturel, professionnel et rassurant. "
        "Tu gardes aussi une casquette humaine: empathie, ecoute, et reformulation utile quand le client est stressé ou hesitant. "
        "Ecris comme un vrai conseiller qui comprend le client, pas comme un robot. "
        "Evite les formulations froides ou mecaniques. "
        "Ne repete pas la salutation 'Bonjour' a chaque message. "
        "Si ce n'est pas le premier tour, ne te represents pas (pas de 'ici Antoine...'). "
        "Structure en 4 a 8 lignes courtes. "
        "Integre systematiquement: 1) un angle diagnostic chantier, 2) un point normes/qualite, 3) une suite operationnelle. "
        "Si infos manquantes, demande uniquement les donnees critiques pour chiffrer serieusement. "
        "Ne promets jamais un prix ferme sans visite technique ni releve precis. "
        "Si une estimation est fournie, rappelle toujours qu'elle concerne la main-d'oeuvre uniquement."
    )

    context_prompt = (
        "Contexte metier:\n"
        f"- estimation_initiale: {estimate_text}\n"
        f"- mention_obligatoire: {LABOR_ONLY_MENTION}\n"
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
        "model": os.getenv("OPENAI_CHAT_MODEL", "gpt-5"),
        "temperature": 0.2,
        "max_output_tokens": 280,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": context_prompt},
            *conversation,
        ],
    }

    req = urlrequest.Request(
        "https://api.openai.com/v1/responses",
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
        if isinstance(data.get("output_text"), str):
            reply_text = data.get("output_text", "").strip()
        else:
            reply_text = ""
            for item in data.get("output") or []:
                if (item or {}).get("type") != "message":
                    continue
                for chunk in (item or {}).get("content") or []:
                    if (chunk or {}).get("type") != "output_text":
                        continue
                    piece = str((chunk or {}).get("text", "")).strip()
                    if piece:
                        reply_text = f"{reply_text}\n{piece}".strip()
        return reply_text or None
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
            "assessment",
            "site",
            "structural",
            "systems",
            "plumbing",
            "electrical",
            "survey",
        )
    )
    has_normes = any(
        key in t
        for key in (
            "norme",
            "nfc",
            "dtu",
            "qualite",
            "conforme",
            "etancheite",
            "securite",
            "code",
            "compliance",
            "standard",
            "safety",
            "regulation",
        )
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
            "next step",
            "next steps",
            "schedule",
            "appointment",
            "site visit",
            "survey",
            "estimate",
            "quote",
            "i need",
            "please send",
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


def _lead_summary_text(payload: dict) -> str:
    estimate_min = payload.get("estimate_min")
    estimate_max = payload.get("estimate_max")
    if not estimate_min and not estimate_max:
        return ""

    summary_lines = [
        f"Estimation: {estimate_min or 'A confirmer'} - {estimate_max or 'A confirmer'}",
        f"Mention: {LABOR_ONLY_MENTION}",
    ]

    if payload.get("work_type"):
        summary_lines.append(f"Type de projet: {payload.get('work_type')}")
    if payload.get("surface"):
        summary_lines.append(f"Surface: {payload.get('surface')}")
    if payload.get("city"):
        summary_lines.append(f"Ville: {payload.get('city')}")

    return "\n".join(summary_lines)


def _inject_lead_summary(conversation: object, payload: dict) -> object:
    summary_text = _lead_summary_text(payload)
    if not summary_text:
        return conversation

    if isinstance(conversation, dict):
        merged = dict(conversation)
        merged["estimate_summary"] = summary_text
        merged["estimate_disclaimer"] = LABOR_ONLY_MENTION
        return merged

    if isinstance(conversation, str):
        raw = conversation.strip()
        block = f"[estimation]\n{summary_text}"
        if raw:
            return f"{raw}\n\n{block}"
        return block

    return {"estimate_summary": summary_text, "estimate_disclaimer": LABOR_ONLY_MENTION}


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
        f"- Mention: {LABOR_ONLY_MENTION}",
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
    source_videos: list[str] | None = None,
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
        f"- Mention: {LABOR_ONLY_MENTION}",
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
        "Videos sources",
        *[f"- {PUBLIC_BASE_URL}{v}" for v in (source_videos or [])],
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
    finishing_level: str = "",
    work_item_key: str = "",
    work_quantity: str = "",
    work_unit: str = "",
) -> dict:
    scope_key = scope if scope in SMART_SCOPE_CONFIG else "renovation_complete"
    style_key = style if style in STYLE_MULTIPLIER else "moderne"
    project_key = project_type if project_type in PROJECT_TYPE_MULTIPLIER else "maison"
    timeline_key = timeline if timeline in TIMELINE_COST_MULTIPLIER else "6_mois"
    finishing_key = finishing_level if finishing_level in {"standard", "premium", "haut_de_gamme", "sur_mesure"} else ""

    config = SMART_SCOPE_CONFIG[scope_key]
    surface_value = _parse_number(surface) or PROJECT_DEFAULT_SURFACE[project_key]
    surface_value = max(18.0, min(5000.0, surface_value))

    room_count = int(_parse_number(rooms) or 0)
    room_count = max(0, min(60, room_count))
    budget_value = _parse_number(budget)
    complexity = _estimate_complexity(notes)
    quantity_value = _parse_number(work_quantity)

    room_factor = 1.0
    if room_count:
        room_factor += min(0.2, (room_count - 2) * 0.02)
    if room_count == 1 and surface_value > 60:
        room_factor += 0.05

    pricing_source = "smart_scope"
    base_from_grid = estimate_from_scope(scope_key, project_key, surface_value, finishing_key)
    if work_item_key:
        base_from_grid = estimate_from_item_key(work_item_key, quantity_value, (work_unit or "").strip() or None)
        if base_from_grid:
            pricing_source = "tariff_item"

    if base_from_grid:
        if pricing_source == "smart_scope":
            pricing_source = "tariff_grid"
        low_raw = (
            base_from_grid["low"]
            * PROJECT_TYPE_MULTIPLIER[project_key]
            * STYLE_MULTIPLIER[style_key]
            * TIMELINE_COST_MULTIPLIER[timeline_key]
            * complexity
            * room_factor
        )
        high_raw = (
            base_from_grid["high"]
            * PROJECT_TYPE_MULTIPLIER[project_key]
            * STYLE_MULTIPLIER[style_key]
            * TIMELINE_COST_MULTIPLIER[timeline_key]
            * complexity
            * room_factor
        )
    else:
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
    if pricing_source == "tariff_grid":
        assumptions.append("Base grille tarifaire Eurobat (main-d'oeuvre).")
    if pricing_source == "tariff_item":
        assumptions.append("Calcul base sur poste specifique de la grille tarifaire.")
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
            "estimate_disclaimer": LABOR_ONLY_MENTION,
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
    ESTIMATE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    Base.metadata.create_all(bind=engine)
    _ensure_admin_user()
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
templates.env.globals["get_current_user"] = _get_current_user
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
    return RedirectResponse("/estimation-projet", status_code=307)


@app.get("/simulation-3d", response_class=HTMLResponse)
def simulation_3d_page(request: Request):
    return RedirectResponse("/estimation-projet", status_code=307)


@app.get("/votre-projet", response_class=HTMLResponse)
def votre_projet(request: Request):
    return templates.TemplateResponse(request, "votre_projet.html")


@app.get("/votre-projet/particulier", response_class=HTMLResponse)
def votre_projet_particulier(request: Request):
    return templates.TemplateResponse(request, "votre_projet_particulier.html")


@app.get("/votre-projet/professionnel", response_class=HTMLResponse)
def votre_projet_professionnel(request: Request):
    return templates.TemplateResponse(request, "votre_projet_professionnel.html")


@app.get("/nos-chantiers", response_class=HTMLResponse)
def nos_chantiers(request: Request):
    return templates.TemplateResponse(request, "nos_chantiers.html")


@app.get("/avant-apres", response_class=HTMLResponse)
def avant_apres(request: Request):
    return templates.TemplateResponse(request, "avant_apres.html")


@app.get("/ressources", response_class=HTMLResponse)
def ressources(request: Request):
    return templates.TemplateResponse(request, "ressources.html")


@app.get("/nos-metiers", response_class=HTMLResponse)
def nos_metiers(request: Request):
    return templates.TemplateResponse(request, "nos_metiers.html")


@app.get("/estimez-votre-projet", response_class=HTMLResponse)
def estimate_page(request: Request):
    return RedirectResponse("/estimation-projet", status_code=307)


@app.get("/estimation-projet", response_class=HTMLResponse)
def estimate_project_page(request: Request):
    return templates.TemplateResponse(request, "estimation_projet.html")


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(request, "contact.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = ""):
    current_user = _get_current_user(request)
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next},
    )


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, next: str = ""):
    current_user = _get_current_user(request)
    if current_user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "signup.html",
        {"next": next},
    )


@app.post("/signup")
def signup_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    password: str = Form(...),
    next: str = Form(""),
):
    normalized_email = email.strip().lower()
    db = SessionLocal()
    try:
        existing = db.query(UserAccount).filter(UserAccount.email == normalized_email).first()
        if existing:
            return templates.TemplateResponse(
                request,
                "signup.html",
                {
                    "error": "Un compte existe deja avec cet email.",
                    "next": next,
                },
                status_code=400,
            )
        client = UserAccount(
            email=normalized_email,
            password_hash=_hash_password(password),
            role="client",
            name=name.strip(),
            phone=phone.strip(),
            status="actif",
        )
        db.add(client)
        db.commit()
        db.refresh(client)
        token = _create_session(db, client.id)
    finally:
        db.close()

    redirect_target = next or "/dashboard"
    response = RedirectResponse(redirect_target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        path="/",
    )
    return response


@app.get("/password-reset", response_class=HTMLResponse)
def password_reset_request_page(request: Request):
    return templates.TemplateResponse(request, "password_reset_request.html")


@app.post("/password-reset")
def password_reset_request(
    request: Request,
    email: str = Form(...),
):
    normalized_email = email.strip().lower()
    db = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.email == normalized_email).first()
        if not user:
            return templates.TemplateResponse(
                request,
                "password_reset_request.html",
                {"error": "Aucun compte avec cet email."},
                status_code=400,
            )

        smtp_cfg = _smtp_settings()
        if not _smtp_ready(smtp_cfg):
            return templates.TemplateResponse(
                request,
                "password_reset_request.html",
                {"error": "Envoi email indisponible. Merci de reessayer plus tard."},
                status_code=503,
            )

        token = _create_password_reset(db, user.id)
    finally:
        db.close()

    base_url = str(request.base_url).rstrip("/")
    reset_link = f"{base_url}/password-reset/{token}"
    subject = "Reinitialisation mot de passe"
    body = (
        "Bonjour,\n\n"
        "Voici votre lien pour reinitialiser votre mot de passe:\n"
        f"{reset_link}\n\n"
        f"Ce lien expire dans {PASSWORD_RESET_TTL_HOURS} heures.\n"
        "Si vous n'etes pas a l'origine de cette demande, ignorez ce message.\n"
    )
    _send_email_message(to_email=normalized_email, subject=subject, text_body=body)
    return templates.TemplateResponse(request, "password_reset_done.html", {"email": normalized_email})


@app.get("/password-reset/{token}", response_class=HTMLResponse)
def password_reset_confirm_page(request: Request, token: str):
    return templates.TemplateResponse(request, "password_reset_confirm.html", {"token": token})


@app.post("/password-reset/{token}")
def password_reset_confirm(
    request: Request,
    token: str,
    password: str = Form(...),
):
    db = SessionLocal()
    try:
        reset = _consume_password_reset(db, token)
        if not reset:
            return templates.TemplateResponse(
                request,
                "password_reset_confirm.html",
                {
                    "token": token,
                    "error": "Lien invalide ou expire.",
                },
                status_code=400,
            )
        user = db.query(UserAccount).filter(UserAccount.id == reset.user_id).first()
        if not user:
            return templates.TemplateResponse(
                request,
                "password_reset_confirm.html",
                {
                    "token": token,
                    "error": "Compte introuvable.",
                },
                status_code=400,
            )
        user.password_hash = _hash_password(password)
        user.updated_at = _utc_now()
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/login", status_code=303)


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    normalized_email = email.strip().lower()
    db = SessionLocal()
    try:
        user = db.query(UserAccount).filter(UserAccount.email == normalized_email).first()
        if not user or not _verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "error": "Email ou mot de passe incorrect.",
                    "next": next,
                },
                status_code=400,
            )
        token = _create_session(db, user.id)
    finally:
        db.close()

    redirect_target = next or "/dashboard"
    response = RedirectResponse(redirect_target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        db = SessionLocal()
        try:
            session = db.query(UserSession).filter(UserSession.token == token).first()
            if session:
                db.delete(session)
                db.commit()
        finally:
            db.close()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard", status_code=303)
    if user.get("role") == "admin":
        return RedirectResponse("/admin", status_code=303)

    db = SessionLocal()
    try:
        projects = (
            db.query(ClientProject)
            .filter(ClientProject.client_id == user["id"])
            .order_by(ClientProject.updated_at.desc(), ClientProject.created_at.desc())
            .all()
        )
        project_ids = [project.id for project in projects]
        documents = []
        if project_ids:
            documents = (
                db.query(ProjectDocument)
                .filter(ProjectDocument.project_id.in_(project_ids))
                .order_by(ProjectDocument.created_at.desc())
                .all()
            )
        latest_handoff = (
            db.query(HandoffRequest)
            .filter(
                (HandoffRequest.email == user.get("email"))
                | (HandoffRequest.phone == user.get("phone"))
                | (HandoffRequest.email.is_(None))
            )
            .order_by(HandoffRequest.created_at.desc())
            .first()
        )
    finally:
        db.close()

    documents_by_project = {}
    for doc in documents:
        documents_by_project.setdefault(doc.project_id, []).append(doc)

    # build project recap map
    project_recaps = {}
    for project in projects:
        recap = _parse_project_summary(project.summary)
        if recap:
            if not recap.get("updated_label") and project.updated_at:
                recap["updated_at"] = project.updated_at
                recap["updated_label"] = project.updated_at.strftime("%d/%m/%Y %H:%M")
        project_recaps[project.id] = recap

    # latest handoff recap enriched with columns
    handoff_recap = {}
    if latest_handoff:
        try:
            payload = json.loads(latest_handoff.conversation or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            handoff_recap = {
                "project_type": payload.get("project_type") or payload.get("work_type") or "",
                "scope": payload.get("scope") or "",
                "style": payload.get("style") or "",
                "surface": payload.get("surface") or (latest_handoff.surface or ""),
                "rooms": payload.get("rooms") or "",
                "budget": payload.get("budget") or "",
                "city": payload.get("city") or (latest_handoff.city or ""),
                "finishing_level": payload.get("finishing_level") or "",
                "estimate_range": payload.get("quote", {}).get("estimate_range") if isinstance(payload.get("quote"), dict) else None,
                "appointment_status": payload.get("appointment_status") or latest_handoff.status or "Non planifie",
                "timeline": payload.get("timeline") or "",
                "work_item_key": payload.get("work_item_key") or "",
                "work_quantity": payload.get("work_quantity") or "",
                "work_unit": payload.get("work_unit") or "",
                "updated_at": latest_handoff.created_at,
                "updated_label": latest_handoff.created_at.strftime("%d/%m/%Y %H:%M") if latest_handoff.created_at else "",
            }

    # merge preference: projet si non vide, sinon handoff; si handoff plus récent, on remplace
    recap: dict = {}
    project_recap = project_recaps.get(projects[0].id) if projects else {}
    project_ts = projects[0].updated_at if projects else None
    handoff_ts = handoff_recap.get("updated_at") if handoff_recap else None

    if project_recap and not _is_recap_empty(project_recap):
        recap = project_recap
    if handoff_recap:
        if _is_recap_empty(recap):
            recap = handoff_recap
        elif handoff_ts and project_ts and handoff_ts > project_ts:
            recap = handoff_recap

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "projects": projects,
            "documents_by_project": documents_by_project,
            "project_recaps": project_recaps,
            "hide_public_header": True,
            "recap": recap,
        },
    )


@app.get("/dashboard/documents", response_class=HTMLResponse)
@app.get("/dashboard/documents/", response_class=HTMLResponse)
def dashboard_documents(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/documents", status_code=303)
    if user.get("role") == "admin":
        return RedirectResponse("/admin", status_code=303)

    db = SessionLocal()
    try:
        projects = (
            db.query(ClientProject)
            .filter(ClientProject.client_id == user["id"])
            .order_by(ClientProject.updated_at.desc(), ClientProject.created_at.desc())
            .all()
        )
        documents = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.client_id == user["id"])
            .order_by(ProjectDocument.created_at.desc())
            .all()
        )
    finally:
        db.close()

    project_recaps: dict[int, dict] = {}
    for project in projects:
        recap = _parse_project_summary(project.summary)
        project_recaps[project.id] = recap or {}

    docs_by_project: dict[int, list[dict]] = {}
    docs_flat: list[dict] = []
    for doc in documents:
        resolved_path = _resolve_document_path(doc.stored_name)
        url = _document_public_url(doc.stored_name)
        size_label = ""
        if resolved_path and resolved_path.exists():
            try:
                size_bytes = resolved_path.stat().st_size
                if size_bytes >= 1_000_000:
                    size_label = f"{size_bytes/1_000_000:.1f} Mo"
                elif size_bytes >= 1_000:
                    size_label = f"{size_bytes/1_000:.0f} Ko"
                else:
                    size_label = f"{size_bytes} o"
            except OSError:
                size_label = ""
        entry = {
            "id": doc.id,
            "label": doc.label or "Document",
            "name": doc.original_name,
            "url": url,
            "mime_type": doc.mime_type or "",
            "created_at": doc.created_at.strftime("%d/%m/%Y %H:%M") if doc.created_at else "",
            "stored_name": doc.stored_name,
            "size": size_label,
        }
        docs_by_project.setdefault(doc.project_id, []).append(entry)
        docs_flat.append(entry)

    return templates.TemplateResponse(
        request,
        "dashboard_documents.html",
        {
            "user": user,
            "projects": projects,
            "docs_by_project": docs_by_project,
            "docs_flat": docs_flat,
            "project_recaps": project_recaps,
            "hide_public_header": True,
        },
    )


@app.get("/documents", response_class=HTMLResponse)
@app.get("/documents/", response_class=HTMLResponse)
def documents_redirect(request: Request):
    user = _get_current_user(request)
    if user:
        return RedirectResponse("/dashboard/documents", status_code=303)
    return RedirectResponse("/login?next=/dashboard/documents", status_code=303)


@app.post("/api/devis-final")
def request_final_quote(
    request: Request,
    project_id: int = Form(...),
    message: str = Form(""),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard", status_code=303)

    db = SessionLocal()
    try:
        project = (
            db.query(ClientProject)
            .filter(ClientProject.id == project_id, ClientProject.client_id == user["id"])
            .first()
        )
        if not project:
            return RedirectResponse("/dashboard?error=project_missing", status_code=303)

        project.status = "Devis final demande"
        project.updated_at = _utc_now()
        db.commit()
    finally:
        db.close()

    smtp_cfg = _smtp_settings()
    if _smtp_ready(smtp_cfg):
        internal_recipient = (INTERNAL_REPORT_EMAIL or smtp_cfg.get("from_email") or "").strip()
        if internal_recipient:
            subject = "Demande devis final client"
            body = (
                f"Client: {user.get('name') or user.get('email')}\n"
                f"Email: {user.get('email')}\n"
                f"Projet: {project.title}\n"
                f"Statut: {project.status}\n"
                f"Message: {message or 'Aucun message.'}\n"
            )
            _send_email_message(to_email=internal_recipient, subject=subject, text_body=body)

    return RedirectResponse("/dashboard?success=devis_final", status_code=303)


def _safe_doc_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm"}:
        return suffix
    return ".pdf"


@app.post("/api/project-document")
async def upload_project_document(
    request: Request,
    project_id: int = Form(...),
    label: str = Form("Document client"),
    file: UploadFile = File(...),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/documents", status_code=303)

    if not file or not file.filename:
        return RedirectResponse("/dashboard/documents?error=missing_file", status_code=303)

    db = SessionLocal()
    try:
        project = (
            db.query(ClientProject)
            .filter(ClientProject.id == project_id, ClientProject.client_id == user["id"])
            .first()
        )
        if not project:
            return RedirectResponse("/dashboard/documents?error=invalid_project", status_code=303)

        suffix = _safe_doc_suffix(file.filename)
        CLIENT_DOCS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"client_doc_{_utc_file_stamp()}_{uuid.uuid4().hex[:8]}{suffix}"
        dst = CLIENT_DOCS_DIR / filename
        content = await file.read()
        if not content:
            return RedirectResponse("/dashboard/documents?error=empty_file", status_code=303)
        dst.write_bytes(content)

        db.add(
            ProjectDocument(
                client_id=user["id"],
                project_id=project.id,
                label=label or "Document client",
                original_name=file.filename,
                stored_name=filename,
                mime_type=file.content_type,
            )
        )
        project.updated_at = _utc_now()
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/dashboard/documents?success=uploaded", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/login?next=/admin", status_code=303)

    db = SessionLocal()
    try:
        clients = (
            db.query(UserAccount)
            .filter(UserAccount.role == "client")
            .order_by(UserAccount.created_at.desc())
            .all()
        )
        projects = db.query(ClientProject).order_by(ClientProject.created_at.desc()).all()
        documents = db.query(ProjectDocument).order_by(ProjectDocument.created_at.desc()).all()
    finally:
        db.close()

    projects_by_client = {}
    for project in projects:
        projects_by_client.setdefault(project.client_id, []).append(project)

    documents_by_project = {}
    for doc in documents:
        documents_by_project.setdefault(doc.project_id, []).append(doc)

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": user,
            "clients": clients,
            "projects_by_client": projects_by_client,
            "documents_by_project": documents_by_project,
        },
    )


@app.post("/admin/clients")
def admin_create_client(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(""),
    status: str = Form(""),
    password: str = Form(...),
    project_title: str = Form(""),
    project_summary: str = Form(""),
    project_status: str = Form(""),
):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/login?next=/admin", status_code=303)

    normalized_email = email.strip().lower()
    db = SessionLocal()
    try:
        existing = db.query(UserAccount).filter(UserAccount.email == normalized_email).first()
        if existing:
            return RedirectResponse("/admin?error=client_exists", status_code=303)

        client = UserAccount(
            email=normalized_email,
            password_hash=_hash_password(password),
            role="client",
            name=name.strip(),
            phone=phone.strip(),
            status=status.strip() if status else None,
        )
        db.add(client)
        db.commit()
        db.refresh(client)

        if project_title.strip():
            project = ClientProject(
                client_id=client.id,
                title=project_title.strip(),
                summary=project_summary.strip() if project_summary else None,
                status=project_status.strip() if project_status else None,
            )
            db.add(project)
            db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/documents")
def admin_upload_document(
    request: Request,
    project_id: int = Form(...),
    label: str = Form(""),
    document: UploadFile = File(...),
):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/login?next=/admin", status_code=303)

    suffix = _safe_suffix(document.filename or "")
    stored_name = f"{project_id}-{_utc_file_stamp()}-{uuid.uuid4().hex}{suffix}"
    file_path = CLIENT_DOCS_DIR / stored_name
    with file_path.open("wb") as buffer:
        buffer.write(document.file.read())
    document.file.close()

    db = SessionLocal()
    try:
        project = db.query(ClientProject).filter(ClientProject.id == project_id).first()
        if not project:
            return RedirectResponse("/admin?error=project_missing", status_code=303)
        doc = ProjectDocument(
            client_id=project.client_id,
            project_id=project.id,
            label=label.strip() if label else None,
            original_name=document.filename or stored_name,
            stored_name=stored_name,
            mime_type=document.content_type,
        )
        db.add(doc)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin", status_code=303)


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

    contact_detected = has_contact_info(full_text)
    contact_just_provided = has_contact_info(last) and not has_contact_info(previous_text)
    work_type = context.get("work_type")
    surface_m2 = context.get("surface_m2")
    city_hint = context.get("city_hint")
    budget_hint = context.get("budget_hint")
    timeline_hint = context.get("timeline_hint")
    client_mood = context.get("client_mood", "neutral")
    tariff_item = get_tariff_item(last) or get_tariff_item(full_text)

    est = estimate_from_text(last, default_surface=surface_m2)
    if est.get("confidence", 0) <= 0.5 and full_text:
        est = estimate_from_text(full_text, default_surface=surface_m2)
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
    if tariff_item and not has_required_quantity(full_text, tariff_item):
        unit = tariff_item.get("unit")
        if unit == "m2":
            missing_fields.append("surface en m2")
        elif unit == "m3":
            missing_fields.append("volume en m3")
        elif unit == "unite":
            missing_fields.append("nombre d'unites")
        elif unit == "ml":
            missing_fields.append("metres lineaires")

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
    finishing_level: str = Form(""),
    work_item_key: str = Form(""),
    work_quantity: str = Form(""),
    work_unit: str = Form(""),
    city: str = Form(""),
    surface: str = Form(""),
    rooms: str = Form(""),
    budget: str = Form(""),
    notes: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    project_photos: list[UploadFile] = File([]),
    project_videos: list[UploadFile] = File([]),
    project_dpe: UploadFile | None = File(None),
    project_plans: list[UploadFile] = File([]),
    visitor_id: str = Form(""),
    visitor_landing: str = Form(""),
    visitor_referrer: str = Form(""),
    visitor_utm: str = Form(""),
):
    current_user = _get_current_user(request)
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
    email_enabled = _smtp_ready(smtp_cfg)
    internal_recipient = (INTERNAL_REPORT_EMAIL or smtp_cfg.get("from_email") or "").strip()

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
        finishing_level=finishing_level,
        work_item_key=work_item_key,
        work_quantity=work_quantity,
        work_unit=work_unit,
    )

    project_saved = False
    saved_project_id = None
    if current_user:
        db = SessionLocal()
        try:
            project_title = f"Renovation {PROJECT_TYPE_LABELS.get(project_key, 'Projet')}"
            summary_payload = {
                "project_type": PROJECT_TYPE_LABELS.get(project_key, project_key),
                "scope": SMART_SCOPE_LABELS.get(scope_key, scope_key),
                "style": STYLE_LABELS.get(style_key, style_key) if "STYLE_LABELS" in globals() else style_key,
                "surface": surface or "",
                "rooms": rooms or "",
                "budget": budget or "",
                "city": city or "",
                "finishing_level": finishing_level or "",
                "estimate_range": f"{quote['low_label']} - {quote['high_label']}",
                "timeline": timeline or "",
                "work_item_key": work_item_key or "",
                "work_quantity": work_quantity or "",
                "work_unit": work_unit or "",
                "appointment_status": "Non",
            }
            # update latest project if exists, else create
            latest_project = (
                db.query(ClientProject)
                .filter(ClientProject.client_id == current_user["id"])
                .order_by(ClientProject.created_at.desc())
                .first()
            )
            if latest_project:
                latest_project.title = project_title
                latest_project.summary = json.dumps(summary_payload, ensure_ascii=False)
                latest_project.status = "Pre-devis mis a jour"
                latest_project.updated_at = _utc_now()
                saved_project_id = latest_project.id
            else:
                project = ClientProject(
                    client_id=current_user["id"],
                    title=project_title,
                    summary=json.dumps(summary_payload, ensure_ascii=False),
                    status="Pre-devis envoye",
                )
                db.add(project)
                db.commit()
                db.refresh(project)
                saved_project_id = project.id
            db.commit()
            project_saved = True
        finally:
            db.close()

    photo_urls: list[str] = []
    for upload in project_photos or []:
        if not upload or not upload.filename:
            continue
        suffix = _safe_suffix(upload.filename)
        filename = f"estimate_photo_{_utc_file_stamp()}_{uuid.uuid4().hex[:8]}{suffix}"
        dst = ESTIMATE_UPLOAD_DIR / filename
        content = await upload.read()
        if content:
            dst.write_bytes(content)
            photo_urls.append(_public_static_url(dst))
            document_refs.append(
                {
                    "label": "Photo du bien",
                    "original_name": upload.filename,
                    "stored_name": filename,
                    "mime_type": upload.content_type,
                    "path": dst,
                }
            )

    video_urls: list[str] = []
    for upload in project_videos or []:
        if not upload or not upload.filename:
            continue
        suffix = _safe_video_suffix(upload.filename)
        filename = f"estimate_video_{_utc_file_stamp()}_{uuid.uuid4().hex[:8]}{suffix}"
        dst = ESTIMATE_UPLOAD_DIR / filename
        content = await upload.read()
        if content:
            dst.write_bytes(content)
            video_urls.append(_public_static_url(dst))
            document_refs.append(
                {
                    "label": "Video du bien",
                    "original_name": upload.filename,
                    "stored_name": filename,
                    "mime_type": upload.content_type,
                    "path": dst,
                }
            )

    document_refs: list[dict] = []
    if project_dpe and project_dpe.filename:
        suffix = _safe_suffix(project_dpe.filename)
        filename = f"dpe_{_utc_file_stamp()}_{uuid.uuid4().hex[:8]}{suffix}"
        dst = ESTIMATE_UPLOAD_DIR / filename
        content = await project_dpe.read()
        if content:
            dst.write_bytes(content)
            document_refs.append(
                {
                    "label": "DPE",
                    "original_name": project_dpe.filename,
                    "stored_name": filename,
                    "mime_type": project_dpe.content_type,
                }
            )

    for upload in project_plans or []:
        if not upload or not upload.filename:
            continue
        suffix = _safe_suffix(upload.filename)
        filename = f"plan_{_utc_file_stamp()}_{uuid.uuid4().hex[:8]}{suffix}"
        dst = ESTIMATE_UPLOAD_DIR / filename
        content = await upload.read()
        if content:
            dst.write_bytes(content)
            document_refs.append(
                {
                    "label": "Plan du bien",
                    "original_name": upload.filename,
                    "stored_name": filename,
                    "mime_type": upload.content_type,
                }
            )

    if project_saved and saved_project_id and document_refs:
        db = SessionLocal()
        try:
            for doc in document_refs:
                db.add(
                    ProjectDocument(
                        client_id=current_user["id"],
                        project_id=saved_project_id,
                        label=doc["label"],
                        original_name=doc["original_name"],
                        stored_name=doc["stored_name"],
                        mime_type=doc.get("mime_type"),
                    )
                )
            db.commit()
        finally:
            db.close()

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
                        "finishing_level": finishing_level,
                        "work_item_key": work_item_key,
                        "work_quantity": work_quantity,
                        "work_unit": work_unit,
                        "city": city,
                        "surface": surface,
                        "rooms": rooms,
                        "budget": budget,
                        "quote": quote,
                        "estimate_disclaimer": LABOR_ONLY_MENTION,
                        "source_photos": photo_urls,
                        "source_videos": video_urls,
                        "documents": document_refs,
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

    # si SMTP non config, on renvoie quand même l'estimation
    if not email_enabled or not internal_recipient:
        return {
            "ok": True,
            "message": "Estimation sauvegardee (email non envoye - SMTP non configure).",
            "quote": quote,
            "handoff_id": handoff_id,
            "delivery": {
                "client_email_sent": False,
                "internal_email_sent": False,
            },
        }

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
        source_photos=photo_urls,
        source_videos=video_urls,
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
        "project_saved": project_saved,
        "project_id": saved_project_id,
        "account_required_for_final": current_user is None,
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
            finishing_level=str((handoff_payload or {}).get("finishing_level", "")),
            work_item_key=str((handoff_payload or {}).get("work_item_key", "")),
            work_quantity=str((handoff_payload or {}).get("work_quantity", "")),
            work_unit=str((handoff_payload or {}).get("work_unit", "")),
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
                    "estimate_disclaimer": LABOR_ONLY_MENTION,
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
                        "estimate_disclaimer": LABOR_ONLY_MENTION,
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
            source_videos=None,
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
        finishing_level="",
        work_item_key="",
        work_quantity="",
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
                        "estimate_disclaimer": LABOR_ONLY_MENTION,
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
        source_videos=None,
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
    raw_message = payload.get("raw_message")
    lead_summary = _lead_summary_text(payload)
    if lead_summary:
        if raw_message:
            raw_message = f"{raw_message}\n\n[estimation]\n{lead_summary}"
        else:
            raw_message = f"[estimation]\n{lead_summary}"
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
            raw_message=_attach_tracking_to_raw(raw_message, tracking_context),
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
    conversation_payload = _inject_lead_summary(payload.get("conversation"), payload)
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
            conversation=_attach_tracking_to_conversation(conversation_payload, tracking_context),
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
