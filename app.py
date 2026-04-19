# noinspection SpellCheckingInspection
import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import smtplib
import unicodedata
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
try:
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas as rl_canvas
except Exception:  # pragma: no cover - optional runtime dependency
    rl_canvas = None
    A4 = (595.27, 841.89)
    simpleSplit = None
    rl_colors = None

from database import SessionLocal, engine
from db import init_db, insert_lead
from intelligence.router import router as intelligence_router
from models import (
    Base,
    ChantierContract,
    ChantierEvent,
    ChantierLot,
    ChantierMilestone,
    ClientProject,
    HandoffRequest,
    Lead,
    PasswordResetToken,
    ProjectDocument,
    UserAccount,
    UserSession,
)
from pricing import (
    CATALOG_ESTIMATE_ERROR,
    COMPAT_CODE_ALIASES,
    ESTIMATE_WORK_ITEM_GROUPS,
    SCOPE_TO_CODE,
    TARIFF_BY_KEY,
    estimate_catalog_lines,
    estimate_from_text,
    get_tariff_item,
    has_required_quantity,
)
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
PREQUOTE_PDF_DIR = STATIC_DIR / "estimate" / "predevis"
CLIENT_DOCS_DIR = STATIC_DIR / "client-docs"
AGENDA_URL = os.getenv("AGENDA_URL", "").strip()
DEFAULT_SMTP_FROM_NAME = "EUROBAT SERVICES"
DEFAULT_SMTP_FROM_EMAIL = "devis@eurobatservices.com"
INTERNAL_REPORT_EMAIL = os.getenv("INTERNAL_REPORT_EMAIL", DEFAULT_SMTP_FROM_EMAIL).strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
LABOR_ONLY_MENTION = "Main-d'œuvre uniquement, hors matériaux et fournitures."
PREQUOTE_DOC_LABEL_PREFIX = "predevis:estimateur"
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


def _humanize(value: str | None) -> str:
    """Convert snake_case or raw keys to human-readable title text."""
    if not value:
        return "—"
    return str(value).replace("_", " ").strip().title()

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


def _catalog_range(code: str, fallback_low: int, fallback_high: int) -> dict[str, int]:
    item = TARIFF_BY_KEY.get(code)
    if not item:
        raise RuntimeError(f"Catalogue item missing for scope mapping: {code}")
    return {"low_m2": int(item["min"]), "high_m2": int(item["max"])}


SMART_SCOPE_CONFIG = {
    "rafraichissement": {**_catalog_range("renovation_legere", 250, 750), "duration": (2, 6)},
    "renovation_partielle": {**_catalog_range("renovation_legere", 250, 750), "duration": (4, 10)},
    "renovation_complete": {**_catalog_range("renovation_complete", 1200, 2500), "duration": (8, 18)},
    "restructuration_lourde": {**_catalog_range("renovation_lourde", 1200, 2500), "duration": (12, 28)},
}
WORK_ITEM_ONLY_SCOPE = "par_choix_prestation"

SMART_SCOPE_LABELS = {
    WORK_ITEM_ONLY_SCOPE: "Choix par prestation",
    "rafraichissement": "Rafraichissement",
    "renovation_partielle": "Renovation partielle",
    "renovation_complete": "Renovation complete",
    "restructuration_lourde": "Restructuration lourde",
}


def _normalize_scope_key(scope: str, *, default_if_empty: str = "renovation_complete") -> str:
    scope_key = (scope or "").strip().lower()
    if not scope_key:
        return default_if_empty
    alias_map = {
        "choix par prestation": WORK_ITEM_ONLY_SCOPE,
        "par choix prestation": WORK_ITEM_ONLY_SCOPE,
        "prestation unique": WORK_ITEM_ONLY_SCOPE,
        "renovation complete": "renovation_complete",
        "renovation partielle": "renovation_partielle",
        "rafraichissement": "rafraichissement",
        "restructuration lourde": "restructuration_lourde",
    }
    if scope_key in alias_map:
        return alias_map[scope_key]
    if scope_key == WORK_ITEM_ONLY_SCOPE:
        return WORK_ITEM_ONLY_SCOPE
    if scope_key in SMART_SCOPE_CONFIG:
        return scope_key
    return ""

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


def _pdf_ascii_text(value: object, limit: int = 900) -> str:
    raw = _clean_text(value, limit=limit)
    if not raw:
        return ""
    normalized = (
        unicodedata.normalize("NFKC", raw)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("•", "-")
        .replace("–", "-")
        .replace("—", "-")
    )
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = re.sub(r"\bEUR\b", "€", normalized, flags=re.IGNORECASE)
    return normalized.strip()


def _wrap_pdf_line(text: str, max_len: int = 92) -> list[str]:
    normalized = _pdf_ascii_text(text, limit=2000)
    if not normalized:
        return [""]

    lines: list[str] = []
    for source_line in normalized.splitlines():
        stripped = source_line.strip()
        if not stripped:
            lines.append("")
            continue

        words = stripped.split(" ")
        current = ""
        for word in words:
            if not current:
                current = word
                continue
            candidate = f"{current} {word}"
            if len(candidate) <= max_len:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)

    return lines or [""]


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _format_pdf_currency(value: object) -> str:
    parsed = _parse_number(value)
    if parsed is None:
        return _pdf_ascii_text(value, limit=120) or "A confirmer"
    amount = int(round(parsed))
    return f"{amount:,}".replace(",", " ") + " €"


def _format_pdf_surface(value: object) -> str:
    parsed = _parse_number(value)
    if parsed is None or parsed <= 0:
        text = _pdf_ascii_text(value, limit=80)
        return text or "A confirmer"
    if float(parsed).is_integer():
        label = str(int(parsed))
    else:
        label = f"{parsed:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{label} m2"


def _timeline_label(timeline: str) -> str:
    timeline_map = {
        "urgent": "Urgent",
        "3_mois": "Sous 3 mois",
        "6_mois": "Sous 6 mois",
        "flexible": "Flexible",
    }
    return timeline_map.get((timeline or "").strip(), _pdf_ascii_text(timeline, limit=60) or "A confirmer")


def _to_eur_symbol(label: object) -> str:
    text = _pdf_ascii_text(label, limit=140)
    if not text:
        return "A confirmer"
    return re.sub(r"\bEUR\b", "€", text, flags=re.IGNORECASE)


def _generate_prequote_number() -> str:
    year = _utc_now().strftime("%Y")
    serial = f"{secrets.randbelow(10_000):04d}"
    return f"PD-{year}-{serial}"


