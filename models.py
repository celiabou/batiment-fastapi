# noinspection SpellCheckingInspection
from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Lead(Base):
    # Keep a separate table to avoid conflicts with legacy sqlite schema in db.py.
    __tablename__ = "chat_leads"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    name = Column(String(120), nullable=True)
    phone = Column(String(50), nullable=True)
    email = Column(String(120), nullable=True)

    city = Column(String(120), nullable=True)
    postal_code = Column(String(20), nullable=True)
    surface = Column(String(50), nullable=True)
    work_type = Column(String(120), nullable=True)

    estimate_min = Column(String(50), nullable=True)
    estimate_max = Column(String(50), nullable=True)
    ip_address = Column(String(64), nullable=True)

    raw_message = Column(Text, nullable=True)


class HandoffRequest(Base):
    __tablename__ = "handoff_requests"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    status = Column(String(30), default="new", nullable=False, index=True)
    priority = Column(String(30), default="normal", nullable=False, index=True)
    source = Column(String(30), default="chat_widget", nullable=False)

    name = Column(String(120), nullable=True)
    phone = Column(String(50), nullable=True)
    email = Column(String(120), nullable=True)
    city = Column(String(120), nullable=True)
    postal_code = Column(String(20), nullable=True)

    work_type = Column(String(120), nullable=True)
    surface = Column(String(50), nullable=True)
    estimate_min = Column(String(50), nullable=True)
    estimate_max = Column(String(50), nullable=True)

    reason = Column(String(255), nullable=True)
    ip_address = Column(String(64), nullable=True)
    conversation = Column(Text, nullable=True)


class OpportunitySignal(Base):
    __tablename__ = "opportunity_signals"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    source_name = Column(String(80), nullable=False, index=True)
    source_channel = Column(String(40), nullable=False, index=True)
    source_query = Column(Text, nullable=True)

    title = Column(String(500), nullable=False)
    summary = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    canonical_url = Column(Text, nullable=True)
    signature_hash = Column(String(64), nullable=False, unique=True, index=True)

    published_at = Column(DateTime(timezone=True), nullable=True, index=True)

    location_city = Column(String(120), nullable=True, index=True)
    location_department_code = Column(String(4), nullable=True, index=True)
    location_department_name = Column(String(120), nullable=True)
    postal_code = Column(String(10), nullable=True, index=True)

    work_types = Column(Text, nullable=True)
    announcement_type = Column(String(50), nullable=True, index=True)

    budget_min = Column(Integer, nullable=True)
    budget_max = Column(Integer, nullable=True)
    deadline_text = Column(String(120), nullable=True)
    contact_email = Column(String(180), nullable=True)
    contact_phone = Column(String(50), nullable=True)

    score = Column(Integer, nullable=False, default=0, index=True)
    raw_payload = Column(Text, nullable=True)


class TenantAccount(Base):
    __tablename__ = "tenant_accounts"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    tenant_key = Column(String(64), nullable=False, unique=True, index=True)
    company_name = Column(String(180), nullable=False, index=True)
    contact_name = Column(String(120), nullable=True)
    contact_email = Column(String(180), nullable=True, index=True)
    status = Column(String(30), nullable=False, default="active", index=True)
    notes = Column(Text, nullable=True)


class ProductSubscription(Base):
    __tablename__ = "product_subscriptions"
    __table_args__ = (UniqueConstraint("tenant_id", "product_code", name="uq_tenant_product"),)

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    tenant_id = Column(Integer, ForeignKey("tenant_accounts.id"), nullable=False, index=True)
    product_code = Column(String(80), nullable=False, index=True)
    plan_code = Column(String(80), nullable=False, default="trial", index=True)
    status = Column(String(30), nullable=False, default="active", index=True)

    trial_started_at = Column(DateTime(timezone=True), nullable=True)
    trial_ends_at = Column(DateTime(timezone=True), nullable=True, index=True)
    current_period_started_at = Column(DateTime(timezone=True), nullable=True)
    current_period_ends_at = Column(DateTime(timezone=True), nullable=True, index=True)

    monthly_price_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(8), nullable=False, default="EUR")
    auto_renew = Column(Boolean, nullable=False, default=True)
    external_subscription_id = Column(String(120), nullable=True)


