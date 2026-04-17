from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException, UploadFile

from database import SessionLocal
from db import DB_PATH
from models import ClientProject, HandoffRequest, ProjectDocument, UserAccount, UserSession


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt_value: datetime | None) -> datetime | None:
    if dt_value is None:
        return None
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=UTC)
    return dt_value


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


def _hash_password(raw_password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", raw_password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${derived.hex()}"


def _parse_handoff_conversation(handoff: HandoffRequest) -> dict:
    if not handoff or not handoff.conversation:
        return {}
    try:
        payload = json.loads(handoff.conversation)
        if not isinstance(payload, dict):
            return {}
        quote = payload.get("quote") if isinstance(payload.get("quote"), dict) else {}
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
            "estimate_range": quote.get("estimate_range"),
            "appointment_status": payload.get("appointment_status"),
            "work_item_key": payload.get("work_item_key"),
            "work_quantity": payload.get("work_quantity"),
            "work_unit": payload.get("work_unit"),
            "stage": payload.get("stage"),
            "title": quote.get("title"),
        }
    except (json.JSONDecodeError, TypeError):
        return {}


def _parse_full_conversation(handoff: HandoffRequest) -> dict:
    if not handoff or not handoff.conversation:
        return {}
    try:
        payload = json.loads(handoff.conversation)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