def _build_simple_text_pdf(page_lines: list[list[str]]) -> bytes:
    pages = page_lines if page_lines else [["Pre-devis"]]
    object_map: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_refs: list[int] = []

    for index, lines in enumerate(pages):
        page_obj_id = 4 + index * 2
        content_obj_id = page_obj_id + 1
        page_refs.append(page_obj_id)

        commands = ["BT", "/F1 11 Tf", "50 800 Td", "14 TL"]
        for raw_line in lines:
            safe_line = _pdf_escape(_pdf_ascii_text(raw_line, limit=500))
            commands.append(f"({safe_line}) Tj")
            commands.append("T*")
        commands.append("ET")
        content = "\n".join(commands).encode("cp1252", "replace")

        object_map[page_obj_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_id} 0 R >>"
        ).encode("ascii")
        object_map[content_obj_id] = (
            f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
            + content
            + b"\nendstream"
        )

    kids = " ".join(f"{obj_id} 0 R" for obj_id in page_refs) or "4 0 R"
    object_map[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_refs) or 1} >>".encode("ascii")
    if not page_refs:
        object_map[4] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        object_map[5] = b"<< /Length 43 >>\nstream\nBT\n/F1 11 Tf\n50 800 Td\n(Pre-devis) Tj\nET\nendstream"

    max_obj_id = max(object_map.keys())
    buffer = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0] * (max_obj_id + 1)

    for obj_id in range(1, max_obj_id + 1):
        payload = object_map.get(obj_id, b"<<>>")
        offsets[obj_id] = len(buffer)
        buffer += f"{obj_id} 0 obj\n".encode("ascii")
        buffer += payload
        buffer += b"\nendobj\n"

    xref_offset = len(buffer)
    buffer += f"xref\n0 {max_obj_id + 1}\n".encode("ascii")
    buffer += b"0000000000 65535 f \n"
    for obj_id in range(1, max_obj_id + 1):
        buffer += f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii")
    buffer += (
        f"trailer\n<< /Size {max_obj_id + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return buffer


def _build_prequote_pdf_reportlab(
    *,
    prequote_number: str,
    now_label: str,
    summary_lines: list[str],
    context_lines: list[str],
    detail_lines: list[str],
    assumptions_lines: list[str],
    notes_lines: list[str],
    final_lines: list[str],
) -> bytes | None:
    if rl_canvas is None or rl_colors is None or simpleSplit is None:
        return None

    page_width, page_height = A4
    margin_x = 34
    margin_top = 28
    margin_bottom = 36
    content_width = page_width - (2 * margin_x)
    buffer = io.BytesIO()
    c = rl_canvas.Canvas(buffer, pagesize=A4)

    col_bg = rl_colors.HexColor("#F4F6FA")
    col_primary = rl_colors.HexColor("#1F365C")
    col_title = rl_colors.HexColor("#16263F")
    col_text = rl_colors.HexColor("#2C3A4D")
    col_muted = rl_colors.HexColor("#5A6A80")

    def _wrap_line(text: str, font_name: str = "Helvetica", font_size: float = 9.2, max_width: float = 460) -> list[str]:
        clean = _pdf_ascii_text(text, limit=3000)
        if not clean:
            return [""]
        lines = [line for line in simpleSplit(clean, font_name, font_size, max_width) if line]
        return lines or [clean]

    def _draw_page_background() -> None:
        c.setFillColor(col_bg)
        c.rect(0, 0, page_width, page_height, stroke=0, fill=1)

    def _draw_header() -> float:
        top_y = page_height - margin_top
        header_h = 102
        c.setFillColor(col_primary)
        c.roundRect(margin_x, top_y - header_h, content_width, header_h, 14, stroke=0, fill=1)

        text_left = margin_x + 18
        logo_path = STATIC_DIR / "branding" / "eurobat-services.png"
        if logo_path.exists():
            try:
                c.drawImage(
                    str(logo_path),
                    margin_x + 14,
                    top_y - header_h + 19,
                    width=66,
                    height=66,
                    preserveAspectRatio=True,
                    mask="auto",
                )
                text_left = margin_x + 88
            except Exception:
                text_left = margin_x + 18

        c.setFillColor(rl_colors.white)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(text_left, top_y - 31, "EUROBAT SERVICES")
        c.setFont("Helvetica-Bold", 12)
        c.drawString(text_left, top_y - 50, "Pre-devis estimateur")
        c.setFont("Helvetica", 9.5)
        c.drawRightString(margin_x + content_width - 14, top_y - 31, f"N° pre-devis : {prequote_number}")
        c.drawRightString(margin_x + content_width - 14, top_y - 47, f"Date generation : {now_label}")
        c.setStrokeColor(rl_colors.Color(1, 1, 1, alpha=0.32))
        c.setLineWidth(0.8)
        c.line(text_left, top_y - 59, margin_x + content_width - 14, top_y - 59)
        return top_y - header_h - 11

    _draw_page_background()
    cursor_y = _draw_header()

    sections: list[tuple[str, list[str]]] = [
        ("Resume financier", summary_lines),
        ("Contexte du projet", context_lines),
        ("Detail des postes", detail_lines),
        ("Hypotheses de calcul", assumptions_lines),
        ("Notes client", notes_lines),
        ("Mention finale", final_lines),
    ]
    line_height = 10.8
    rows: list[tuple[str, str]] = []

    for title, lines in sections:
        rows.append(("title", title))
        source_lines = lines or ["Aucune donnee."]
        for line in source_lines:
            wrapped = _wrap_line(f"- {line}", font_size=9.1, max_width=content_width - 2)
            for wrapped_line in wrapped:
                rows.append(("line", wrapped_line))
        rows.append(("space", ""))

    if rows and rows[-1][0] == "space":
        rows.pop()

    available_height = max(80.0, cursor_y - margin_bottom)
    max_rows = max(12, int(available_height // line_height))
    if len(rows) > max_rows:
        rows = rows[: max_rows - 2]
        rows.append(("line", "..."))
        rows.append(
            (
                "line",
                "Le devis final est valide avec le chef de projet renovation apres appel ou visite technique.",
            )
        )

    for kind, text in rows:
        if cursor_y <= margin_bottom:
            break
        if kind == "space":
            cursor_y -= line_height * 0.45
            continue
        if kind == "title":
            c.setFillColor(col_title)
            c.setFont("Helvetica-Bold", 10.1)
        else:
            c.setFillColor(col_text if "devis final est valide" not in text else col_muted)
            c.setFont("Helvetica", 9.1)
        c.drawString(margin_x, cursor_y, _pdf_ascii_text(text, limit=500))
        cursor_y -= line_height

    c.setFillColor(col_muted)
    c.setFont("Helvetica-Oblique", 8.2)
    c.drawRightString(page_width - margin_x, margin_bottom - 8, "Document indicatif - version 1 page")

    c.save()
    return buffer.getvalue()


def _build_prequote_pdf(
    *,
    prequote_number: str,
    quote: dict,
    project_type: str,
    scope: str,
    style: str,
    timeline: str,
    city: str,
    surface: str,
    rooms: str,
    budget: str,
    work_item_key: str,
    work_quantity: str,
    work_unit: str,
    notes: str,
) -> bytes:
    now_label = _utc_now().strftime("%d/%m/%Y %H:%M")
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "A confirmer")
    scope_label = SMART_SCOPE_LABELS.get(scope, scope or "A confirmer")
    budget_fit = _pdf_ascii_text((quote or {}).get("budget_fit", {}).get("message", ""), limit=200)
    summary_lines: list[str] = [
        f"Fourchette de prix (main-d'oeuvre) : {_to_eur_symbol(quote.get('low_label'))} - {_to_eur_symbol(quote.get('high_label'))}",
        f"Mention : {LABOR_ONLY_MENTION}",
        f"Base de calcul : {_pdf_ascii_text(quote.get('pricing_context') or 'Catalogue Eurobat', limit=220)}",
        f"Analyse budget : {budget_fit or 'A confirmer'}",
    ]
    context_lines: list[str] = [
        f"Type de bien : {_pdf_ascii_text(project_label, limit=120)}",
        f"Type de travaux : {_pdf_ascii_text(scope_label, limit=120)}",
        f"Ville : {_pdf_ascii_text(city, limit=120) or 'A confirmer'}",
        f"Surface : {_format_pdf_surface(surface)}",
        f"Pieces : {_pdf_ascii_text(rooms, limit=60) or 'A confirmer'}",
        f"Style : {_pdf_ascii_text(style, limit=80) or 'A confirmer'}",
        f"Echeance : {_timeline_label(timeline)}",
        f"Budget client : {_format_pdf_currency(budget)}",
    ]
    if work_item_key:
        work_item = TARIFF_BY_KEY.get(COMPAT_CODE_ALIASES.get(work_item_key, work_item_key), {})
        context_lines.append(f"Poste catalogue : {_pdf_ascii_text(work_item.get('label') or work_item_key, limit=120)}")
        quantity_text = _pdf_ascii_text(work_quantity, limit=40) or "A confirmer"
        unit_text = _pdf_ascii_text(work_unit, limit=20)
        context_lines.append(f"Quantite declaree : {quantity_text}{(' ' + unit_text) if unit_text else ''}")

    detail_lines: list[str] = []
    for item in (quote or {}).get("breakdown", []):
        label = _pdf_ascii_text(item.get("label") or "Poste", limit=140)
        detail = _pdf_ascii_text(item.get("detail") or "")
        price = f"{_to_eur_symbol(item.get('low_label'))} - {_to_eur_symbol(item.get('high_label'))}"
        if detail:
            detail_lines.append(f"{label}: {price} ({detail})")
        else:
            detail_lines.append(f"{label}: {price}")
    if not detail_lines:
        detail_lines.append("Aucun poste detaille disponible.")

    assumptions = [str(x) for x in (quote or {}).get("assumptions", []) if str(x).strip()]
    assumptions_lines: list[str] = []
    if assumptions:
        for assumption in assumptions:
            assumptions_lines.append(_pdf_ascii_text(assumption, limit=600))
    else:
        assumptions_lines.append("Aucune hypothese detaillee fournie.")

    notes_text = _pdf_ascii_text(notes, limit=2500)
    notes_lines: list[str] = []
    if notes_text:
        notes_lines.extend(_wrap_pdf_line(notes_text, max_len=92))
    else:
        notes_lines.append("Aucune note client.")

    final_lines = [
        "Ce pré-devis est généré automatiquement depuis l'estimateur.",
        "Le devis final est a valider avec le chef de projet renovation apres rendez-vous (appel ou visite technique).",
    ]

    rich_pdf = _build_prequote_pdf_reportlab(
        prequote_number=prequote_number,
        now_label=now_label,
        summary_lines=summary_lines,
        context_lines=context_lines,
        detail_lines=detail_lines,
        assumptions_lines=assumptions_lines,
        notes_lines=notes_lines,
        final_lines=final_lines,
    )
    if rich_pdf:
        return rich_pdf

    # Fallback texte si la librairie PDF premium n'est pas disponible.
    lines: list[str] = [
        "EUROBAT SERVICES",
        "PRE-DEVIS ESTIMATEUR",
        f"N° du pré-devis : {prequote_number}",
        f"Date de génération : {now_label}",
        "",
        "Résumé financier",
        *[f"- {line}" for line in summary_lines],
        "",
        "Contexte du projet",
        *[f"- {line}" for line in context_lines],
        "",
        "Détail des postes",
        *[f"- {line}" for line in detail_lines],
        "",
        "Hypothèses de calcul",
        *[f"- {line}" for line in assumptions_lines],
        "",
        "Notes client",
        *notes_lines,
        "",
        "Mention finale / document indicatif",
        *final_lines,
    ]

    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(_wrap_pdf_line(line, max_len=92))

    max_lines = 58
    if len(wrapped) > max_lines:
        wrapped = wrapped[: max_lines - 2] + [
            "...",
            "Le devis final est valide avec le chef de projet renovation apres appel ou visite technique.",
        ]
    return _build_simple_text_pdf([wrapped])


def _create_prequote_document_ref(
    *,
    quote: dict,
    project_type: str,
    scope: str,
    style: str,
    timeline: str,
    city: str,
    surface: str,
    rooms: str,
    budget: str,
    work_item_key: str,
    work_quantity: str,
    work_unit: str,
    notes: str,
) -> dict[str, str]:
    PREQUOTE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    prequote_number = _generate_prequote_number()
    pdf_bytes = _build_prequote_pdf(
        prequote_number=prequote_number,
        quote=quote,
        project_type=project_type,
        scope=scope,
        style=style,
        timeline=timeline,
        city=city,
        surface=surface,
        rooms=rooms,
        budget=budget,
        work_item_key=work_item_key,
        work_quantity=work_quantity,
        work_unit=work_unit,
        notes=notes,
    )
    token = uuid.uuid4().hex[:10]
    filename = f"predevis_{_utc_file_stamp()}_{token}.pdf"
    dst = PREQUOTE_PDF_DIR / filename
    dst.write_bytes(pdf_bytes)
    stored_name = Path("estimate") / "predevis" / filename
    generated_name = f"pre-devis-{prequote_number}.pdf"
    return {
        "label": f"{PREQUOTE_DOC_LABEL_PREFIX}:{prequote_number}|Pre-devis estimateur",
        "original_name": generated_name,
        "stored_name": stored_name.as_posix(),
        "mime_type": "application/pdf",
        "public_url": _public_static_url(dst),
    }


def _fold_lookup(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKD", raw)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    stripped = stripped.replace("_", " ")
    return re.sub(r"\s+", " ", stripped).strip()


def _lookup_key_from_value(
    raw_value: object,
    *,
    labels: dict[str, str],
    default: str,
) -> str:
    candidate = str(raw_value or "").strip()
    if candidate in labels:
        return candidate

    folded_candidate = _fold_lookup(candidate)
    if not folded_candidate:
        return default

    for key, label in labels.items():
        if folded_candidate == _fold_lookup(key):
            return key
        if folded_candidate == _fold_lookup(label):
            return key

    return default


def _extract_prequote_number(value: object) -> str:
    text = str(value or "")
    match = re.search(r"\b(PD-\d{4}-\d{4,6})\b", text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _is_prequote_document(doc: ProjectDocument | None) -> bool:
    if not doc:
        return False
    label = (doc.label or "").strip().lower()
    original = (doc.original_name or "").strip().lower()
    stored = (doc.stored_name or "").strip().lower()
    return (
        label.startswith(PREQUOTE_DOC_LABEL_PREFIX)
        or "predevis" in label
        or "pre-devis" in original
        or "predevis" in original
        or "predevis" in stored
    )


def _is_legacy_prequote_document(doc: ProjectDocument | None) -> bool:
    if not _is_prequote_document(doc):
        return False
    return not any(
        (
            _extract_prequote_number(doc.label if doc else ""),
            _extract_prequote_number(doc.original_name if doc else ""),
            _extract_prequote_number(doc.stored_name if doc else ""),
        )
    )


def _extract_estimate_range_values(raw_range: object) -> tuple[int | None, int | None]:
    text = _pdf_ascii_text(raw_range, limit=180)
    if not text:
        return None, None
    numbers = re.findall(r"\d[\d\s]{0,12}", text)
    values: list[int] = []
    for number in numbers:
        parsed = _parse_number(number)
        if parsed is not None and parsed > 0:
            values.append(int(round(parsed)))
    if len(values) >= 2:
        low, high = sorted(values[:2])
        return low, high
    return None, None


def _upgrade_legacy_prequote_document(db, doc: ProjectDocument) -> bool:
    if not _is_legacy_prequote_document(doc):
        return False

    project = (
        db.query(ClientProject)
        .filter(ClientProject.id == doc.project_id, ClientProject.client_id == doc.client_id)
        .first()
    )
    if not project:
        return False

    recap = _parse_project_summary(project.summary) or {}
    project_key = _lookup_key_from_value(
        recap.get("project_type"),
        labels=PROJECT_TYPE_LABELS,
        default="maison",
    )
    scope_key = _lookup_key_from_value(
        recap.get("scope"),
        labels=SMART_SCOPE_LABELS,
        default="renovation_complete",
    )
    style_key = str(recap.get("style") or "").strip().lower()
    if style_key not in STYLE_MULTIPLIER:
        style_key = "moderne"
    timeline_key = str(recap.get("timeline") or "").strip()
    if timeline_key not in TIMELINE_COST_MULTIPLIER:
        timeline_key = "6_mois"

    surface = str(recap.get("surface") or "")
    rooms = str(recap.get("rooms") or "")
    budget = str(recap.get("budget") or "")
    city = str(recap.get("city") or "")
    work_item_key = str(recap.get("work_item_key") or "")
    work_quantity = str(recap.get("work_quantity") or "")
    work_unit = str(recap.get("work_unit") or "")
    notes = str(recap.get("notes") or "")

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
        finishing_level=str(recap.get("finishing_level") or ""),
        work_item_key=work_item_key,
        work_quantity=work_quantity,
        work_unit=work_unit,
        require_work_item=True,
    )
    if quote.get("error"):
        low, high = _extract_estimate_range_values(recap.get("estimate_range"))
        low = low or 0
        high = high or max(low, 0)
        quote = {
            "low_label": _format_eur(low) if low else "A confirmer",
            "high_label": _format_eur(high) if high else "A confirmer",
            "pricing_context": "Catalogue Eurobat",
            "budget_fit": {"status": "unknown", "message": "Budget non analyse."},
            "breakdown": [],
            "assumptions": ["Migration automatique depuis un pre-devis legacy."],
        }

    new_doc = _create_prequote_document_ref(
        quote=quote,
        project_type=project_key,
        scope=scope_key,
        style=style_key,
        timeline=timeline_key,
        city=city,
        surface=surface,
        rooms=rooms,
        budget=budget,
        work_item_key=work_item_key,
        work_quantity=work_quantity,
        work_unit=work_unit,
        notes=notes,
    )

    old_path = _resolve_document_path(doc.stored_name)
    doc.label = new_doc["label"]
    doc.original_name = new_doc["original_name"]
    doc.stored_name = new_doc["stored_name"]
    doc.mime_type = new_doc.get("mime_type")
    project.updated_at = _utc_now()
    db.commit()
    db.refresh(doc)

    if old_path and old_path.exists():
        try:
            old_path.unlink()
        except OSError:
            pass

    return True


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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


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
    host = os.getenv("SMTP_HOST", "smtp.office365.com").strip()
    port_raw = os.getenv("SMTP_PORT", "587").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 587

    from_email = os.getenv("SMTP_FROM_EMAIL", DEFAULT_SMTP_FROM_EMAIL).strip()
    smtp_user = os.getenv("SMTP_USER", from_email).strip()
    reply_to = os.getenv("SMTP_REPLY_TO", from_email).strip()

    return {
        "host": host,
        "port": port,
        "user": smtp_user,
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "from_name": os.getenv("SMTP_FROM_NAME", DEFAULT_SMTP_FROM_NAME).strip(),
        "from_email": from_email,
        "reply_to": reply_to,
        "ssl": _env_bool("SMTP_SSL", False),
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
    attachments: list[dict[str, object]] | None = None,
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
    if cfg.get("reply_to"):
        msg["Reply-To"] = cfg["reply_to"]
    msg["To"] = recipient
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for attachment in attachments or []:
        filename = _clean_text(attachment.get("filename"), limit=180) or "document.bin"
        content = attachment.get("content")
        if not isinstance(content, (bytes, bytearray)):
            continue
        mime_type = str(attachment.get("mime_type") or "").strip().lower()
        guessed_mime, _ = mimetypes.guess_type(filename)
        resolved_mime = mime_type or guessed_mime or "application/octet-stream"
        if "/" in resolved_mime:
            maintype, subtype = resolved_mime.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(bytes(content), maintype=maintype, subtype=subtype, filename=filename)

    try:
        if cfg.get("ssl"):
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=25) as server:
                if cfg["user"] and cfg["password"]:
                    server.login(cfg["user"], cfg["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=25) as server:
                if cfg["starttls"]:
                    server.starttls()
                if cfg["user"] and cfg["password"]:
                    server.login(cfg["user"], cfg["password"])
                server.send_message(msg)
        return True, None
    except (smtplib.SMTPException, OSError) as exc:
        return False, str(exc)


def _is_email_address(value: str) -> bool:
    candidate = (value or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", candidate))


def _compose_smtp_probe_email(*, initiated_by_email: str) -> tuple[str, str]:
    cfg = _smtp_settings()
    now_label = _utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    from_header = formataddr((cfg.get("from_name") or "", cfg.get("from_email") or ""))
    reply_to = (cfg.get("reply_to") or "").strip() or "(not set)"
    smtp_user = (cfg.get("user") or "").strip() or "(not set)"
    envelope_from = (cfg.get("from_email") or "").strip() or "(not set)"

    subject = "Test SMTP EUROBAT - Verification headers"
    lines = [
        "Test d'envoi SMTP EUROBAT SERVICES",
        "",
        f"- Date: {now_label}",
        f"- Declenche par: {initiated_by_email or 'admin'}",
        "",
        "Valeurs attendues sur ce message:",
        f"- Header From: {from_header}",
        f"- Header Reply-To: {reply_to}",
        f"- Envelope MAIL FROM (app): {envelope_from}",
        f"- SMTP_USER (auth): {smtp_user}",
        "",
        "Controle visuel a faire dans la boite de reception:",
        "- Verifier l'affichage du From.",
        "- Verifier le Reply-To.",
        "- Verifier le Return-Path / source du message.",
        "",
        "Note:",
        "Le Return-Path peut etre re-ecrit par le serveur SMTP selon sa politique.",
        "S'il reste technique (ex: root@...), il faut aligner le compte SMTP du domaine.",
    ]
    return subject, "\n".join(lines)


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
        f"- Base de calcul: {(quote or {}).get('pricing_context', 'Catalogue Eurobat')}",
        "",
        "Postes budgetaires",
    ]

    for item in (quote or {}).get("breakdown", [])[:8]:
        detail = item.get("detail")
        if detail:
            lines.append(
                f"- {item.get('label', 'Poste')}: {item.get('low_label', '?')} - {item.get('high_label', '?')} ({detail})"
            )
        else:
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
            "- Un chef de projet renovation vous contacte pour valider les points techniques.",
            "- Le devis final est valide apres rendez-vous (appel ou visite technique).",
            "",
            f"Equipe EUROBAT SERVICES\n{PUBLIC_BASE_URL}",
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
    prequote_url: str = "",
    has_pdf_attachment: bool = False,
) -> tuple[str, str]:
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "projet")
    scope_label = SMART_SCOPE_LABELS.get(scope, SMART_SCOPE_LABELS["renovation_complete"])
    client_name = (name or "").strip() or "Bonjour"
    normalized_prequote_url = _to_public_link(prequote_url) if prequote_url else ""
    signup_url = _to_public_link("/signup?next=/dashboard")

    subject = "Votre pre-devis EUROBAT SERVICES est pret"
    lines = [
        f"{client_name},",
        "",
        "Votre pre-devis est pret.",
        "",
        "Synthese projet",
        f"- Type: {project_label}",
        f"- Perimetre: {scope_label}",
        f"- Style: {(style or 'moderne').capitalize()}",
        f"- Ville: {city or 'A confirmer'}",
        f"- Budget estime: {(quote or {}).get('low_label', 'A confirmer')} - {(quote or {}).get('high_label', 'A confirmer')}",
        f"- Base de calcul: {(quote or {}).get('pricing_context', 'Catalogue Eurobat')}",
    ]

    if normalized_prequote_url:
        lines.extend(
            [
                "",
                "Telecharger le pre-devis (PDF):",
                normalized_prequote_url,
            ]
        )
    if has_pdf_attachment:
        lines.extend(
            [
                "",
                "Piece jointe:",
                "- Le pre-devis PDF est joint a cet email.",
            ]
        )

    lines.extend(["", "Postes budgetaires"])

    for item in (quote or {}).get("breakdown", [])[:8]:
        detail = item.get("detail")
        if detail:
            lines.append(
                f"- {item.get('label', 'Poste')}: {item.get('low_label', '?')} - {item.get('high_label', '?')} ({detail})"
            )
        else:
            lines.append(
                f"- {item.get('label', 'Poste')}: {item.get('low_label', '?')} - {item.get('high_label', '?')}"
            )

    lines.extend(
        [
            "",
            "Prochaine etape conseillee",
            "- Repondez a cet email pour planifier un echange de 10 minutes.",
            "- Le devis final est a valider avec le chef de projet renovation apres rendez-vous (appel ou visite technique).",
            "",
            "Creer votre espace client (optionnel):",
            signup_url,
            "",
            f"Equipe EUROBAT SERVICES\n{PUBLIC_BASE_URL}",
        ]
    )

    return subject, "\n".join(lines)


def _compose_client_followup_email(
    *,
    name: str,
    project_type: str,
    quote: dict,
    prequote_url: str,
) -> tuple[str, str]:
    client_name = (name or "").strip() or "Bonjour"
    project_label = PROJECT_TYPE_LABELS.get(project_type, project_type or "projet")
    low_label = str((quote or {}).get("low_label") or "").strip()
    high_label = str((quote or {}).get("high_label") or "").strip()
    budget_range = f"{low_label} - {high_label}" if low_label and high_label else "A confirmer"
    normalized_prequote_url = _to_public_link(prequote_url) if prequote_url else ""
    signup_url = _to_public_link("/signup?next=/dashboard")

    subject = "Votre pre-devis est toujours disponible"
    lines = [
        f"{client_name},",
        "",
        "Nous vous avons envoye votre pre-devis.",
        f"- Projet: {project_label}",
        f"- Estimation: {budget_range}",
    ]
    if normalized_prequote_url:
        lines.extend(["", "Consulter le pre-devis PDF:", normalized_prequote_url])

    lines.extend(
        [
            "",
            "Creer votre espace client (optionnel):",
            signup_url,
            "",
            "Souhaitez-vous un ajustement rapide selon votre budget reel ?",
            "Repondez simplement a cet email.",
            "",
            f"Equipe EUROBAT SERVICES\n{PUBLIC_BASE_URL}",
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
            f"Equipe EUROBAT SERVICES\n{PUBLIC_BASE_URL}",
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
        f"- Base de calcul: {(quote or {}).get('pricing_context', 'Catalogue Eurobat')}",
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


def _format_catalog_quantity(value: object, unit: str) -> str:
    amount = _parse_number(value)
    if amount is None:
        return "Forfait"
    if float(amount).is_integer():
        amount_label = str(int(round(amount)))
    else:
        amount_label = f"{amount:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{amount_label} {unit}".strip()


def _catalog_quote_lines(
    *,
    scope: str,
    surface: str,
    work_item_key: str,
    work_quantity: str,
    require_work_item: bool = False,
) -> tuple[list[dict[str, object]] | None, str | None]:
    scope_key = _normalize_scope_key(scope, default_if_empty="")
    if not scope_key:
        return None, "Type de travaux invalide. Selectionnez une option proposee."

    selected_work_item = (work_item_key or "").strip()
    if selected_work_item:
        catalog_code = COMPAT_CODE_ALIASES.get(selected_work_item, selected_work_item)
        item = TARIFF_BY_KEY.get(catalog_code)
        if not item:
            return None, "Poste de travaux invalide dans le catalogue."
        if str(item.get("unit") or "") == "forfait":
            return [{"code": catalog_code}], None

        quantity = _parse_number(work_quantity)
        if quantity is None or quantity <= 0:
            return None, f"Renseignez une quantite valide pour le poste {item['label']}."
        return [{"code": catalog_code, "quantity": quantity}], None

    if require_work_item:
        return None, "Selectionnez un poste de travaux dans la grille catalogue pour lancer l'estimation."

    if scope_key != WORK_ITEM_ONLY_SCOPE:
        catalog_code = SCOPE_TO_CODE.get(scope_key)
        item = TARIFF_BY_KEY.get(catalog_code or "")
        if not item:
            return None, "Type de travaux introuvable dans le catalogue."

        quantity = _parse_number(surface)
        if quantity is None or quantity <= 0:
            return None, "Renseignez la surface a renover (m2) pour calculer cette estimation globale."
        return [{"code": catalog_code, "quantity": quantity}], None

    return None, "Selectionnez un poste de travaux dans la grille catalogue pour ce mode."


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
    require_work_item: bool = False,
) -> dict:
    scope_key = _normalize_scope_key(scope, default_if_empty="")
    if not scope_key:
        return {"error": "Type de travaux invalide. Selectionnez une option proposee."}
    project_key = project_type if project_type in PROJECT_TYPE_MULTIPLIER else "maison"
    room_count = int(_parse_number(rooms) or 0)
    room_count = max(0, min(60, room_count))
    budget_value = _parse_number(budget)
    surface_value = _parse_number(surface)

    quote_lines, quote_error = _catalog_quote_lines(
        scope=scope_key,
        surface=surface,
        work_item_key=work_item_key,
        work_quantity=work_quantity,
        require_work_item=require_work_item,
    )
    if quote_error or not quote_lines:
        return {"error": quote_error or CATALOG_ESTIMATE_ERROR["error"]}

    catalog_result = estimate_catalog_lines(quote_lines)
    if catalog_result.get("error"):
        return {"error": CATALOG_ESTIMATE_ERROR["error"]}

    low = int(round(float(catalog_result["total_min_ht"])))
    high = int(round(float(catalog_result["total_max_ht"])))

    breakdown = []
    for line in catalog_result.get("lines", []):
        code = str(line.get("code") or "")
        item = TARIFF_BY_KEY.get(code, {})
        unit = str(line.get("unit") or item.get("unit") or "")
        quantity = line.get("quantity")
        unit_price_low = int(round(float(line.get("unit_price_min") or 0)))
        unit_price_high = int(round(float(line.get("unit_price_max") or 0)))
        if quantity is None:
            detail = f"Forfait catalogue • {_format_eur(unit_price_low)} - {_format_eur(unit_price_high)}"
        else:
            detail = (
                f"Quantite: {_format_catalog_quantity(quantity, unit)} • "
                f"Prix catalogue: {_format_eur(unit_price_low)} - {_format_eur(unit_price_high)} / {unit}"
            )
        breakdown.append(
            {
                "label": str(item.get("label") or code),
                "code": code,
                "detail": detail,
                "low": int(round(float(line.get("line_total_min") or 0))),
                "high": int(round(float(line.get("line_total_max") or 0))),
                "low_label": _format_eur(int(round(float(line.get("line_total_min") or 0)))),
                "high_label": _format_eur(int(round(float(line.get("line_total_max") or 0)))),
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

    primary_line = breakdown[0] if breakdown else {}
    quantity_hint = ""
    if primary_line:
        raw_quantity = catalog_result.get("lines", [{}])[0].get("quantity")
        raw_unit = str(catalog_result.get("lines", [{}])[0].get("unit") or "")
        if raw_quantity is None:
            quantity_hint = "Forfait"
        else:
            quantity_hint = _format_catalog_quantity(raw_quantity, raw_unit)

    pricing_context_parts = ["Catalogue Eurobat"]
    if primary_line.get("label"):
        pricing_context_parts.append(str(primary_line["label"]))
    if quantity_hint:
        pricing_context_parts.append(quantity_hint)

    assumptions = [
        "Calcul strictement base sur le catalogue Eurobat.",
        "Aucun coefficient type de bien, style, delai, nombre de pieces ou complexite n'est applique.",
        "Montant calcule uniquement a partir du code catalogue et de sa quantite.",
    ]
    if primary_line.get("code"):
        assumptions.append(
            f"Code catalogue utilise: {primary_line['code']} ({primary_line.get('label', primary_line['code'])})."
        )
    if require_work_item:
        assumptions.append("Le type de travaux sert de cadrage et n'influence pas le calcul de l'estimation.")
        assumptions.append("Estimation calculee uniquement sur le poste catalogue selectionne.")
    elif scope_key == WORK_ITEM_ONLY_SCOPE:
        assumptions.append("Mode choix par prestation: estimation ciblee calculee uniquement sur le poste catalogue selectionne.")
    else:
        assumptions.append(
            f"Mode estimation globale: le type de travaux est mappe sur le code catalogue {SCOPE_TO_CODE.get(scope_key, '')}."
        )

    estimate_mode = "item_targeted" if (require_work_item or scope_key == WORK_ITEM_ONLY_SCOPE) else "project_global"
    estimate_mode_label = "Devis cible par prestation" if estimate_mode == "item_targeted" else "Pre-devis global du projet"

    return {
        "pricing_basis": "catalog",
        "estimate_mode": estimate_mode,
        "estimate_mode_label": estimate_mode_label,
        "pricing_context": " • ".join(part for part in pricing_context_parts if part),
        "project_type_label": PROJECT_TYPE_LABELS.get(project_key, project_key),
        "scope_label": SMART_SCOPE_LABELS.get(scope_key, scope_key),
        "surface_m2": round(surface_value or 0.0, 1),
        "rooms": room_count or None,
        "low": low,
        "high": high,
        "low_label": _format_eur(low),
        "high_label": _format_eur(high),
        "budget_fit": budget_fit,
        "breakdown": breakdown,
        "catalog_lines": catalog_result.get("lines", []),
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


def _parse_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _record_quote_email_sent(
    *,
    handoff_id: int | None,
    prequote_url: str,
    project_type: str,
    quote: dict,
) -> None:
    if not handoff_id:
        return
    db = SessionLocal()
    try:
        handoff = db.query(HandoffRequest).filter(HandoffRequest.id == handoff_id).first()
        if not handoff:
            return
        payload = _extract_handoff_conversation_payload(handoff)
        now = _utc_now()
        payload["quote_email_sent_at"] = now.isoformat()
        payload["followup_j1_due_at"] = (now + timedelta(days=1)).isoformat()
        payload["project_type"] = payload.get("project_type") or project_type
        payload["quote"] = payload.get("quote") or quote
        if prequote_url:
            payload["prequote_url"] = _to_public_link(prequote_url)
        handoff.conversation = json.dumps(payload, ensure_ascii=False)
        db.commit()
    finally:
        db.close()


def _process_quote_followup_j1() -> int:
    if not _env_bool("EMAIL_REMINDER_J1_ENABLED", True):
        return 0
    if not _smtp_ready(_smtp_settings()):
        return 0

    now = _utc_now()
    lookback_days = max(1, _env_int("EMAIL_REMINDER_LOOKBACK_DAYS", 10))
    min_created_at = now - timedelta(days=lookback_days)
    sent_count = 0

    db = SessionLocal()
    try:
        candidates = (
            db.query(HandoffRequest)
            .filter(HandoffRequest.source == "devis_intelligent")
            .filter(HandoffRequest.email.isnot(None))
            .filter(HandoffRequest.created_at >= min_created_at)
            .order_by(HandoffRequest.created_at.asc())
            .all()
        )
        for handoff in candidates:
            recipient = (handoff.email or "").strip().lower()
            if not _is_email_address(recipient):
                continue
            if str(handoff.status or "").strip().lower() not in {"", "new", "pending"}:
                continue

            payload = _extract_handoff_conversation_payload(handoff)
            if payload.get("followup_j1_sent_at"):
                continue

            quote_sent_at = _parse_iso_datetime(payload.get("quote_email_sent_at")) or _as_utc(handoff.created_at)
            if not quote_sent_at:
                continue
            due_at = _parse_iso_datetime(payload.get("followup_j1_due_at")) or (quote_sent_at + timedelta(days=1))
            if due_at > now:
                continue

            project_type = str(payload.get("project_type") or "")
            quote_payload = payload.get("quote")
            quote = quote_payload if isinstance(quote_payload, dict) else {}
            prequote_url = str(payload.get("prequote_url") or "").strip()

            subject, body = _compose_client_followup_email(
                name=str(handoff.name or ""),
                project_type=project_type,
                quote=quote,
                prequote_url=prequote_url,
            )
            sent, error = _send_email_message(to_email=recipient, subject=subject, text_body=body)

            attempts = int(payload.get("followup_j1_attempts") or 0) + 1
            payload["followup_j1_attempts"] = attempts
            if sent:
                payload["followup_j1_sent_at"] = now.isoformat()
                payload.pop("followup_j1_last_error", None)
                sent_count += 1
            else:
                payload["followup_j1_last_error"] = _clean_text(error or "smtp_error", limit=220)
            handoff.conversation = json.dumps(payload, ensure_ascii=False)
            db.commit()
    finally:
        db.close()

    return sent_count


async def _quote_followup_j1_worker() -> None:
    interval_seconds = max(60, _env_int("EMAIL_REMINDER_POLL_SECONDS", 900))
    while True:
        _process_quote_followup_j1()
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ARCHITECTURE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ARCHITECTURE_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    ESTIMATE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PREQUOTE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    Base.metadata.create_all(bind=engine)
    _ensure_admin_user()
    reminder_task: asyncio.Task | None = None
    if _env_bool("EMAIL_REMINDER_J1_ENABLED", True):
        reminder_task = asyncio.create_task(_quote_followup_j1_worker())
    try:
        yield
    finally:
        if reminder_task:
            reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await reminder_task


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
templates.env.globals["ESTIMATE_WORK_ITEM_GROUPS"] = ESTIMATE_WORK_ITEM_GROUPS
templates.env.globals["get_current_user"] = _get_current_user
templates.env.globals["humanize"] = _humanize
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


@app.head("/", include_in_schema=False)
def home_head():
    return Response(status_code=200)


@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
def health_check():
    return Response(status_code=200)


@app.get("/favicon.ico", include_in_schema=False)
@app.head("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(
        path=str(STATIC_DIR / "branding" / "eurobat-services.png"),
        media_type="image/png",
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


@app.get("/chantiers/renovation-salle-de-bain-paris", response_class=HTMLResponse)
@app.get("/chantiers/renovation-salle-de-bain-paris/", response_class=HTMLResponse)
def chantier_sdb_paris(request: Request):
    return templates.TemplateResponse(request, "chantier_sdb_paris.html")


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
        user_role = user.role
        token = _create_session(db, user.id)
    finally:
        db.close()

    redirect_target = next
    if not redirect_target:
        redirect_target = "/admin/dashboard" if user_role == "admin" else "/dashboard"
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
        return RedirectResponse("/admin/dashboard", status_code=303)

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


def _latest_handoff_for_user(db, user: dict) -> HandoffRequest | None:
    email = str(user.get("email") or "").strip().lower()
    phone = str(user.get("phone") or "").strip()
    query = db.query(HandoffRequest)
    if email and phone:
        query = query.filter((HandoffRequest.email == email) | (HandoffRequest.phone == phone))
    elif email:
        query = query.filter(HandoffRequest.email == email)
    elif phone:
        query = query.filter(HandoffRequest.phone == phone)
    else:
        return None
    return query.order_by(HandoffRequest.created_at.desc()).first()


def _latest_prequote_reference_for_user(db, user_id: int) -> str:
    docs = (
        db.query(ProjectDocument)
        .filter(ProjectDocument.client_id == user_id)
        .order_by(ProjectDocument.created_at.desc())
        .limit(40)
        .all()
    )
    for doc in docs:
        if not _is_prequote_document(doc):
            continue
        ref = _extract_prequote_number(doc.label) or _extract_prequote_number(doc.original_name)
        if ref:
            return ref
    return ""


def _build_chantier_seed_labels(handoff: HandoffRequest | None) -> list[str]:
    _ = handoff
    return [
        "Projet enregistre",
        "Preparation du chantier",
        "Intervention planifiee",
        "Chantier demarre",
        "Travaux en cours",
        "Verification finale",
        "Chantier termine",
        "Reception client",
        "Projet cloture",
    ]


def _parse_chantier_form_date(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed_date = None
    try:
        parsed_date = datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            parsed_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None
    return datetime(parsed_date.year, parsed_date.month, parsed_date.day, 12, 0, tzinfo=UTC)


def _format_chantier_date(value: datetime | None) -> str:
    normalized = _as_utc(value)
    if not normalized:
        return ""
    return normalized.strftime("%d/%m/%Y")


def _format_chantier_input_date(value: datetime | None) -> str:
    normalized = _as_utc(value)
    if not normalized:
        return ""
    return normalized.strftime("%Y-%m-%d")


def _extract_client_comment(detail_text: str) -> str:
    marker = "Commentaire client:"
    if marker not in (detail_text or ""):
        return ""
    return _clean_text(str(detail_text).split(marker, 1)[1], limit=500)


def _step_label_from_event_title(title: str) -> str:
    text = str(title or "")
    if ":" not in text:
        return ""
    return _clean_text(text.split(":", 1)[1], limit=180)


def _first_pending_or_blocked_step_label(steps: list[dict]) -> str:
    for step in steps:
        status = _normalize_chantier_step_status(str(step.get("status") or ""))
        if status in {"pending", "blocked", "delayed"}:
            return str(step.get("label") or "")
    return ""


def _compute_chantier_overview(steps: list[dict]) -> tuple[int, str, list[str], list[str]]:
    if not steps:
        return 0, "", [], []

    completed_steps = [str(step.get("label") or "") for step in steps if step.get("status") == "validated"]
    current_idx = -1
    for idx, step in enumerate(steps):
        if step.get("status") in {"in_progress", "blocked", "delayed"}:
            current_idx = idx
            break
    if current_idx < 0:
        for idx, step in enumerate(steps):
            if step.get("status") == "pending":
                current_idx = idx
                break
    current_label = str(steps[current_idx].get("label") or "") if current_idx >= 0 else str(steps[-1].get("label") or "")

    total_steps = len(steps)
    completed_count = len(completed_steps)
    if completed_count >= total_steps:
        return (
            100,
            current_label,
            completed_steps,
            [],
        )

    current_progress = 0
    if current_idx >= 0:
        current_status = str(steps[current_idx].get("status") or "")
        if current_status not in {"pending", "validated"}:
            current_progress = max(0, min(100, int(steps[current_idx].get("progress_percent") or 0)))
    global_progress = int(round(((completed_count + (current_progress / 100.0)) / float(total_steps)) * 100))
    global_progress = max(0, min(99, global_progress))

    upcoming_steps: list[str] = []
    if current_idx >= 0:
        for step in steps[current_idx + 1:]:
            if step.get("status") != "validated":
                upcoming_steps.append(str(step.get("label") or ""))
            if len(upcoming_steps) >= 3:
                break

    return global_progress, current_label, completed_steps, upcoming_steps


def _normalize_chantier_step_status(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"pending", "planifie", "planned"}:
        return "pending"
    if raw in {"in_progress", "en_cours", "en cours", "running"}:
        return "in_progress"
    if raw in {"blocked", "bloque", "blockage"}:
        return "blocked"
    if raw in {"delayed", "delay", "retard"}:
        return "delayed"
    if raw in {"validated", "complete", "completed", "termine", "done"}:
        return "validated"
    return "pending"


def _chantier_step_status_label(status: str) -> str:
    labels = {
        "pending": "Planifie",
        "in_progress": "En cours",
        "blocked": "Bloque",
        "delayed": "En retard",
        "validated": "Termine",
    }
    return labels.get(_normalize_chantier_step_status(status), "Planifie")


def _seed_chantier_contract_data(db, contract: ChantierContract, handoff: HandoffRequest | None) -> None:
    now = _utc_now()
    step_labels = _build_chantier_seed_labels(handoff)
    for idx, label in enumerate(step_labels):
        start = now + timedelta(days=idx * 4)
        status = "pending"
        progress = 0
        next_step = "Etape planifiee."
        if idx == 0:
            status = "validated"
            progress = 100
            next_step = "Etape terminee."
        elif idx == 1:
            status = "in_progress"
            progress = 20
            next_step = "Mise en place de la preparation chantier."
        step = ChantierLot(
            contract_id=contract.id,
            code=f"step_{idx + 1}",
            label=label,
            status=status,
            progress_percent=progress,
            next_step=next_step,
            planned_start=start,
            planned_end=(now if idx == 0 else None),
            sort_order=idx,
        )
        db.add(step)

    db.add(
        ChantierEvent(
            contract_id=contract.id,
            event_type="signature",
            title="Devis signe - chantier active",
            detail="Le suivi chantier client est active sur les grandes etapes du projet.",
            impact_timeline="Plan de suivi initialise",
            impact_scope="client",
        )
    )
    db.add(
        ChantierEvent(
            contract_id=contract.id,
            event_type="planning",
            title="Grandes etapes publiees",
            detail="Les etapes sont mises a jour par EUROBAT depuis le CRM.",
            impact_scope="client",
        )
    )


def _sync_lot_progress_state(db, lot_id: int) -> None:
    step = db.query(ChantierLot).filter(ChantierLot.id == lot_id).first()
    if not step:
        return
    progress = max(0, min(100, int(step.progress_percent or 0)))
    current_status = _normalize_chantier_step_status(step.status or "")
    if progress >= 100:
        step.status = "validated"
        step.progress_percent = 100
        step.next_step = "Etape terminee."
    elif progress <= 0:
        step.status = "pending"
        step.progress_percent = 0
        step.next_step = step.next_step or "Etape planifiee."
    else:
        if current_status in {"blocked", "delayed"}:
            step.status = current_status
        else:
            step.status = "in_progress"
        step.progress_percent = progress
        step.next_step = step.next_step or "Execution en cours."
    step.updated_at = _utc_now()


def _promote_next_milestone_for_lot(db, lot_id: int) -> None:
    _ = db
    _ = lot_id


def _sync_contract_status(db, contract: ChantierContract) -> None:
    steps = db.query(ChantierLot).filter(ChantierLot.contract_id == contract.id).all()
    if not steps:
        contract.status = "active"
        contract.updated_at = _utc_now()
        return
    completed = 0
    blocked = False
    for step in steps:
        status = _normalize_chantier_step_status(step.status or "")
        progress = int(step.progress_percent or 0)
        if status in {"blocked", "delayed"}:
            blocked = True
        if status == "validated" or progress >= 100:
            completed += 1
    if completed == len(steps):
        contract.status = "completed"
    elif blocked:
        contract.status = "blocked"
    else:
        contract.status = "active"
    contract.updated_at = _utc_now()


@app.get("/dashboard/chantier", response_class=HTMLResponse)
@app.get("/dashboard/chantier/", response_class=HTMLResponse)
def dashboard_chantier(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/chantier", status_code=303)
    if user.get("role") == "admin":
        return RedirectResponse("/admin", status_code=303)

    state = request.query_params.get("state", "").strip().lower()
    state_messages = {
        "signed": ("success", "Devis signe. Votre suivi chantier est actif."),
        "already_active": ("info", "Un chantier actif existe deja sur votre espace."),
        "terms_required": ("error", "Confirmez l'acceptation du devis avant signature."),
        "missing_signer": ("error", "Renseignez le nom du signataire."),
        "crm_managed": ("info", "Le suivi est mis a jour par EUROBAT depuis le CRM."),
    }
    flash = state_messages.get(state)

    db = SessionLocal()
    try:
        contract = (
            db.query(ChantierContract)
            .filter(
                ChantierContract.client_id == user["id"],
                ChantierContract.status.in_(["active", "completed", "blocked"]),
            )
            .order_by(ChantierContract.signed_at.desc(), ChantierContract.created_at.desc())
            .first()
        )
        latest_project = (
            db.query(ClientProject)
            .filter(ClientProject.client_id == user["id"])
            .order_by(ClientProject.updated_at.desc(), ClientProject.created_at.desc())
            .first()
        )
        prequote_reference = _latest_prequote_reference_for_user(db, int(user["id"]))

        steps_view: list[dict] = []
        events_view: list[dict] = []
        comments_view: list[dict] = []
        contract_summary: dict | None = None
        last_update_label = ""

        if contract:
            contract_project = (
                db.query(ClientProject).filter(ClientProject.id == contract.project_id).first()
                if contract.project_id
                else latest_project
            )
            steps = (
                db.query(ChantierLot)
                .filter(ChantierLot.contract_id == contract.id)
                .order_by(ChantierLot.sort_order.asc(), ChantierLot.id.asc())
                .all()
            )
            for step in steps:
                progress = max(0, min(100, int(step.progress_percent or 0)))
                status = _normalize_chantier_step_status(step.status or "")
                if progress >= 100:
                    status = "validated"
                steps_view.append(
                    {
                        "id": step.id,
                        "label": step.label,
                        "status": status,
                        "status_label": _chantier_step_status_label(status),
                        "progress_percent": progress,
                        "next_step": step.next_step or "Mise a jour a venir.",
                        "planned_date_label": _format_chantier_date(step.planned_start),
                        "actual_date_label": _format_chantier_date(step.planned_end),
                        "client_comment": "",
                    }
                )

            events = (
                db.query(ChantierEvent)
                .filter(ChantierEvent.contract_id == contract.id)
                .order_by(ChantierEvent.created_at.desc(), ChantierEvent.id.desc())
                .limit(30)
                .all()
            )
            for event in events:
                visibility = str(event.impact_scope or "").strip().lower()
                if visibility == "internal":
                    continue
                event_type = str(event.event_type or "").strip().lower()
                if event_type in {"signature", "planning"}:
                    continue
                detail_text = event.detail or ""
                events_view.append(
                    {
                        "title": event.title,
                        "detail": detail_text,
                        "impact_timeline": event.impact_timeline or "",
                        "impact_scope": event.impact_scope or "",
                        "created_label": event.created_at.strftime("%d/%m/%Y %H:%M") if event.created_at else "",
                    }
                )
                marker = "Commentaire client:"
                if marker in detail_text:
                    comment_text = _extract_client_comment(detail_text)
                    step_label = _step_label_from_event_title(event.title or "")
                    if comment_text:
                        comments_view.append(
                            {
                                "title": event.title,
                                "detail": comment_text,
                                "created_label": event.created_at.strftime("%d/%m/%Y %H:%M") if event.created_at else "",
                            }
                        )
                    if step_label and comment_text:
                        for step_row in steps_view:
                            if (
                                _fold_lookup(step_row.get("label")) == _fold_lookup(step_label)
                                and not str(step_row.get("client_comment") or "").strip()
                            ):
                                step_row["client_comment"] = comment_text
                                break
                elif event_type == "incident" and detail_text:
                    comments_view.append(
                        {
                            "title": event.title,
                            "detail": detail_text,
                            "created_label": event.created_at.strftime("%d/%m/%Y %H:%M") if event.created_at else "",
                        }
                    )
            if events_view:
                last_update_label = events_view[0]["created_label"]
            elif contract.updated_at:
                last_update_label = contract.updated_at.strftime("%d/%m/%Y %H:%M")

            global_progress, current_step_label, completed_steps, upcoming_steps = _compute_chantier_overview(steps_view)
            next_step_label = upcoming_steps[0] if upcoming_steps else ""
            if not next_step_label:
                next_step_label = _first_pending_or_blocked_step_label(steps_view)
            status_label_map = {
                "active": "En cours",
                "completed": "Termine",
                "blocked": "Bloque",
            }

            contract_summary = {
                "id": contract.id,
                "status": contract.status or "active",
                "status_label": status_label_map.get(str(contract.status or "").lower(), "En cours"),
                "signed_label": contract.signed_at.strftime("%d/%m/%Y %H:%M") if contract.signed_at else "",
                "quote_reference": contract.quote_reference or prequote_reference or "A confirmer",
                "signer_name": contract.signer_name,
                "last_update_label": last_update_label,
                "global_progress": global_progress,
                "current_step_label": current_step_label,
                "next_step_label": next_step_label,
                "completed_steps_count": len(completed_steps),
                "total_steps": len(steps_view),
                "project_name": (contract_project.title if contract_project else "Projet chantier"),
            }
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "dashboard_chantier.html",
        {
            "user": user,
            "hide_public_header": True,
            "flash": flash,
            "contract": contract_summary,
            "steps": steps_view,
            "events": events_view,
            "comments": comments_view[:10],
            "completed_steps": (completed_steps if contract_summary else []),
            "upcoming_steps": (upcoming_steps if contract_summary else []),
            "latest_project": latest_project,
            "prequote_reference": prequote_reference,
        },
    )


@app.post("/dashboard/chantier/sign-devis")
def dashboard_chantier_sign_devis(
    request: Request,
    signer_name: str = Form(""),
    accept_terms: str = Form(""),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/chantier", status_code=303)
    if user.get("role") == "admin":
        return RedirectResponse("/admin", status_code=303)

    signer = _clean_text(signer_name, limit=120) or _clean_text(user.get("name"), limit=120)
    if not signer:
        return RedirectResponse("/dashboard/chantier?state=missing_signer", status_code=303)
    if str(accept_terms or "").strip().lower() not in {"1", "true", "yes", "on", "oui"}:
        return RedirectResponse("/dashboard/chantier?state=terms_required", status_code=303)

    db = SessionLocal()
    try:
        existing = (
            db.query(ChantierContract)
            .filter(
                ChantierContract.client_id == user["id"],
                ChantierContract.status.in_(["active", "completed", "blocked"]),
            )
            .order_by(ChantierContract.signed_at.desc(), ChantierContract.id.desc())
            .first()
        )
        if existing:
            return RedirectResponse("/dashboard/chantier?state=already_active", status_code=303)

        latest_project = (
            db.query(ClientProject)
            .filter(ClientProject.client_id == user["id"])
            .order_by(ClientProject.updated_at.desc(), ClientProject.created_at.desc())
            .first()
        )
        handoff = _latest_handoff_for_user(db, user)
        quote_reference = _latest_prequote_reference_for_user(db, int(user["id"]))
        contract = ChantierContract(
            client_id=user["id"],
            project_id=(latest_project.id if latest_project else None),
            status="active",
            signed_at=_utc_now(),
            signer_name=signer,
            signer_email=str(user.get("email") or "").strip().lower(),
            signer_ip=(request.client.host if request.client else None),
            quote_reference=quote_reference or None,
            terms_version="v1",
            updated_at=_utc_now(),
        )
        db.add(contract)
        db.flush()

        _seed_chantier_contract_data(db, contract, handoff)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/dashboard/chantier?state=signed", status_code=303)


@app.post("/dashboard/chantier/validate-milestone")
def dashboard_chantier_validate_milestone(
    request: Request,
    milestone_id: int = Form(...),
    validation_note: str = Form(""),
):
    _ = milestone_id
    _ = validation_note
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/chantier", status_code=303)
    return RedirectResponse("/dashboard/chantier?state=crm_managed", status_code=303)


@app.post("/dashboard/chantier/add-change")
def dashboard_chantier_add_change(
    request: Request,
    change_title: str = Form(""),
    change_detail: str = Form(""),
    change_impact_timeline: str = Form(""),
    change_impact_scope: str = Form(""),
):
    _ = change_title
    _ = change_detail
    _ = change_impact_timeline
    _ = change_impact_scope
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/chantier", status_code=303)
    return RedirectResponse("/dashboard/chantier?state=crm_managed", status_code=303)


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

        upgraded_any = False
        for doc in documents:
            if _is_legacy_prequote_document(doc):
                if _upgrade_legacy_prequote_document(db, doc):
                    upgraded_any = True

        if upgraded_any:
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
            "label": (doc.label or "Document").split("|", 1)[1] if (doc.label or "").startswith("cat:") and "|" in (doc.label or "") else (doc.label or "Document"),
            "raw_label": doc.label or "Document",
            "name": doc.original_name,
            "url": f"/api/project-document/{doc.id}/open",
            "mime_type": doc.mime_type or "",
            "created_at": doc.created_at.strftime("%d/%m/%Y %H:%M") if doc.created_at else "",
            "stored_name": doc.stored_name,
            "size": size_label,
        }
        mime = (entry["mime_type"] or "").lower()
        name_lower = (entry["name"] or "").lower()
        kind = "other"
        if mime.startswith("image/") or name_lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")):
            kind = "image"
        elif mime.startswith("video/") or name_lower.endswith((".mp4", ".mov", ".webm")):
            kind = "video"
        elif "pdf" in mime or name_lower.endswith(".pdf"):
            kind = "pdf"
        entry["kind"] = kind
        docs_by_project.setdefault(doc.project_id, []).append(entry)
        docs_flat.append(entry)

    current_project = projects[0] if projects else None
    current_docs = docs_by_project.get(current_project.id, []) if current_project else []
    current_recap = project_recaps.get(current_project.id, {}) if current_project else {}

    allowed_keys = {"presentation", "plans", "technique", "inspirations", "autres"}

    def _normalize_txt(value: str) -> str:
        raw = unicodedata.normalize("NFKD", value or "")
        return "".join(ch for ch in raw if not unicodedata.combining(ch)).lower().strip()

    def _bucket(doc_label: str) -> str:
        raw = (doc_label or "").strip()
        if raw.startswith("cat:") and "|" in raw:
            key = raw.split("|", 1)[0].replace("cat:", "", 1).strip().lower()
            if key in allowed_keys:
                return key
        lbl = _normalize_txt(raw)
        if "presentation du bien" in lbl:
            return "presentation"
        if "documents techniques" in lbl:
            return "technique"
        if "autres documents" in lbl:
            return "autres"
        if any(k in lbl for k in ["photo", "video", "annonce"]):
            return "presentation"
        if any(k in lbl for k in ["plan", "3d", "dossier"]):
            return "plans"
        if any(k in lbl for k in ["dpe", "diagnostic", "audit", "calcul", "technique"]):
            return "technique"
        if any(k in lbl for k in ["inspiration", "pinterest", "mood"]):
            return "inspirations"
        return "autres"

    buckets: dict[str, list[dict]] = {"presentation": [], "plans": [], "technique": [], "inspirations": [], "autres": []}
    for doc in current_docs:
        cat = _bucket(doc.get("raw_label") or doc.get("label") or doc.get("name"))
        buckets.setdefault(cat, []).append(doc)

    return templates.TemplateResponse(
        request,
        "dashboard_documents.html",
        {
            "user": user,
            "projects": projects,
            "docs_by_project": docs_by_project,
            "docs_flat": docs_flat,
            "current_project": current_project,
            "current_recap": current_recap,
            "project_recaps": project_recaps,
            "hide_public_header": True,
            "current_docs": current_docs,
            "doc_buckets": buckets,
        },
    )


@app.get("/dashboard/pre-devis", response_class=HTMLResponse)
@app.get("/dashboard/pre-devis/", response_class=HTMLResponse)
def dashboard_prequotes(request: Request):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/pre-devis", status_code=303)
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

    project_title_by_id = {project.id: project.title for project in projects}
    project_recaps: dict[int, dict] = {}
    for project in projects:
        project_recaps[project.id] = _parse_project_summary(project.summary) or {}

    prequote_items: list[dict[str, str]] = []
    for doc in documents:
        raw_label = (doc.label or "").strip()
        raw_label_lower = raw_label.lower()
        original_name = (doc.original_name or "").strip()
        original_name_lower = original_name.lower()
        stored_name = (doc.stored_name or "").strip()
        stored_name_lower = stored_name.lower()
        mime_type = (doc.mime_type or "").strip().lower()

        looks_like_prequote = (
            raw_label_lower.startswith(PREQUOTE_DOC_LABEL_PREFIX)
            or "predevis" in raw_label_lower
            or "pre-devis" in original_name_lower
            or "predevis" in original_name_lower
            or "predevis" in stored_name_lower
        )
        is_pdf = ("pdf" in mime_type) or original_name_lower.endswith(".pdf") or stored_name_lower.endswith(".pdf")
        if not looks_like_prequote or not is_pdf:
            continue

        recap = project_recaps.get(doc.project_id) or {}
        estimate_range = _clean_text(recap.get("estimate_range"), limit=120)
        project_title = project_title_by_id.get(doc.project_id) or "Projet"
        title = original_name or Path(stored_name).name or "pre-devis.pdf"
        created_label = doc.created_at.strftime("%d/%m/%Y %H:%M") if doc.created_at else ""
        prequote_number = ""
        label_head = raw_label.split("|", 1)[0]
        label_parts = [part.strip() for part in label_head.split(":") if part.strip()]
        if len(label_parts) >= 3 and label_parts[0] == "predevis" and label_parts[1] == "estimateur":
            prequote_number = label_parts[2]
        if not prequote_number:
            match = re.search(r"(PD-\d{4}-\d{4,6})", original_name, flags=re.IGNORECASE)
            if match:
                prequote_number = match.group(1).upper()

        prequote_items.append(
            {
                "id": str(doc.id),
                "title": title,
                "prequote_number": prequote_number,
                "project_title": project_title,
                "estimate_range": estimate_range,
                "created_at": created_label,
                "url": f"/api/project-document/{doc.id}/open",
                "view_url": f"/dashboard/pre-devis/{doc.id}/view",
            }
        )

    return templates.TemplateResponse(
        request,
        "dashboard_predevis.html",
        {
            "user": user,
            "prequote_items": prequote_items,
            "hide_public_header": True,
        },
    )


@app.get("/dashboard/pre-devis/{doc_id}", response_class=HTMLResponse)
@app.get("/dashboard/pre-devis/{doc_id}/", response_class=HTMLResponse)
def dashboard_predevis_view_legacy(doc_id: int):
    return RedirectResponse(f"/dashboard/pre-devis/{doc_id}/view", status_code=307)


@app.get("/dashboard/pre-devis/{doc_id}/view", response_class=HTMLResponse)
@app.get("/dashboard/pre-devis/{doc_id}/view/", response_class=HTMLResponse)
def dashboard_predevis_view(request: Request, doc_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/pre-devis", status_code=303)

    db = SessionLocal()
    try:
        doc = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.id == doc_id, ProjectDocument.client_id == user["id"])
            .first()
        )
        if not doc or not _is_prequote_document(doc):
            return RedirectResponse("/dashboard/pre-devis?error=document_missing", status_code=303)

        if _is_legacy_prequote_document(doc):
            _upgrade_legacy_prequote_document(db, doc)
            db.refresh(doc)

        path = _resolve_document_path(doc.stored_name)
        if not path or not path.exists():
            return RedirectResponse("/dashboard/pre-devis?error=document_missing", status_code=303)

        title = (doc.original_name or "").strip() or path.name
        prequote_number = _extract_prequote_number(doc.label) or _extract_prequote_number(doc.original_name)
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "dashboard_predevis_viewer.html",
        {
            "user": user,
            "doc_id": doc_id,
            "doc_title": title,
            "prequote_number": prequote_number,
            "doc_url": f"/api/project-document/{doc_id}/open",
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


@app.post("/api/catalog-estimate")
def catalog_estimate(payload: dict = Body(...)):
    result = estimate_catalog_lines(payload.get("lines"))
    if result.get("error") == CATALOG_ESTIMATE_ERROR["error"]:
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(content=result)


def _safe_doc_suffix(filename: str, mime_type: str = "") -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".mp4", ".mov", ".webm"}:
        return suffix
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return ".jpg"
    if mime.startswith("video/"):
        return ".mp4"
    if "pdf" in mime:
        return ".pdf"
    return ".bin"


@app.post("/api/project-document")
async def upload_project_document(
    request: Request,
    project_id: int | None = Form(None),
    category_key: str = Form(""),
    label: str = Form("Document client"),
    file: list[UploadFile] = File(...),
):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/documents", status_code=303)

    uploads = [f for f in (file or []) if f and f.filename]
    if not uploads:
        return RedirectResponse("/dashboard/documents?error=missing_file", status_code=303)

    db = SessionLocal()
    try:
        safe_category = (category_key or "").strip().lower()
        if safe_category not in {"presentation", "plans", "technique", "inspirations", "autres"}:
            safe_category = ""

        project = None
        if project_id:
            project = (
                db.query(ClientProject)
                .filter(ClientProject.id == project_id, ClientProject.client_id == user["id"])
                .first()
            )
        if not project:
            project = (
                db.query(ClientProject)
                .filter(ClientProject.client_id == user["id"])
                .order_by(ClientProject.updated_at.desc(), ClientProject.created_at.desc())
                .first()
            )
        if not project:
            project = ClientProject(
                client_id=user["id"],
                title="Projet documents",
                summary="{}",
                status="Documents",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        if not project:
            return RedirectResponse("/dashboard/documents?error=invalid_project", status_code=303)

        CLIENT_DOCS_DIR.mkdir(parents=True, exist_ok=True)
        saved_any = False
        for upload in uploads:
            suffix = _safe_doc_suffix(upload.filename, upload.content_type or "")
            filename = f"client_doc_{_utc_file_stamp()}_{uuid.uuid4().hex[:8]}{suffix}"
            dst = CLIENT_DOCS_DIR / filename
            content = await upload.read()
            if not content:
                continue
            dst.write_bytes(content)
            clean_label = (label or "Document client").strip()
            stored_label = f"cat:{safe_category}|{clean_label}" if safe_category else clean_label
            db.add(
                ProjectDocument(
                    client_id=user["id"],
                    project_id=project.id,
                    label=stored_label,
                    original_name=upload.filename,
                    stored_name=filename,
                    mime_type=upload.content_type,
                )
            )
            saved_any = True
        if saved_any:
            project.updated_at = _utc_now()
            db.commit()
        else:
            return RedirectResponse("/dashboard/documents?error=empty_file", status_code=303)
    finally:
        db.close()

    return RedirectResponse("/dashboard/documents?success=uploaded", status_code=303)


@app.get("/api/project-document/{doc_id}/open")
def open_project_document(request: Request, doc_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/documents", status_code=303)

    db = SessionLocal()
    try:
        doc = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.id == doc_id, ProjectDocument.client_id == user["id"])
            .first()
        )
        if not doc:
            return RedirectResponse("/dashboard/documents?error=document_missing", status_code=303)

        # Migration transparente: conversion des anciens pre-devis texte vers le template premium.
        if _is_legacy_prequote_document(doc):
            _upgrade_legacy_prequote_document(db, doc)
            db.refresh(doc)

        path = _resolve_document_path(doc.stored_name)
        if not path or not path.exists():
            return RedirectResponse("/dashboard/documents?error=document_missing", status_code=303)

        media_type = (doc.mime_type or "").strip()
        if not media_type:
            guessed, _ = mimetypes.guess_type(str(path))
            media_type = guessed or "application/octet-stream"
    finally:
        db.close()

    return FileResponse(
        path=str(path),
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/api/project-document/delete")
def delete_project_document(request: Request, doc_id: int = Form(...)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/documents", status_code=303)

    db = SessionLocal()
    try:
        doc = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.id == doc_id, ProjectDocument.client_id == user["id"])
            .first()
        )
        if not doc:
            return RedirectResponse("/dashboard/documents?error=document_missing", status_code=303)

        path = _resolve_document_path(doc.stored_name)
        db.delete(doc)
        db.commit()

        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    finally:
        db.close()

    return RedirectResponse("/dashboard/documents?success=deleted", status_code=303)


def _delete_prequote_document_for_user(user_id: int, doc_id: int) -> bool:
    db = SessionLocal()
    try:
        doc = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.id == doc_id, ProjectDocument.client_id == user_id)
            .first()
        )
        if not doc or not _is_prequote_document(doc):
            return False

        path = _resolve_document_path(doc.stored_name)
        db.delete(doc)
        db.commit()

        if path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return True
    finally:
        db.close()


@app.post("/api/pre-devis/delete")
@app.post("/api/pre-devis/delete/")
def delete_prequote_document(request: Request, doc_id: int = Form(...)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/pre-devis", status_code=303)

    deleted = _delete_prequote_document_for_user(user["id"], doc_id)
    if not deleted:
        return RedirectResponse("/dashboard/pre-devis?error=document_missing", status_code=303)

    return RedirectResponse("/dashboard/pre-devis?success=deleted", status_code=303)


@app.post("/api/pre-devis/{doc_id}/delete")
@app.post("/api/pre-devis/{doc_id}/delete/")
def delete_prequote_document_legacy(request: Request, doc_id: int):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/pre-devis", status_code=303)

    deleted = _delete_prequote_document_for_user(user["id"], doc_id)
    if not deleted:
        return RedirectResponse("/dashboard/pre-devis?error=document_missing", status_code=303)

    return RedirectResponse("/dashboard/pre-devis?success=deleted", status_code=303)


@app.post("/api/project-document/delete-selected")
def delete_selected_project_documents(request: Request, doc_ids: list[int] | None = Form(None)):
    user = _require_user(request)
    if not user:
        return RedirectResponse("/login?next=/dashboard/documents", status_code=303)

    selected_ids = sorted({doc_id for doc_id in (doc_ids or []) if isinstance(doc_id, int)})
    if not selected_ids:
        return RedirectResponse("/dashboard/documents?error=missing_selection", status_code=303)

    db = SessionLocal()
    try:
        docs = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.client_id == user["id"], ProjectDocument.id.in_(selected_ids))
            .all()
        )
        if not docs:
            return RedirectResponse("/dashboard/documents?error=document_missing", status_code=303)

        file_paths: list[Path] = []
        for doc in docs:
            path = _resolve_document_path(doc.stored_name)
            if path:
                file_paths.append(path)
            db.delete(doc)
        db.commit()
    finally:
        db.close()

    for path in file_paths:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    return RedirectResponse("/dashboard/documents?success=deleted_selected", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin_redirect(request: Request):
    return RedirectResponse("/admin/dashboard", status_code=303)


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
    scope_key = _normalize_scope_key(scope, default_if_empty="")
    if not scope_key:
        return JSONResponse(
            {
                "ok": False,
                "error": "Type de travaux invalide. Selectionnez une option proposee.",
            },
            status_code=400,
        )
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
        require_work_item=True,
    )
    if quote.get("error"):
        return JSONResponse({"ok": False, "error": str(quote["error"])}, status_code=400)

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

    prequote_document_ref: dict[str, str] | None = None
    try:
        prequote_document_ref = _create_prequote_document_ref(
            quote=quote,
            project_type=project_key,
            scope=scope_key,
            style=style_key,
            timeline=timeline_key,
            city=city,
            surface=surface,
            rooms=rooms,
            budget=budget,
            work_item_key=work_item_key,
            work_quantity=work_quantity,
            work_unit=work_unit,
            notes=notes,
        )
    except OSError:
        prequote_document_ref = None
    prequote_public_url = (prequote_document_ref or {}).get("public_url", "")
    prequote_attachment: dict[str, object] | None = None
    prequote_stored_name = (prequote_document_ref or {}).get("stored_name", "")
    prequote_original_name = (prequote_document_ref or {}).get("original_name", "")
    if prequote_stored_name:
        prequote_path = _resolve_document_path(prequote_stored_name)
        if prequote_path and prequote_path.exists():
            try:
                prequote_attachment = {
                    "filename": prequote_original_name or prequote_path.name,
                    "content": prequote_path.read_bytes(),
                    "mime_type": "application/pdf",
                }
            except OSError:
                prequote_attachment = None

    document_refs: list[dict] = []
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
                    "public_url": _public_static_url(dst),
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
                    "public_url": _public_static_url(dst),
                }
            )

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

    project_document_refs = [dict(doc) for doc in document_refs]
    if prequote_document_ref:
        project_document_refs.insert(0, dict(prequote_document_ref))

    if project_saved and saved_project_id and current_user and project_document_refs:
        db = SessionLocal()
        try:
            for doc in project_document_refs:
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
                        "documents": project_document_refs,
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
            "prequote_url": prequote_public_url,
            "account_required_for_final": False,
            "account_optional": current_user is None,
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
        prequote_url=prequote_public_url,
        has_pdf_attachment=bool(prequote_attachment),
    )
    client_email_sent, client_error = _send_email_message(
        to_email=client_email,
        subject=client_subject,
        text_body=client_body,
        attachments=([prequote_attachment] if prequote_attachment else None),
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

    _record_quote_email_sent(
        handoff_id=handoff_id,
        prequote_url=prequote_public_url,
        project_type=project_key,
        quote=quote,
    )

    return {
        "ok": True,
        "message": "Pre-devis PDF envoye au client. Le rendu 3D est disponible sur demande apres devis.",
        "quote": quote,
        "handoff_id": handoff_id,
        "render_request_enabled": True,
        "project_saved": project_saved,
        "project_id": saved_project_id,
        "account_required_for_final": False,
        "account_optional": current_user is None,
        "prequote_url": prequote_public_url,
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
        raw_scope = scope if scope is not None else ""
        if not str(raw_scope or "").strip():
            raw_scope = handoff_payload.get("scope") or "renovation_complete"
        scope_key = _normalize_scope_key(str(raw_scope), default_if_empty="renovation_complete")
        if not scope_key:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Type de travaux invalide. Selectionnez une option proposee.",
                },
                status_code=400,
            )
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
        if (
            not isinstance(stored_quote, dict)
            or stored_quote.get("pricing_basis") != "catalog"
            or not stored_quote.get("low_label")
            or not stored_quote.get("high_label")
        ):
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
        if quote.get("error"):
            return JSONResponse({"ok": False, "error": str(quote["error"])}, status_code=400)

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
    scope_key = _normalize_scope_key(scope, default_if_empty="")
    if not scope_key:
        return JSONResponse(
            {
                "ok": False,
                "error": "Type de travaux invalide. Selectionnez une option proposee.",
            },
            status_code=400,
        )
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
        scope=scope_key,
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
    if quote.get("error"):
        return JSONResponse({"ok": False, "error": str(quote["error"])}, status_code=400)
    interior_offer = _build_interior_offer(
        project_type=project_type,
        style=style,
        scope=scope_key,
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
        scope=scope_key,
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
        scope=scope_key,
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
                        "scope": scope_key,
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
        scope=scope_key,
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
        scope=scope_key,
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


# ============================================================
# ADMIN DASHBOARD ROUTES (separate from /admin management)
# ============================================================

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request):
    user = _require_admin(request)
    if user:
        return RedirectResponse("/admin/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": request.query_params.get("error")},
    )


@app.post("/admin/login")
def admin_login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(""),
):
    normalized_email = email.strip().lower()
    db = SessionLocal()
    try:
        user_account = (
            db.query(UserAccount)
            .filter(UserAccount.email == normalized_email)
            .first()
        )
        if not user_account or not _verify_password(password, user_account.password_hash):
            return RedirectResponse("/admin/login?error=invalid", status_code=303)
        if user_account.role != "admin":
            return RedirectResponse("/admin/login?error=forbidden", status_code=303)

        token = _create_session(db, user_account.id)
    finally:
        db.close()

    redirect_to = next_url.strip() or "/admin/dashboard"
    response = RedirectResponse(redirect_to, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 86400,
        path="/",
    )
    return response