class AIModelProfile(Base):
    __tablename__ = "ai_model_profiles"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    tenant_id = Column(Integer, ForeignKey("tenant_accounts.id"), nullable=False, index=True)
    product_code = Column(String(80), nullable=False, index=True)
    model_name = Column(String(180), nullable=False)
    model_version = Column(String(80), nullable=False, default="v1")
    training_mode = Column(String(40), nullable=False, default="on_demand")
    status = Column(String(30), nullable=False, default="ready", index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    last_trained_at = Column(DateTime(timezone=True), nullable=True)
    metadata_json = Column(Text, nullable=True)


class AITrainingJob(Base):
    __tablename__ = "ai_training_jobs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    tenant_id = Column(Integer, ForeignKey("tenant_accounts.id"), nullable=False, index=True)
    product_code = Column(String(80), nullable=False, index=True)
    model_profile_id = Column(Integer, ForeignKey("ai_model_profiles.id"), nullable=True, index=True)

    status = Column(String(30), nullable=False, default="queued", index=True)
    dataset_uri = Column(Text, nullable=True)
    objective = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    requested_by = Column(String(180), nullable=True)

    requested_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    metrics_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    email = Column(String(180), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(20), nullable=False, default="client", index=True)

    name = Column(String(120), nullable=True)
    phone = Column(String(50), nullable=True)
    status = Column(String(40), nullable=True)


class ClientProject(Base):
    __tablename__ = "client_projects"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    client_id = Column(Integer, ForeignKey("user_accounts.id"), nullable=False, index=True)
    title = Column(String(180), nullable=False)
    summary = Column(Text, nullable=True)
    status = Column(String(40), nullable=True)


class ChantierContract(Base):
    __tablename__ = "chantier_contracts"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    client_id = Column(Integer, ForeignKey("user_accounts.id"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("client_projects.id"), nullable=True, index=True)
    status = Column(String(30), nullable=False, default="active", index=True)

    signed_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    signer_name = Column(String(120), nullable=False)
    signer_email = Column(String(180), nullable=False, index=True)
    signer_ip = Column(String(64), nullable=True)
    quote_reference = Column(String(80), nullable=True)
    terms_version = Column(String(20), nullable=False, default="v1")


class ChantierLot(Base):
    __tablename__ = "chantier_lots"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    contract_id = Column(Integer, ForeignKey("chantier_contracts.id"), nullable=False, index=True)
    code = Column(String(64), nullable=True)
    label = Column(String(180), nullable=False)
    status = Column(String(30), nullable=False, default="pending", index=True)
    progress_percent = Column(Integer, nullable=False, default=0)
    next_step = Column(Text, nullable=True)
    planned_start = Column(DateTime(timezone=True), nullable=True, index=True)
    planned_end = Column(DateTime(timezone=True), nullable=True, index=True)
    sort_order = Column(Integer, nullable=False, default=0)


class ChantierMilestone(Base):
    __tablename__ = "chantier_milestones"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    lot_id = Column(Integer, ForeignKey("chantier_lots.id"), nullable=False, index=True)
    title = Column(String(180), nullable=False)
    status = Column(String(30), nullable=False, default="pending", index=True)
    notes = Column(Text, nullable=True)
    due_at = Column(DateTime(timezone=True), nullable=True, index=True)
    ready_at = Column(DateTime(timezone=True), nullable=True)
    validated_at = Column(DateTime(timezone=True), nullable=True, index=True)


class ChantierEvent(Base):
    __tablename__ = "chantier_events"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    contract_id = Column(Integer, ForeignKey("chantier_contracts.id"), nullable=False, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    title = Column(String(180), nullable=False)
    detail = Column(Text, nullable=True)
    impact_timeline = Column(String(120), nullable=True)
    impact_scope = Column(String(120), nullable=True)


class ProjectDocument(Base):
    __tablename__ = "project_documents"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    client_id = Column(Integer, ForeignKey("user_accounts.id"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("client_projects.id"), nullable=False, index=True)
    label = Column(String(180), nullable=True)
    original_name = Column(String(255), nullable=False)
    stored_name = Column(String(255), nullable=False)
    mime_type = Column(String(120), nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    user_id = Column(Integer, ForeignKey("user_accounts.id"), nullable=False, index=True)
    token = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)

    user_id = Column(Integer, ForeignKey("user_accounts.id"), nullable=False, index=True)
    token = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
