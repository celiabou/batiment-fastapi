from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from database import SessionLocal
from models import AIModelProfile, AITrainingJob, ProductSubscription, TenantAccount
from saas_ai.constants import (
    DEFAULT_BILLING_DAYS,
    DEFAULT_PLAN_CODE,
    DEFAULT_TRIAL_DAYS,
    PRODUCT_CODES,
    normalize_product_code,
)


class SaaSAIService:
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    def create_tenant(
        self,
        company_name: str,
        contact_email: str | None = None,
        contact_name: str | None = None,
        notes: str | None = None,
    ) -> dict:
        if not company_name.strip():
            raise ValueError("company_name est obligatoire")

        tenant = TenantAccount(
            tenant_key=uuid.uuid4().hex,
            company_name=company_name.strip(),
            contact_name=(contact_name or "").strip() or None,
            contact_email=(contact_email or "").strip().lower() or None,
            notes=(notes or "").strip() or None,
        )

        session = self.session_factory()
        try:
            session.add(tenant)
            session.commit()
            session.refresh(tenant)
            return self._serialize_tenant(tenant)
        finally:
            session.close()

    def start_trial(
        self,
        tenant_id: int,
        product_codes: list[str] | None = None,
        trial_days: int = DEFAULT_TRIAL_DAYS,
    ) -> dict:
        products = sorted({normalize_product_code(p) for p in (product_codes or list(PRODUCT_CODES))})
        if trial_days <= 0:
            raise ValueError("trial_days doit etre > 0")

        session = self.session_factory()
        try:
            tenant = self._require_tenant(session, tenant_id)
            now = _utc_now()
            trial_end = now + timedelta(days=trial_days)

            for product_code in products:
                row = self._get_subscription(session, tenant_id=tenant.id, product_code=product_code)
                if row is None:
                    row = ProductSubscription(
                        tenant_id=tenant.id,
                        product_code=product_code,
                    )
                    session.add(row)

                row.plan_code = "trial"
                row.status = "active"
                row.trial_started_at = now
                row.trial_ends_at = trial_end
                row.current_period_started_at = now
                row.current_period_ends_at = trial_end
                row.monthly_price_cents = 0
                row.currency = "EUR"
                row.auto_renew = False
                row.updated_at = now

            session.commit()

            subscriptions = self._list_subscriptions_for_tenant(session, tenant.id)
            return {
                "tenant": self._serialize_tenant(tenant),
                "subscriptions": [self._serialize_subscription(sub) for sub in subscriptions],
            }
        finally:
            session.close()

    def upgrade_subscription(
        self,
        tenant_id: int,
        product_code: str,
        monthly_price_cents: int,
        plan_code: str = DEFAULT_PLAN_CODE,
        billing_days: int = DEFAULT_BILLING_DAYS,
        auto_renew: bool = True,
        external_subscription_id: str | None = None,
    ) -> dict:
        normalized_product = normalize_product_code(product_code)
        if monthly_price_cents < 0:
            raise ValueError("monthly_price_cents doit etre >= 0")
        if billing_days <= 0:
            raise ValueError("billing_days doit etre > 0")

        session = self.session_factory()
        try:
            tenant = self._require_tenant(session, tenant_id)
            row = self._get_subscription(session, tenant_id=tenant.id, product_code=normalized_product)
            if row is None:
                row = ProductSubscription(
                    tenant_id=tenant.id,
                    product_code=normalized_product,
                )
                session.add(row)

            now = _utc_now()
            period_end = now + timedelta(days=billing_days)

            row.plan_code = (plan_code or DEFAULT_PLAN_CODE).strip().lower()
            row.status = "active"
            row.current_period_started_at = now
            row.current_period_ends_at = period_end
            row.monthly_price_cents = int(monthly_price_cents)
            row.currency = "EUR"
            row.auto_renew = bool(auto_renew)
            row.external_subscription_id = (external_subscription_id or "").strip() or None
            row.updated_at = now

            session.commit()
            session.refresh(row)

            return {
                "tenant": self._serialize_tenant(tenant),
                "subscription": self._serialize_subscription(row),
            }
        finally:
            session.close()

    def get_subscription_status(self, tenant_id: int) -> dict:
        session = self.session_factory()
        try:
            tenant = self._require_tenant(session, tenant_id)
            subscriptions = self._list_subscriptions_for_tenant(session, tenant_id=tenant.id)
            return {
                "tenant": self._serialize_tenant(tenant),
                "subscriptions": [self._serialize_subscription(row) for row in subscriptions],
            }
        finally:
            session.close()

    def has_active_entitlement(self, tenant_id: int, product_code: str) -> bool:
        normalized_product = normalize_product_code(product_code)
        session = self.session_factory()
        try:
            row = self._get_subscription(session, tenant_id=tenant_id, product_code=normalized_product)
            if row is None:
                return False
            return self._is_subscription_active(row)
        finally:
            session.close()

    def request_training_job(
        self,
        tenant_id: int,
        product_code: str,
        objective: str,
        dataset_uri: str | None = None,
        requested_by: str | None = None,
        notes: str | None = None,
    ) -> dict:
        normalized_product = normalize_product_code(product_code)

        session = self.session_factory()
        try:
            tenant = self._require_tenant(session, tenant_id)
            row = self._get_subscription(session, tenant_id=tenant.id, product_code=normalized_product)
            if row is None or not self._is_subscription_active(row):
                raise PermissionError(
                    "Abonnement inactif: activez d'abord l'essai ou un abonnement pour ce produit."
                )

            profile = self._get_or_create_model_profile(
                session=session,
                tenant_id=tenant.id,
                product_code=normalized_product,
            )

            now = _utc_now()
            profile.status = "queued"
            profile.updated_at = now

            job = AITrainingJob(
                tenant_id=tenant.id,
                product_code=normalized_product,
                model_profile_id=profile.id,
                status="queued",
                dataset_uri=(dataset_uri or "").strip() or None,
                objective=(objective or "").strip() or None,
                notes=(notes or "").strip() or None,
                requested_by=(requested_by or "").strip() or None,
                requested_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            session.refresh(job)

            return {
                "tenant": self._serialize_tenant(tenant),
                "subscription": self._serialize_subscription(row),
                "model_profile": self._serialize_model_profile(profile),
                "job": self._serialize_job(job),
            }
        finally:
            session.close()

    def start_training_job(self, job_id: int) -> dict:
        session = self.session_factory()
        try:
            job = self._require_job(session, job_id)
            if job.status not in {"queued", "retry"}:
                raise ValueError("Job non demarrable: statut actuel incompatible")

            now = _utc_now()
            job.status = "running"
            job.started_at = now
            job.updated_at = now

            profile = self._get_model_profile(session, job.model_profile_id)
            if profile is not None:
                profile.status = "training"
                profile.updated_at = now

            session.commit()
            return {"job": self._serialize_job(job)}
        finally:
            session.close()

    def complete_training_job(self, job_id: int, metrics: dict | None = None) -> dict:
        session = self.session_factory()
        try:
            job = self._require_job(session, job_id)
            if job.status not in {"queued", "running"}:
                raise ValueError("Job non finalisable: statut actuel incompatible")

            now = _utc_now()
            job.status = "completed"
            job.finished_at = now
            job.updated_at = now
            job.metrics_json = json.dumps(metrics or {}, ensure_ascii=False)
            job.error_message = None

            profile = self._get_model_profile(session, job.model_profile_id)
            if profile is not None:
                profile.model_version = _next_model_version(profile.model_version)
                profile.status = "ready"
                profile.last_trained_at = now
                profile.updated_at = now

            session.commit()

            payload = {"job": self._serialize_job(job)}
            if profile is not None:
                payload["model_profile"] = self._serialize_model_profile(profile)
            return payload
        finally:
            session.close()

    def fail_training_job(self, job_id: int, error_message: str) -> dict:
        session = self.session_factory()
        try:
            job = self._require_job(session, job_id)
            if job.status not in {"queued", "running", "retry"}:
                raise ValueError("Job non echecable: statut actuel incompatible")

            now = _utc_now()
            job.status = "failed"
            job.finished_at = now
            job.updated_at = now
            job.error_message = (error_message or "Erreur non specifiee").strip()

            profile = self._get_model_profile(session, job.model_profile_id)
            if profile is not None:
                profile.status = "error"
                profile.updated_at = now

            session.commit()
            return {"job": self._serialize_job(job)}
        finally:
            session.close()

    def list_model_profiles(self, tenant_id: int, product_code: str | None = None) -> dict:
        session = self.session_factory()
        try:
            tenant = self._require_tenant(session, tenant_id)

            stmt = select(AIModelProfile).where(AIModelProfile.tenant_id == tenant.id)
            if product_code:
                stmt = stmt.where(AIModelProfile.product_code == normalize_product_code(product_code))

            rows = list(session.execute(stmt.order_by(AIModelProfile.updated_at.desc())).scalars())
            return {
                "tenant": self._serialize_tenant(tenant),
                "models": [self._serialize_model_profile(row) for row in rows],
            }
        finally:
            session.close()

    def list_training_jobs(
        self,
        tenant_id: int,
        product_code: str | None = None,
        limit: int = 50,
    ) -> dict:
        session = self.session_factory()
        try:
            tenant = self._require_tenant(session, tenant_id)

            stmt = select(AITrainingJob).where(AITrainingJob.tenant_id == tenant.id)
            if product_code:
                stmt = stmt.where(AITrainingJob.product_code == normalize_product_code(product_code))

            safe_limit = max(1, min(limit, 500))
            rows = list(session.execute(stmt.order_by(AITrainingJob.requested_at.desc()).limit(safe_limit)).scalars())
            return {
                "tenant": self._serialize_tenant(tenant),
                "jobs": [self._serialize_job(row) for row in rows],
            }
        finally:
            session.close()

    def _require_tenant(self, session, tenant_id: int) -> TenantAccount:
        row = session.execute(select(TenantAccount).where(TenantAccount.id == tenant_id)).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Tenant introuvable: {tenant_id}")
        return row

    def _require_job(self, session, job_id: int) -> AITrainingJob:
        row = session.execute(select(AITrainingJob).where(AITrainingJob.id == job_id)).scalar_one_or_none()
        if row is None:
            raise ValueError(f"Job introuvable: {job_id}")
        return row

    def _get_subscription(self, session, tenant_id: int, product_code: str) -> ProductSubscription | None:
        return session.execute(
            select(ProductSubscription).where(
                ProductSubscription.tenant_id == tenant_id,
                ProductSubscription.product_code == product_code,
            )
        ).scalar_one_or_none()

    def _list_subscriptions_for_tenant(self, session, tenant_id: int) -> list[ProductSubscription]:
        stmt = select(ProductSubscription).where(ProductSubscription.tenant_id == tenant_id)
        return list(session.execute(stmt.order_by(ProductSubscription.product_code.asc())).scalars())

    def _get_or_create_model_profile(self, session, tenant_id: int, product_code: str) -> AIModelProfile:
        stmt = select(AIModelProfile).where(
            AIModelProfile.tenant_id == tenant_id,
            AIModelProfile.product_code == product_code,
            AIModelProfile.is_active.is_(True),
        )
        profile = session.execute(stmt.order_by(AIModelProfile.updated_at.desc())).scalars().first()
        if profile is not None:
            return profile

        now = _utc_now()
        profile = AIModelProfile(
            tenant_id=tenant_id,
            product_code=product_code,
            model_name=f"{product_code}_custom_model",
            model_version="v1",
            training_mode="on_demand",
            status="ready",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(profile)
        session.flush()
        return profile

    def _get_model_profile(self, session, model_profile_id: int | None) -> AIModelProfile | None:
        if model_profile_id is None:
            return None
        return session.execute(select(AIModelProfile).where(AIModelProfile.id == model_profile_id)).scalar_one_or_none()

    def _is_subscription_active(self, row: ProductSubscription) -> bool:
        if row.status != "active":
            return False
        reference_end = _as_utc(row.current_period_ends_at or row.trial_ends_at)
        if reference_end is None:
            return True
        now = _utc_now()
        return reference_end >= now

    def _serialize_tenant(self, row: TenantAccount) -> dict:
        return {
            "id": row.id,
            "tenant_key": row.tenant_key,
            "company_name": row.company_name,
            "contact_name": row.contact_name,
            "contact_email": row.contact_email,
            "status": row.status,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    def _serialize_subscription(self, row: ProductSubscription) -> dict:
        active = self._is_subscription_active(row)
        now = _utc_now()

        reference_end = _as_utc(row.current_period_ends_at or row.trial_ends_at)
        remaining_days = None
        if reference_end is not None:
            delta = reference_end - now
            remaining_days = max(0, int(delta.total_seconds() // 86400))

        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "product_code": row.product_code,
            "plan_code": row.plan_code,
            "status": row.status,
            "trial_started_at": _iso(row.trial_started_at),
            "trial_ends_at": _iso(row.trial_ends_at),
            "current_period_started_at": _iso(row.current_period_started_at),
            "current_period_ends_at": _iso(row.current_period_ends_at),
            "monthly_price_cents": row.monthly_price_cents,
            "currency": row.currency,
            "auto_renew": row.auto_renew,
            "external_subscription_id": row.external_subscription_id,
            "is_entitled": active,
            "remaining_days": remaining_days,
            "updated_at": _iso(row.updated_at),
        }

    def _serialize_model_profile(self, row: AIModelProfile) -> dict:
        metadata = _safe_json(row.metadata_json)
        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "product_code": row.product_code,
            "model_name": row.model_name,
            "model_version": row.model_version,
            "training_mode": row.training_mode,
            "status": row.status,
            "is_active": row.is_active,
            "last_trained_at": _iso(row.last_trained_at),
            "metadata": metadata,
            "updated_at": _iso(row.updated_at),
        }

    def _serialize_job(self, row: AITrainingJob) -> dict:
        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "product_code": row.product_code,
            "model_profile_id": row.model_profile_id,
            "status": row.status,
            "dataset_uri": row.dataset_uri,
            "objective": row.objective,
            "notes": row.notes,
            "requested_by": row.requested_by,
            "requested_at": _iso(row.requested_at),
            "started_at": _iso(row.started_at),
            "finished_at": _iso(row.finished_at),
            "metrics": _safe_json(row.metrics_json),
            "error_message": row.error_message,
            "updated_at": _iso(row.updated_at),
        }



def _utc_now() -> datetime:
    return datetime.now(UTC)



def _iso(value: datetime | None) -> str | None:
    normalized = _as_utc(value)
    return normalized.isoformat() if normalized else None



def _safe_json(raw: str | None):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}



def _next_model_version(current: str | None) -> str:
    raw = (current or "v0").strip().lower()
    match = re.fullmatch(r"v(\d+)", raw)
    if not match:
        return "v1"
    return f"v{int(match.group(1)) + 1}"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