@app.get("/admin/logout")
@app.post("/admin/logout")
def admin_logout(request: Request):
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
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard_view(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/dashboard", status_code=303)

    db = SessionLocal()
    try:
        handoffs = (
            db.query(HandoffRequest)
            .order_by(HandoffRequest.created_at.desc())
            .all()
        )
        users = (
            db.query(UserAccount)
            .filter(UserAccount.role == "client")
            .order_by(UserAccount.created_at.desc())
            .all()
        )

        now = _utc_now()
        week_ago = now - timedelta(days=7)
        recent_count = (
            db.query(HandoffRequest)
            .filter(HandoffRequest.created_at >= week_ago)
            .count()
        )
        
        pending_count = (
            db.query(HandoffRequest)
            .filter(HandoffRequest.status == "new")
            .count()
        )
        
        # Get contact form leads from legacy SQLite
        from db import DB_PATH
        import sqlite3
        contacts = []
        try:
            with sqlite3.connect(DB_PATH) as con:
                con.row_factory = sqlite3.Row
                contacts = con.execute(
                    "SELECT * FROM leads ORDER BY created_at DESC"
                ).fetchall()
        except Exception:
            contacts = []
        
        # Build estimate list with parsed recaps
        estimates = []
        for h in handoffs:
            recap = _parse_handoff_conversation(h)
            estimates.append({
                "id": h.id,
                "created_at": h.created_at,
                "status": h.status,
                "priority": h.priority,
                "name": h.name,
                "email": h.email,
                "phone": h.phone,
                "city": h.city,
                "surface": h.surface,
                "recap": recap,
            })
        
        # Build chart data - estimations per day for last 30 days
        chart_labels = []
        chart_values = []
        for i in range(29, -1, -1):
            day = now - timedelta(days=i)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            count = (
                db.query(HandoffRequest)
                .filter(HandoffRequest.created_at >= day_start)
                .filter(HandoffRequest.created_at < day_end)
                .count()
            )
            chart_labels.append(day.strftime("%d/%m"))
            chart_values.append(count)
        
        # Build project types data
        project_type_counts = {}
        for h in handoffs:
            recap = _parse_handoff_conversation(h)
            ptype = recap.get("project_type", "Autre")
            project_type_counts[ptype] = project_type_counts.get(ptype, 0) + 1
        
        project_types_labels = list(project_type_counts.keys()) if project_type_counts else ["Aucun"]
        project_types_values = list(project_type_counts.values()) if project_type_counts else [0]
        
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "user": user,
            "active_page": "dashboard",
            "hide_public_header": True,
            "estimates": estimates,
            "users": users,
            "recent_estimates_count": recent_count,
            "pending_estimations": pending_count,
            "total_contacts": len(contacts),
            "chart_data": {
                "labels": chart_labels,
                "values": chart_values,
            },
            "project_types_data": {
                "labels": project_types_labels,
                "values": project_types_values,
            },
        },
    )


