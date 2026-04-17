from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field

from crm_api.service import CRMService, extract_bearer_token


router = APIRouter(prefix="/api/admin/v1", tags=["crm-admin"])
_service: CRMService | None = None


def _get_service() -> CRMService:
    global _service
    if _service is None:
        _service = CRMService()
    return _service


def _require_admin(authorization: str | None = Header(default=None)) -> dict:
    service = _get_service()
    token = extract_bearer_token(authorization)
    try:
        return service.resolve_admin_by_token(token)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


class AdminLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=180)
    password: str = Field(min_length=3, max_length=200)


class CreateClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=180)
    phone: str | None = Field(default=None, max_length=50)
    status: str | None = Field(default=None, max_length=40)
    password: str = Field(min_length=6, max_length=200)
    project_title: str | None = Field(default=None, max_length=180)
    project_summary: str | None = None
    project_status: str | None = Field(default=None, max_length=40)


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.post("/auth/login")
def admin_login(payload: AdminLoginRequest) -> dict:
    service = _get_service()
    try:
        return service.login_admin(payload.email, payload.password)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/logout")
def admin_logout(authorization: str | None = Header(default=None), _: dict = Depends(_require_admin)) -> dict:
    service = _get_service()
    token = extract_bearer_token(authorization)
    return service.logout(token)


@router.get("/auth/me")
def admin_me(authorization: str | None = Header(default=None), _: dict = Depends(_require_admin)) -> dict:
    service = _get_service()
    token = extract_bearer_token(authorization)
    return service.get_admin_me(token)


@router.get("/dashboard")
def dashboard(_: dict = Depends(_require_admin)) -> dict:
    return _get_service().get_dashboard()


@router.get("/estimations")
def estimations(limit: int = 100, offset: int = 0, _: dict = Depends(_require_admin)) -> dict:
    return _get_service().list_estimations(limit=limit, offset=offset)


@router.get("/estimations/{estimate_id}")
def estimation_detail(estimate_id: int, _: dict = Depends(_require_admin)) -> dict:
    try:
        return _get_service().get_estimation_detail(estimate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/contacts")
def contacts(limit: int = 100, offset: int = 0, _: dict = Depends(_require_admin)) -> dict:
    try:
        return _get_service().list_contacts(limit=limit, offset=offset)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Impossible de lire les contacts") from exc


@router.get("/users")
def users(email: str | None = None, limit: int = 100, offset: int = 0, _: dict = Depends(_require_admin)) -> dict:
    return _get_service().list_users(email=email, limit=limit, offset=offset)


@router.get("/users/detail")
def user_detail(email: str, _: dict = Depends(_require_admin)) -> dict:
    try:
        return _get_service().get_user_detail(email)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/clients")
def create_client(payload: CreateClientRequest, _: dict = Depends(_require_admin)) -> dict:
    try:
        return _get_service().create_client(
            name=payload.name,
            email=payload.email,
            phone=payload.phone,
            status=payload.status,
            password=payload.password,
            project_title=payload.project_title,
            project_summary=payload.project_summary,
            project_status=payload.project_status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/documents")
def upload_document(
    project_id: int = Form(...),
    label: str = Form(default=""),
    document: UploadFile = File(...),
    _: dict = Depends(_require_admin),
) -> dict:
    try:
        return _get_service().upload_project_document(
            project_id=project_id,
            label=label,
            document=document,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