class CRMService:
    def __init__(
        self,
        session_factory=SessionLocal,
        session_ttl_days: int = 30,
        docs_dir: Path | None = None,
    ):
        self._session_factory = session_factory
        self._session_ttl_days = session_ttl_days
        base_dir = Path(__file__).resolve().parent.parent
        self._docs_dir = docs_dir or (base_dir / "static" / "client-docs")
        self._docs_dir.mkdir(parents=True, exist_ok=True)

    def _create_session(self, db, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = _utc_now() + timedelta(days=self._session_ttl_days)
        db.add(UserSession(user_id=user_id, token=token, expires_at=expires_at))
        db.commit()
        return token

    def resolve_admin_by_token(self, token: str) -> dict:
        db = self._session_factory()
        try:
            session = (
                db.query(UserSession)
                .filter(UserSession.token == token)
                .order_by(UserSession.id.desc())
                .first()
            )
            if not session:
                raise PermissionError("Session invalide")

            expires_at = _as_utc(session.expires_at)
            if expires_at and expires_at <= _utc_now():
                db.delete(session)
                db.commit()
                raise PermissionError("Session expiree")

            user = db.query(UserAccount).filter(UserAccount.id == session.user_id).first()
            if not user or user.role != "admin":
                raise PermissionError("Acces admin requis")

            return {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "session_token": token,
            }
        finally:
            db.close()

    def login_admin(self, email: str, password: str) -> dict:
        normalized = email.strip().lower()
        if not normalized or not password:
            raise ValueError("Email et mot de passe requis")

        db = self._session_factory()
        try:
            user = db.query(UserAccount).filter(UserAccount.email == normalized).first()
            if not user or not _verify_password(password, user.password_hash):
                raise PermissionError("Identifiants invalides")
            if user.role != "admin":
                raise PermissionError("Acces admin requis")

            token = self._create_session(db, user.id)
            return {
                "token": token,
                "token_type": "bearer",
                "expires_in_seconds": self._session_ttl_days * 86400,
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": user.name,
                    "role": user.role,
                },
            }
        finally:
            db.close()

    def logout(self, token: str) -> dict:
        db = self._session_factory()
        try:
            session = db.query(UserSession).filter(UserSession.token == token).first()
            if session:
                db.delete(session)
                db.commit()
            return {"ok": True}
        finally:
            db.close()

    def get_admin_me(self, token: str) -> dict:
        user = self.resolve_admin_by_token(token)
        return {
            "user": {
                "id": user["id"],
                "email": user["email"],
                "name": user["name"],
                "role": user["role"],
            }
        }

    def get_dashboard(self) -> dict:
        db = self._session_factory()
        try:
            handoffs = db.query(HandoffRequest).order_by(HandoffRequest.created_at.desc()).all()
            users = (
                db.query(UserAccount)
                .filter(UserAccount.role == "client")
                .order_by(UserAccount.created_at.desc())
                .all()
            )

            now = _utc_now()
            week_ago = now - timedelta(days=7)
            recent_count = db.query(HandoffRequest).filter(HandoffRequest.created_at >= week_ago).count()
            pending_count = db.query(HandoffRequest).filter(HandoffRequest.status == "new").count()

            estimates = []
            project_type_counts = {}
            for handoff in handoffs:
                recap = _parse_handoff_conversation(handoff)
                estimates.append(
                    {
                        "id": handoff.id,
                        "created_at": handoff.created_at,
                        "status": handoff.status,
                        "priority": handoff.priority,
                        "name": handoff.name,
                        "email": handoff.email,
                        "phone": handoff.phone,
                        "city": handoff.city,
                        "surface": handoff.surface,
                        "recap": recap,
                    }
                )
                ptype = recap.get("project_type") or "Autre"
                project_type_counts[ptype] = project_type_counts.get(ptype, 0) + 1

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

            total_contacts = self._count_contacts()

            return {
                "stats": {
                    "recent_estimates_count": recent_count,
                    "pending_estimations": pending_count,
                    "total_contacts": total_contacts,
                    "total_users": len(users),
                },
                "chart_data": {"labels": chart_labels, "values": chart_values},
                "project_types_data": {
                    "labels": list(project_type_counts.keys()) if project_type_counts else ["Aucun"],
                    "values": list(project_type_counts.values()) if project_type_counts else [0],
                },
                "recent_estimates": estimates[:25],
            }
        finally:
            db.close()

    def list_estimations(self, limit: int = 100, offset: int = 0) -> dict:
        db = self._session_factory()
        try:
            query = db.query(HandoffRequest)
            total = query.count()
            handoffs = (
                query.order_by(HandoffRequest.created_at.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )
            items = []
            for handoff in handoffs:
                items.append(
                    {
                        "id": handoff.id,
                        "created_at": handoff.created_at,
                        "status": handoff.status,
                        "priority": handoff.priority,
                        "name": handoff.name,
                        "email": handoff.email,
                        "phone": handoff.phone,
                        "city": handoff.city,
                        "surface": handoff.surface,
                        "recap": _parse_handoff_conversation(handoff),
                    }
                )

            now = _utc_now()
            week_ago = now - timedelta(days=7)
            this_week_count = db.query(HandoffRequest).filter(HandoffRequest.created_at >= week_ago).count()
            new_count = db.query(HandoffRequest).filter(HandoffRequest.status == "new").count()

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "stats": {
                    "new_count": new_count,
                    "this_week_count": this_week_count,
                },
                "items": items,
            }
        finally:
            db.close()

    def get_estimation_detail(self, estimate_id: int) -> dict:
        db = self._session_factory()
        try:
            handoff = db.query(HandoffRequest).filter(HandoffRequest.id == estimate_id).first()
            if not handoff:
                raise ValueError("Estimation introuvable")
            return {
                "estimate": {
                    "id": handoff.id,
                    "created_at": handoff.created_at,
                    "status": handoff.status,
                    "priority": handoff.priority,
                    "name": handoff.name,
                    "email": handoff.email,
                    "phone": handoff.phone,
                    "city": handoff.city,
                    "postal_code": handoff.postal_code,
                    "work_type": handoff.work_type,
                    "surface": handoff.surface,
                    "estimate_min": handoff.estimate_min,
                    "estimate_max": handoff.estimate_max,
                    "reason": handoff.reason,
                    "source": handoff.source,
                },
                "recap": _parse_handoff_conversation(handoff),
                "raw": _parse_full_conversation(handoff),
            }
        finally:
            db.close()

    def list_contacts(self, limit: int = 100, offset: int = 0) -> dict:
        query = "SELECT id, name, email, phone, message, created_at FROM leads ORDER BY created_at DESC LIMIT ? OFFSET ?"
        contacts = []
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(query, (limit, offset)).fetchall()
            for row in rows:
                contacts.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "email": row["email"],
                        "phone": row["phone"],
                        "message": row["message"],
                        "created_at": row["created_at"],
                    }
                )
            total_row = con.execute("SELECT COUNT(*) as c FROM leads").fetchone()

        return {
            "total": int(total_row["c"]) if total_row else 0,
            "limit": limit,
            "offset": offset,
            "items": contacts,
        }

    def _count_contacts(self) -> int:
        try:
            with sqlite3.connect(DB_PATH) as con:
                row = con.execute("SELECT COUNT(*) FROM leads").fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def list_users(self, email: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        db = self._session_factory()
        try:
            query = db.query(UserAccount)
            if email:
                query = query.filter(UserAccount.email == email.strip().lower())
            total = query.count()

            users = (
                query.order_by(UserAccount.created_at.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )

            items = []
            for user in users:
                estimate_count = (
                    db.query(HandoffRequest)
                    .filter((HandoffRequest.email == user.email) | (HandoffRequest.phone == user.phone))
                    .count()
                )
                items.append(
                    {
                        "id": user.id,
                        "email": user.email,
                        "name": user.name,
                        "phone": user.phone,
                        "role": user.role,
                        "status": user.status,
                        "created_at": user.created_at,
                        "estimate_count": estimate_count,
                    }
                )

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": items,
            }
        finally:
            db.close()

    def get_user_detail(self, email: str) -> dict:
        normalized = (email or "").strip().lower()
        if not normalized:
            raise ValueError("Email requis")

        db = self._session_factory()
        try:
            user = db.query(UserAccount).filter(UserAccount.email == normalized).first()
            if not user:
                raise ValueError("Utilisateur introuvable")

            handoffs = (
                db.query(HandoffRequest)
                .filter((HandoffRequest.email == user.email) | (HandoffRequest.phone == user.phone))
                .order_by(HandoffRequest.created_at.desc())
                .all()
            )
            projects = (
                db.query(ClientProject)
                .filter(ClientProject.client_id == user.id)
                .order_by(ClientProject.created_at.desc())
                .all()
            )

            project_ids = [project.id for project in projects]
            doc_counts: dict[int, int] = {}
            if project_ids:
                docs = db.query(ProjectDocument).filter(ProjectDocument.project_id.in_(project_ids)).all()
                for doc in docs:
                    doc_counts[doc.project_id] = doc_counts.get(doc.project_id, 0) + 1

            return {
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "name": user.name,
                    "phone": user.phone,
                    "role": user.role,
                    "status": user.status,
                    "created_at": user.created_at,
                },
                "estimates": [
                    {
                        "id": handoff.id,
                        "created_at": handoff.created_at,
                        "status": handoff.status,
                        "priority": handoff.priority,
                        "recap": _parse_handoff_conversation(handoff),
                    }
                    for handoff in handoffs
                ],
                "projects": [
                    {
                        "id": project.id,
                        "created_at": project.created_at,
                        "title": project.title,
                        "summary": project.summary,
                        "status": project.status,
                        "document_count": doc_counts.get(project.id, 0),
                    }
                    for project in projects
                ],
            }
        finally:
            db.close()

    def create_client(
        self,
        *,
        name: str,
        email: str,
        phone: str | None,
        status: str | None,
        password: str,
        project_title: str | None,
        project_summary: str | None,
        project_status: str | None,
    ) -> dict:
        normalized_email = email.strip().lower()
        if not normalized_email or not password:
            raise ValueError("Email et mot de passe requis")

        db = self._session_factory()
        try:
            existing = db.query(UserAccount).filter(UserAccount.email == normalized_email).first()
            if existing:
                raise ValueError("Client existe deja")

            client = UserAccount(
                email=normalized_email,
                password_hash=_hash_password(password),
                role="client",
                name=(name or "").strip() or None,
                phone=(phone or "").strip() or None,
                status=(status or "").strip() or None,
            )
            db.add(client)
            db.commit()
            db.refresh(client)

            project_payload = None
            if (project_title or "").strip():
                project = ClientProject(
                    client_id=client.id,
                    title=project_title.strip(),
                    summary=(project_summary or "").strip() or None,
                    status=(project_status or "").strip() or None,
                )
                db.add(project)
                db.commit()
                db.refresh(project)
                project_payload = {
                    "id": project.id,
                    "title": project.title,
                    "summary": project.summary,
                    "status": project.status,
                }

            return {
                "client": {
                    "id": client.id,
                    "email": client.email,
                    "name": client.name,
                    "phone": client.phone,
                    "role": client.role,
                    "status": client.status,
                },
                "project": project_payload,
            }
        finally:
            db.close()

    def upload_project_document(self, *, project_id: int, label: str | None, document: UploadFile) -> dict:
        db = self._session_factory()
        try:
            project = db.query(ClientProject).filter(ClientProject.id == project_id).first()
            if not project:
                raise ValueError("Projet introuvable")

            suffix = Path(document.filename or "").suffix.lower()
            if len(suffix) > 10:
                suffix = ""
            stored_name = f"{project_id}-{_utc_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex}{suffix}"
            file_path = self._docs_dir / stored_name
            with file_path.open("wb") as buffer:
                buffer.write(document.file.read())
            document.file.close()

            doc = ProjectDocument(
                client_id=project.client_id,
                project_id=project.id,
                label=(label or "").strip() or None,
                original_name=document.filename or stored_name,
                stored_name=stored_name,
                mime_type=document.content_type,
            )
            db.add(doc)
            db.commit()
            db.refresh(doc)

            return {
                "document": {
                    "id": doc.id,
                    "project_id": doc.project_id,
                    "client_id": doc.client_id,
                    "label": doc.label,
                    "original_name": doc.original_name,
                    "stored_name": doc.stored_name,
                    "mime_type": doc.mime_type,
                    "created_at": doc.created_at,
                }
            }
        finally:
            db.close()


def extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header manquant")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Authorization bearer invalide")
    return parts[1].strip()