@app.post("/admin/dashboard/email-test")
def admin_dashboard_email_test(request: Request, to_email: str = Form(...)):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/dashboard", status_code=303)

    recipient = (to_email or "").strip().lower()
    if not _is_email_address(recipient):
        query = urlencode(
            {
                "email_test": "error",
                "email_test_to": recipient,
                "email_test_msg": "Adresse email invalide.",
            }
        )
        return RedirectResponse(f"/admin/dashboard?{query}", status_code=303)

    smtp_cfg = _smtp_settings()
    if not _smtp_ready(smtp_cfg):
        query = urlencode(
            {
                "email_test": "error",
                "email_test_to": recipient,
                "email_test_msg": "SMTP non configure (host/user/password/from).",
            }
        )
        return RedirectResponse(f"/admin/dashboard?{query}", status_code=303)

    subject, body = _compose_smtp_probe_email(initiated_by_email=str(user.get("email") or "admin"))
    sent, error = _send_email_message(to_email=recipient, subject=subject, text_body=body)
    if not sent:
        error_message = _clean_text(error or "", limit=200) or "Erreur SMTP inconnue."
        query = urlencode(
            {
                "email_test": "error",
                "email_test_to": recipient,
                "email_test_msg": error_message,
            }
        )
        return RedirectResponse(f"/admin/dashboard?{query}", status_code=303)

    query = urlencode(
        {
            "email_test": "ok",
            "email_test_to": recipient,
        }
    )
    return RedirectResponse(f"/admin/dashboard?{query}", status_code=303)


