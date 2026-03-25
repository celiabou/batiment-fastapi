from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from saas_ai.service import SaaSAIService


router = APIRouter(prefix="/api/saas-ai", tags=["saas-ai"])
_service: SaaSAIService | None = None



def _get_service() -> SaaSAIService:
    global _service
    if _service is None:
        _service = SaaSAIService()
    return _service


class CreateTenantRequest(BaseModel):
    company_name: str = Field(min_length=2, max_length=180)
    contact_email: str | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class StartTrialRequest(BaseModel):
    product_codes: list[str] | None = None
    trial_days: int = Field(default=60, ge=1, le=365)


class UpgradeSubscriptionRequest(BaseModel):
    product_code: str
    monthly_price_cents: int = Field(ge=0)
    plan_code: str = Field(default="abonnement", max_length=80)
    billing_days: int = Field(default=30, ge=1, le=365)
    auto_renew: bool = True
    external_subscription_id: str | None = None


class TrainModelRequest(BaseModel):
    tenant_id: int = Field(ge=1)
    product_code: str
    objective: str = Field(min_length=2)
    dataset_uri: str | None = None
    requested_by: str | None = None
    notes: str | None = None


class CompleteTrainingRequest(BaseModel):
    metrics: dict | None = None


class FailTrainingRequest(BaseModel):
    error_message: str = Field(min_length=2)


@router.get("/health")
def saas_health() -> dict:
    return {"ok": True}


@router.post("/tenants")
def create_tenant(payload: CreateTenantRequest) -> dict:
    service = _get_service()
    try:
        tenant = service.create_tenant(
            company_name=payload.company_name,
            contact_email=payload.contact_email,
            contact_name=payload.contact_name,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"tenant": tenant}


@router.post("/tenants/{tenant_id}/trial")
def start_trial(tenant_id: int, payload: StartTrialRequest) -> dict:
    service = _get_service()
    try:
        return service.start_trial(
            tenant_id=tenant_id,
            product_codes=payload.product_codes,
            trial_days=payload.trial_days,
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.post("/tenants/{tenant_id}/subscriptions/upgrade")
def upgrade_subscription(tenant_id: int, payload: UpgradeSubscriptionRequest) -> dict:
    service = _get_service()
    try:
        return service.upgrade_subscription(
            tenant_id=tenant_id,
            product_code=payload.product_code,
            monthly_price_cents=payload.monthly_price_cents,
            plan_code=payload.plan_code,
            billing_days=payload.billing_days,
            auto_renew=payload.auto_renew,
            external_subscription_id=payload.external_subscription_id,
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("/tenants/{tenant_id}/subscriptions")
def subscription_status(tenant_id: int) -> dict:
    service = _get_service()
    try:
        return service.get_subscription_status(tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/tenants/{tenant_id}/entitlement")
def check_entitlement(tenant_id: int, product_code: str) -> dict:
    service = _get_service()
    try:
        entitled = service.has_active_entitlement(tenant_id=tenant_id, product_code=product_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "tenant_id": tenant_id,
        "product_code": product_code,
        "entitled": entitled,
    }


@router.post("/models/train")
def request_training(payload: TrainModelRequest) -> dict:
    service = _get_service()
    try:
        return service.request_training_job(
            tenant_id=payload.tenant_id,
            product_code=payload.product_code,
            objective=payload.objective,
            dataset_uri=payload.dataset_uri,
            requested_by=payload.requested_by,
            notes=payload.notes,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.post("/models/jobs/{job_id}/start")
def start_training(job_id: int) -> dict:
    service = _get_service()
    try:
        return service.start_training_job(job_id=job_id)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.post("/models/jobs/{job_id}/complete")
def complete_training(job_id: int, payload: CompleteTrainingRequest) -> dict:
    service = _get_service()
    try:
        return service.complete_training_job(job_id=job_id, metrics=payload.metrics)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.post("/models/jobs/{job_id}/fail")
def fail_training(job_id: int, payload: FailTrainingRequest) -> dict:
    service = _get_service()
    try:
        return service.fail_training_job(job_id=job_id, error_message=payload.error_message)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("/tenants/{tenant_id}/models")
def list_models(tenant_id: int, product_code: str | None = None) -> dict:
    service = _get_service()
    try:
        return service.list_model_profiles(tenant_id=tenant_id, product_code=product_code)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc


@router.get("/tenants/{tenant_id}/training-jobs")
def list_jobs(tenant_id: int, product_code: str | None = None, limit: int = 50) -> dict:
    service = _get_service()
    try:
        return service.list_training_jobs(
            tenant_id=tenant_id,
            product_code=product_code,
            limit=limit,
        )
    except ValueError as exc:
        message = str(exc)
        status = 404 if "introuvable" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc
