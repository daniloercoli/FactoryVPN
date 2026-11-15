"""Explicit, bounded provisioning contracts. No secrets in JSON responses."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from api.schemas.gateway import GatewayRegisterRequest, GatewayInventoryFields

PeerKind = Literal["gateway", "technician"]


class GatewayProvisionRequest(GatewayRegisterRequest, GatewayInventoryFields):
    group_id: UUID
    display_name: str = Field(min_length=1, max_length=100, pattern=r".*\S.*")


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_certificate_id: UUID | None


class CertificateResponse(BaseModel):
    id: UUID
    serial: str
    not_before: datetime
    not_after: datetime
    state: Literal["candidate", "active", "revoked"]
    revoked_at: datetime | None
    crl_applied_at: datetime | None


class JobResponse(BaseModel):
    id: UUID
    kind: PeerKind
    peer_id: UUID
    state: Literal["queued", "ready", "active", "cancelled", "failed", "expired"]
    created_at: datetime
    expires_at: datetime
    downloaded_at: datetime | None
    error_code: str | None
    can_download: bool
    can_activate: bool


class ProvisioningState(BaseModel):
    enrollment_pending: bool = False
    remote_renewal_state: str | None = None
    remote_renewal_requested: bool = False
    certificate: CertificateResponse | None
    pending_job: JobResponse | None
    recent_job: JobResponse | None
    registered_at: datetime | None
    cleanup_pending: bool
    revocation_pending: bool
    enabled: bool
    issuer_status: Literal["ok", "error", "unknown", "stale"]
    issuer_updated_at: datetime | None