@app.get("/admin/chantiers", response_class=HTMLResponse)
def admin_chantiers_view(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/chantiers", status_code=303)

    state = request.query_params.get("state", "").strip().lower()
    state_messages = {
        "step_updated": ("success", "Etape chantier mise a jour."),
        "step_missing": ("error", "Etape chantier introuvable."),
        "invalid_progress": ("error", "Progression invalide."),
        "invalid_date": ("error", "Format de date invalide."),
    }
    flash = state_messages.get(state)

    db = SessionLocal()
    try:
        contracts = (
            db.query(ChantierContract)
            .order_by(ChantierContract.signed_at.desc(), ChantierContract.id.desc())
            .all()
        )
        contract_ids = [contract.id for contract in contracts]
        client_ids = sorted({int(contract.client_id) for contract in contracts if contract.client_id})

        users_by_id: dict[int, UserAccount] = {}
        if client_ids:
            users = db.query(UserAccount).filter(UserAccount.id.in_(client_ids)).all()
            users_by_id = {int(item.id): item for item in users}
        project_ids = sorted({int(contract.project_id) for contract in contracts if contract.project_id})
        projects_by_id: dict[int, ClientProject] = {}
        if project_ids:
            projects = db.query(ClientProject).filter(ClientProject.id.in_(project_ids)).all()
            projects_by_id = {int(item.id): item for item in projects}

        steps_by_contract: dict[int, list[ChantierLot]] = {}
        if contract_ids:
            steps = (
                db.query(ChantierLot)
                .filter(ChantierLot.contract_id.in_(contract_ids))
                .order_by(ChantierLot.contract_id.asc(), ChantierLot.sort_order.asc(), ChantierLot.id.asc())
                .all()
            )
            for step in steps:
                steps_by_contract.setdefault(int(step.contract_id), []).append(step)

        events_by_contract: dict[int, list[ChantierEvent]] = {}
        if contract_ids:
            events = (
                db.query(ChantierEvent)
                .filter(ChantierEvent.contract_id.in_(contract_ids))
                .order_by(ChantierEvent.created_at.desc(), ChantierEvent.id.desc())
                .all()
            )
            for event in events:
                bucket = events_by_contract.setdefault(int(event.contract_id), [])
                if len(bucket) < 6:
                    bucket.append(event)

        contracts_view: list[dict] = []
        status_label_map = {
            "active": "En cours",
            "completed": "Termine",
            "blocked": "Bloque",
        }
        for contract in contracts:
            client = users_by_id.get(int(contract.client_id))
            project = projects_by_id.get(int(contract.project_id)) if contract.project_id else None
            raw_steps = steps_by_contract.get(int(contract.id), [])
            step_rows: list[dict] = []
            step_comments: dict[str, str] = {}
            completed_steps: list[str] = []
            for step in raw_steps:
                status = _normalize_chantier_step_status(step.status or "")
                progress = max(0, min(100, int(step.progress_percent or 0)))
                if progress >= 100:
                    status = "validated"
                if status == "validated":
                    completed_steps.append(step.label)
                step_rows.append(
                    {
                        "id": step.id,
                        "label": step.label,
                        "status": status,
                        "status_label": _chantier_step_status_label(status),
                        "progress_percent": progress,
                        "next_step": step.next_step or "",
                        "planned_date_value": _format_chantier_input_date(step.planned_start),
                        "actual_date_value": _format_chantier_input_date(step.planned_end),
                        "planned_date_label": _format_chantier_date(step.planned_start),
                        "actual_date_label": _format_chantier_date(step.planned_end),
                        "client_comment": "",
                    }
                )

            event_rows: list[dict] = []
            for event in events_by_contract.get(int(contract.id), []):
                comment_text = _extract_client_comment(event.detail or "")
                step_label = _step_label_from_event_title(event.title or "")
                if comment_text and step_label:
                    step_comments.setdefault(_fold_lookup(step_label), comment_text)
                event_rows.append(
                    {
                        "title": event.title,
                        "detail": event.detail or "",
                        "created_label": event.created_at.strftime("%d/%m/%Y %H:%M") if event.created_at else "",
                    }
                )

            for step_row in step_rows:
                step_row["client_comment"] = step_comments.get(_fold_lookup(step_row.get("label")), "")

            global_progress, current_step_label, _, upcoming_steps = _compute_chantier_overview(step_rows)
            next_step_label = upcoming_steps[0] if upcoming_steps else ""
            if not next_step_label:
                next_step_label = _first_pending_or_blocked_step_label(step_rows)
            last_update_label = ""
            if event_rows:
                last_update_label = event_rows[0]["created_label"]
            elif contract.updated_at:
                last_update_label = contract.updated_at.strftime("%d/%m/%Y %H:%M")

            contracts_view.append(
                {
                    "id": contract.id,
                    "status": str(contract.status or "active"),
                    "status_label": status_label_map.get(str(contract.status or "").lower(), "En cours"),
                    "signed_label": contract.signed_at.strftime("%d/%m/%Y %H:%M") if contract.signed_at else "",
                    "quote_reference": contract.quote_reference or "A confirmer",
                    "client_name": (client.name if client and client.name else "Client"),
                    "client_email": (client.email if client and client.email else ""),
                    "project_name": (project.title if project else f"Chantier #{contract.id}"),
                    "global_progress": global_progress,
                    "current_step_label": current_step_label,
                    "next_step_label": next_step_label,
                    "last_update_label": last_update_label,
                    "completed_steps_count": len(completed_steps),
                    "total_steps": len(step_rows),
                    "steps": step_rows,
                    "events": event_rows,
                }
            )
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin_chantiers.html",
        {
            "user": user,
            "active_page": "chantiers",
            "hide_public_header": True,
            "flash": flash,
            "contracts": contracts_view,
        },
    )


