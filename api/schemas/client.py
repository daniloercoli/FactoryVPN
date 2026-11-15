"""Client contracts used by response models and additional OpenAPI metadata."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from api.schemas.gateway import GatewayAvailabilityResponse, GatewayInventoryFields
from api.schemas.diagnostics import DiagnosticsResponse

SessionStatus = Literal["REQUESTED", "ACTIVE", "CLOSED", "EXPIRED", "FAILED"]
PAGINATION_RESPONSES = {
    200: {
        "headers": {
            "X-Total-Count": {
                "description": "Total rows after authorization and filters, before pagination.",
                "schema": {"type": "integer", "minimum": 0},
            }
        }
    }
}


class CurrentUserResponse(BaseModel):
    revision: str = ""
    auth_mode: Literal["local", "sso"] = "local"
    id: UUID
    username: str
    email: str | None
    email_verified: bool = False
    email_two_step: bool = False
    email_two_step_required: bool = False
    full_name: str | None
    is_admin: bool
    last_login: datetime | None
    permissions: list[str]
    group_ids: list[UUID]


class MessageResponse(BaseModel):
    message: str


class ExpirationResponse(BaseModel):
    expired: int


class SessionSummaryResponse(BaseModel):
    id: UUID
    group_id: UUID | None = None
    technician_id: UUID | None
    gateway_id: UUID | None
    status: SessionStatus
    activation_mode: Literal["sms", "wifi"]
    activation_received_at: datetime | None
    gateway_ready_at: datetime | None
    requested_at: datetime | None
    support_started_at: datetime | None
    ended_at: datetime | None
    expires_at: datetime | None
    cleaned_at: datetime | None


class SessionListResponse(SessionSummaryResponse):
    cleanup_pending: bool
    # Current labels are shown only when the gateway is still visible to the caller.
    gateway_display_name: str | None
    gateway_mac_address: str | None


class AdminSessionListResponse(SessionListResponse):
    technician_username: str | None
    technician_full_name: str | None
    technician_manageable: bool
    technician_vpn_ip: str | None
    gateway_vpn_ip: str | None
    # Expiry of the worker evidence, not the session or the portal login.
    observation_valid_until: datetime | None
    diagnostics: DiagnosticsResponse


class AdminSessionResponse(SessionSummaryResponse):
    result: str | None
    error_reason: str | None


class SessionDetailResponse(AdminSessionResponse):
    sso_session_id: UUID | None = None
    web_session_id: UUID | None = None
    technician_vpn_ip: str | None
    gateway_vpn_ip: str | None
    sms_sent_at: datetime | None
    sms_last_attempt_at: datetime | None
    gateway_connected_at: datetime | None
    network_ready_at: datetime | None
    worker_seen_at: datetime | None


class AdminSessionCloseResponse(BaseModel):
    id: UUID
    status: SessionStatus
    ended_at: datetime | None


class AdminGatewaySummaryResponse(GatewayInventoryFields):
    revision: str = ""
    id: UUID
    display_name: str | None = None
    group_id: UUID | None = None
    mac_address: str | None
    phone_number: str | None
    activation_mode: Literal["sms", "wifi"]
    status: str
    vpn_ip: str | None
    last_seen: datetime | None
    created_at: datetime | None


class AdminGatewayCatalogResponse(AdminGatewaySummaryResponse, GatewayAvailabilityResponse):
    pass


class AdminGatewayDetailResponse(AdminGatewayCatalogResponse):
    metadata: dict[str, Any] | None


class AdminDeviceResponse(BaseModel):
    revision: str
    id: UUID
    name: str | None
    ip_address: str | None
    mac_address: str | None
    device_type: str
    status: str
    last_seen: datetime | None


class AdminDeviceCreatedResponse(BaseModel):
    id: UUID
    status: str
