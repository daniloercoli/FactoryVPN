from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from factoryvpn_common.runtime_info import RuntimeInfo


SmtpMode = Literal["connection", "send"]
SmtpCode = Literal[
    "running",
    "connected",
    "accepted",
    "not_configured",
    "tls_failed",
    "authentication_failed",
    "recipient_rejected",
    "connection_failed",
    "timeout",
]


class SmtpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    mode: SmtpMode = "connection"


class SmtpResult(BaseModel):
    request_id: UUID
    mode: SmtpMode
    code: SmtpCode
    started_at: datetime
    finished_at: datetime | None


class SmtpStatus(BaseModel):
    configured: bool
    host: str | None
    port: int
    security: Literal["plain", "tls", "starttls"]
    sender: str | None
    authentication: bool
    custom_ca: bool
    test_recipient: str | None
    last_check: SmtpResult | None
    retry_after_seconds: int


class CentralComponent(BaseModel):
    name: str
    version: str | None
    build: str | None = None
    observed_at: datetime | None
    state: Literal["ok", "error", "idle", "unknown"]
    valid_for_seconds: float = Field(ge=0, le=90)


class CertificateStatus(BaseModel):
    name: Literal["https", "openvpn", "openvpn_ca"]
    state: Literal[
        "not_configured", "missing", "unreadable", "valid", "expiring", "expired", "not_yet_valid"
    ]
    observed_at: datetime
    valid_for_seconds: float = Field(ge=0, le=30)
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    days_remaining: int | None = None
    fingerprint_sha256: str | None = None


class BackupCopySummary(BaseModel):
    id: UUID
    created_at: datetime
    tested_at: datetime | None


class BackupTestSummary(BaseModel):
    backup_id: UUID | None
    finished_at: datetime
    state: Literal["succeeded", "failed"]


class BackupOperationSummary(BaseModel):
    kind: Literal["create", "import", "verify", "restore", "delete"]
    state: Literal["queued", "uploading", "running", "succeeded", "failed"]
    created_at: datetime
    finished_at: datetime | None


class BackupSummary(BaseModel):
    state: Literal["not_configured", "unavailable", "available"]
    observed_at: datetime
    valid_for_seconds: float = Field(ge=0, le=30)
    coordinator_state: Literal["online", "offline", "unknown"] = "unknown"
    coordinator_observed_at: datetime | None = None
    copies: int | None = Field(default=None, ge=0)
    latest_backup: BackupCopySummary | None = None
    last_test: BackupTestSummary | None = None
    last_operation: BackupOperationSummary | None = None
    active_operation: BackupOperationSummary | None = None


class AuthorityStatus(BaseModel):
    id: str
    role: Literal["root", "issuing", "retiring"]
    expires_at: datetime
    crl_next_update: datetime | None
    issuance_deadline: datetime | None = None
    can_issue_clients: bool = False
    renewal_due: bool = False


class PKIStatus(BaseModel):
    state: Literal["not_configured", "ok", "error"]
    observed_at: datetime
    valid_for_seconds: int = 30
    crl_valid: bool = False
    authorities: list[AuthorityStatus] = []


class ServerStatus(BaseModel):
    observed_at: datetime
    components: list[CentralComponent]
    api_runtime: RuntimeInfo
    smtp: SmtpStatus
    certificates: list[CertificateStatus]
    backup: BackupSummary
    pki: PKIStatus