@app.post("/admin/chantiers/step-update")
def admin_chantiers_step_update(
    request: Request,
    step_id: int = Form(...),
    status: str = Form("pending"),
    progress_percent: str = Form("0"),
    planned_date: str = Form(""),
    actual_date: str = Form(""),
    next_step: str = Form(""),
    internal_comment: str = Form(""),
    client_comment: str = Form(""),
    issue_flag: str = Form("none"),
    action: str = Form("save"),
):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/chantiers", status_code=303)

    parsed_progress = _parse_number(progress_percent)
    if parsed_progress is None:
        return RedirectResponse("/admin/chantiers?state=invalid_progress", status_code=303)

    planned_raw = str(planned_date or "").strip()
    actual_raw = str(actual_date or "").strip()
    planned_dt = _parse_chantier_form_date(planned_raw) if planned_raw else None
    actual_dt = _parse_chantier_form_date(actual_raw) if actual_raw else None
    if planned_raw and planned_dt is None:
        return RedirectResponse("/admin/chantiers?state=invalid_date", status_code=303)
    if actual_raw and actual_dt is None:
        return RedirectResponse("/admin/chantiers?state=invalid_date", status_code=303)

    db = SessionLocal()
    try:
        step = db.query(ChantierLot).filter(ChantierLot.id == step_id).first()
        if not step:
            return RedirectResponse("/admin/chantiers?state=step_missing", status_code=303)

        normalized_status = _normalize_chantier_step_status(status)
        action_key = str(action or "save").strip().lower()
        issue_key = str(issue_flag or "none").strip().lower()

        if action_key == "validate":
            normalized_status = "validated"
        elif issue_key == "blocked":
            normalized_status = "blocked"
        elif issue_key == "delayed":
            normalized_status = "delayed"

        progress = max(0, min(100, int(round(parsed_progress))))
        if normalized_status == "validated":
            progress = 100
        elif progress >= 100:
            normalized_status = "validated"
            progress = 100
        elif progress > 0 and normalized_status == "pending":
            normalized_status = "in_progress"
        elif progress == 0 and normalized_status in {"in_progress", "blocked", "delayed"}:
            progress = 10
        elif progress == 0 and normalized_status == "pending":
            normalized_status = "pending"

        step.status = normalized_status
        step.progress_percent = progress
        if planned_raw:
            step.planned_start = planned_dt
        if actual_raw:
            step.planned_end = actual_dt
        elif normalized_status == "validated":
            step.planned_end = step.planned_end or _utc_now()
        cleaned_next_step = _clean_text(next_step, limit=240)
        if cleaned_next_step:
            step.next_step = cleaned_next_step
        elif normalized_status == "validated":
            step.next_step = "Etape terminee."
        elif normalized_status == "blocked":
            step.next_step = "Blocage a lever avant reprise."
        elif normalized_status == "delayed":
            step.next_step = "Replanification en cours."
        elif normalized_status == "in_progress":
            step.next_step = "Execution en cours."
        else:
            step.next_step = "Etape planifiee."
        step.updated_at = _utc_now()

        contract = db.query(ChantierContract).filter(ChantierContract.id == step.contract_id).first()
        status_label = _chantier_step_status_label(normalized_status)
        detail_lines = [f"Statut: {status_label}. Avancement: {progress}%."]
        if step.planned_start:
            detail_lines.append(f"Date prevue: {_format_chantier_date(step.planned_start)}.")
        if step.planned_end and normalized_status == "validated":
            detail_lines.append(f"Date reelle: {_format_chantier_date(step.planned_end)}.")
        if step.next_step:
            detail_lines.append(f"Prochaine action: {step.next_step}")
        clean_internal_comment = _clean_text(internal_comment, limit=500)
        clean_client_comment = _clean_text(client_comment, limit=500)
        visible_to_client = action_key == "validate" or issue_key in {"blocked", "delayed"} or bool(clean_client_comment)
        if clean_internal_comment and not visible_to_client:
            detail_lines.append(f"Commentaire interne: {clean_internal_comment}")
        if clean_client_comment:
            detail_lines.append(f"Commentaire client: {clean_client_comment}")

        event_type = "progress"
        title = f"Mise a jour etape: {step.label}"
        if action_key == "validate":
            event_type = "validation"
            title = f"Etape validee: {step.label}"
        elif issue_key == "blocked":
            event_type = "incident"
            title = f"Blocage signale: {step.label}"
        elif issue_key == "delayed":
            event_type = "incident"
            title = f"Retard signale: {step.label}"

        db.add(
            ChantierEvent(
                contract_id=int(step.contract_id),
                event_type=event_type,
                title=title,
                detail=" ".join(detail_lines),
                impact_scope=("client" if visible_to_client else "internal"),
            )
        )

        if action_key == "validate" and contract:
            next_step = (
                db.query(ChantierLot)
                .filter(
                    ChantierLot.contract_id == contract.id,
                    ChantierLot.sort_order > step.sort_order,
                    ChantierLot.status == "pending",
                )
                .order_by(ChantierLot.sort_order.asc(), ChantierLot.id.asc())
                .first()
            )
            if next_step:
                next_step.status = "in_progress"
                next_step.progress_percent = max(10, int(next_step.progress_percent or 0))
                if not next_step.next_step:
                    next_step.next_step = "Execution en cours."
                next_step.updated_at = _utc_now()

        if contract:
            _sync_contract_status(db, contract)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/admin/chantiers?state=step_updated", status_code=303)


@app.get("/admin/estimations", response_class=HTMLResponse)
def admin_estimations_view(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/estimations", status_code=303)

    db = SessionLocal()
    try:
        handoffs = (
            db.query(HandoffRequest)
            .order_by(HandoffRequest.created_at.desc())
            .all()
        )
        
        now = _utc_now()
        week_ago = now - timedelta(days=7)
        this_week_count = (
            db.query(HandoffRequest)
            .filter(HandoffRequest.created_at >= week_ago)
            .count()
        )
        
        new_count = (
            db.query(HandoffRequest)
            .filter(HandoffRequest.status == "new")
            .count()
        )
        
        # Build estimate list with parsed recaps
        estimates = []
        for h in handoffs:
            recap = _parse_handoff_conversation(h)
            estimates.append({
                "id": h.id,
                "created_at": h.created_at,
                "status": h.status,
                "priority": h.priority,
                "name": h.name,
                "email": h.email,
                "phone": h.phone,
                "city": h.city,
                "surface": h.surface,
                "recap": recap,
            })
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin_estimations.html",
        {
            "user": user,
            "active_page": "estimations",
            "hide_public_header": True,
            "estimations": estimates,
            "total_estimations": len(estimates),
            "new_count": new_count,
            "this_week_count": this_week_count,
        },
    )


@app.get("/admin/contacts", response_class=HTMLResponse)
def admin_contacts_view(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login?next=/admin/contacts", status_code=303)

    # Get contact form leads from legacy SQLite
    from db import DB_PATH
    import sqlite3
    contacts = []
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            contacts = con.execute(
                "SELECT * FROM leads ORDER BY created_at DESC"
            ).fetchall()
    except Exception:
        contacts = []

    # Convert to dict for template
    contacts_list = []
    for contact in contacts:
        try:
            created_at = datetime.fromisoformat(contact["created_at"].replace("Z", "+00:00")) if contact["created_at"] else None
        except Exception:
            created_at = None
        
        contacts_list.append({
            "id": contact["id"],
            "name": contact["name"],
            "email": contact["email"],
            "phone": contact["phone"],
            "message": contact["message"],
            "created_at": created_at,
        })

    return templates.TemplateResponse(
        request,
        "admin_contacts.html",
        {
            "user": user,
            "active_page": "contacts",
            "hide_public_header": True,
            "contacts": contacts_list,
        },
    )


@app.get("/admin/dashboard/estimate/{estimate_id}", response_class=HTMLResponse)
def admin_estimate_detail(estimate_id: int, request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)

    db = SessionLocal()
    try:
        handoff = (
            db.query(HandoffRequest)
            .filter(HandoffRequest.id == estimate_id)
            .first()
        )
    finally:
        db.close()

    if not handoff:
        return RedirectResponse("/admin/dashboard?error=not_found", status_code=303)

    recap = _parse_handoff_conversation(handoff)
    raw = _parse_full_conversation(handoff)

    return templates.TemplateResponse(
        request,
        "admin_estimate_detail.html",
        {
            "user": user,
            "hide_public_header": True,
            "estimate": handoff,
            "recap": recap,
            "raw": raw,
            "user_email": handoff.email,
        },
    )


@app.get("/admin/dashboard/users", response_class=HTMLResponse)
def admin_users_view(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)

    email_filter = request.query_params.get("email", "").strip().lower()

    db = SessionLocal()
    try:
        if email_filter:
            users = (
                db.query(UserAccount)
                .filter(UserAccount.email == email_filter)
                .all()
            )
            if not users:
                users = (
                    db.query(UserAccount)
                    .order_by(UserAccount.created_at.desc())
                    .all()
                )
        else:
            users = (
                db.query(UserAccount)
                .order_by(UserAccount.created_at.desc())
                .all()
            )

        # Count estimates per user
        estimate_counts = {}
        for u in users:
            count = (
                db.query(HandoffRequest)
                .filter(
                    (HandoffRequest.email == u.email)
                    | (HandoffRequest.phone == u.phone)
                )
                .count()
            )
            estimate_counts[u.id] = count
    finally:
        db.close()

    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "user": user,
            "hide_public_header": True,
            "users": users,
            "estimate_counts": estimate_counts,
        },
    )


@app.get("/admin/dashboard/users/detail", response_class=HTMLResponse)
def admin_user_detail(request: Request):
    user = _require_admin(request)
    if not user:
        return RedirectResponse("/admin/login", status_code=303)

    user_email = request.query_params.get("email", "").strip().lower()
    if not user_email:
        return RedirectResponse("/admin/dashboard/users", status_code=303)

    db = SessionLocal()
    try:
        user_account = (
            db.query(UserAccount)
            .filter(UserAccount.email == user_email)
            .first()
        )
        if not user_account:
            return RedirectResponse("/admin/dashboard/users?error=not_found", status_code=303)

        # Get all handoffs for this user
        handoffs = (
            db.query(HandoffRequest)
            .filter(
                (HandoffRequest.email == user_account.email)
                | (HandoffRequest.phone == user_account.phone)
            )
            .order_by(HandoffRequest.created_at.desc())
            .all()
        )

        # Get projects
        projects = (
            db.query(ClientProject)
            .filter(ClientProject.client_id == user_account.id)
            .order_by(ClientProject.created_at.desc())
            .all()
        )

        # Doc counts
        project_ids = [p.id for p in projects]
        doc_counts = {}
        if project_ids:
            docs = (
                db.query(ProjectDocument)
                .filter(ProjectDocument.project_id.in_(project_ids))
                .all()
            )
            for doc in docs:
                doc_counts[doc.project_id] = doc_counts.get(doc.project_id, 0) + 1
    finally:
        db.close()

    # Parse handoff recaps
    estimates = []
    for h in handoffs:
        recap = _parse_handoff_conversation(h)
        estimates.append({
            "id": h.id,
            "created_at": h.created_at,
            "status": h.status,
            "recap": recap,
        })

    return templates.TemplateResponse(
        request,
        "admin_user_detail.html",
        {
            "user": user,
            "hide_public_header": True,
            "user_account": user_account,
            "estimates": estimates,
            "projects": projects,
            "doc_counts": doc_counts,
        },
    )


def _parse_handoff_conversation(handoff) -> dict:
    """Parse a HandoffRequest conversation JSON into a recap dict."""
    if not handoff or not handoff.conversation:
        return {}
    try:
        payload = json.loads(handoff.conversation)
        if not isinstance(payload, dict):
            return {}
        return {
            "project_type": payload.get("project_type") or payload.get("work_type"),
            "scope": payload.get("scope"),
            "style": payload.get("style"),
            "surface": payload.get("surface"),
            "rooms": payload.get("rooms"),
            "budget": payload.get("budget"),
            "city": payload.get("city"),
            "finishing_level": payload.get("finishing_level"),
            "timeline": payload.get("timeline"),
            "estimate_range": payload.get("quote", {}).get("estimate_range") if isinstance(payload.get("quote"), dict) else None,
            "appointment_status": payload.get("appointment_status"),
            "work_item_key": payload.get("work_item_key"),
            "work_quantity": payload.get("work_quantity"),
            "work_unit": payload.get("work_unit"),
            "stage": payload.get("stage"),
            "title": payload.get("quote", {}).get("title") if isinstance(payload.get("quote"), dict) else None,
        }
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_full_conversation(handoff) -> dict:
    """Parse full conversation JSON for detailed admin view."""
    if not handoff or not handoff.conversation:
        return {}
    try:
        payload = json.loads(handoff.conversation)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _format_conversation_json(conversation_str: str | None) -> str:
    """Format conversation JSON for readable display."""
    if not conversation_str:
        return "{}"
    try:
        payload = json.loads(conversation_str)
        return json.dumps(payload, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return conversation_str or "{}"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=10000, reload=True)
