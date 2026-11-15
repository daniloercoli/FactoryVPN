"""
Gateway schemas with input validation
"""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from uuid import UUID

GatewayStatus = Literal["PENDING", "ACTIVE", "INACTIVE"]
ActivationMode = Literal["sms", "wifi"]
GatewayAvailability = Literal["available", "busy", "cleanup_pending", "unconfigured"]
StartBlockReason = Literal[
    "permission_required",
    "personal_profile_required",
    "gateway_unconfigured",
    "gateway_busy",
    "gateway_cleanup_pending",
    "technician_busy",
    "technician_cleanup_pending",
]


class GatewayRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gateway_id: UUID
    activation_mode: Literal["sms", "wifi"] = "sms"
    phone_number: str | None = Field(default=None, pattern=r"^\+[1-9]\d{9,14}$")
    mac_address: str | None = Field(
        default=None, pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$"
    )

    @field_validator("mac_address")
    @classmethod
    def normalize_mac(cls, value):
        return value.upper().replace("-", ":") if value else None

    @model_validator(mode="after")
    def validate_activation(self):
        if self.activation_mode == "wifi" and not self.mac_address:
            raise ValueError("Wi-Fi activation requires a MAC address")
        if self.activation_mode == "sms" and not self.phone_number:
            raise ValueError("SMS activation requires a phone number")
        return self


class HeartbeatRequest(BaseModel):
    vpn_ip: str | None = None
    ready_session_id: UUID | None = None
    activation_token: str | None = Field(default=None, max_length=160)


class GatewayInventoryFields(BaseModel):
    """Identificativi di inventario, separati dalle credenziali del gateway."""

    serial_number: str | None = Field(default=None, max_length=100)
    imei: str | None = Field(default=None, pattern=r"^[0-9]{15}$")
    iccid: str | None = Field(default=None, pattern=r"^[0-9]{18,22}$")

    @field_validator("serial_number", "imei", "iccid", mode="before")
    @classmethod
    def normalize_inventory(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class GatewayUpdateRequest(GatewayInventoryFields):
    """Inventario delegabile; configurazione operativa riservata a HQ."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})
    display_name: str | None = Field(default=None, max_length=100)
    activation_mode: ActivationMode | None = None
    phone_number: str | None = Field(None, pattern=r"^\+[1-9]\d{9,14}$")
    mac_address: str | None = Field(None, pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")

    @field_validator("mac_address")
    @classmethod
    def normalize_mac(cls, value):
        return value.upper().replace("-", ":") if value else None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one inventory field or display name")
        return self

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value):
        return value.strip() or None if value is not None else None


class GatewayResponse(GatewayInventoryFields):
    id: str
    display_name: str | None = None
    group_id: UUID | None = None
    phone_number: str | None
    activation_mode: Literal["sms", "wifi"]
    mac_address: str | None
    status: str
    vpn_ip: str | None
    last_seen: str | None


class GatewayAvailabilityResponse(BaseModel):
    """Advisory reservation state, never proof of connectivity or a reservation."""

    availability: GatewayAvailability
    can_start: bool
    start_block_reason: StartBlockReason | None


class GatewayCatalogResponse(GatewayResponse, GatewayAvailabilityResponse):
    pass


class GatewayCurrentSession(BaseModel):
    id: UUID
    status: Literal["REQUESTED", "ACTIVE", "CLOSED", "EXPIRED", "FAILED"]
    activation_mode: ActivationMode
    activation_received_at: datetime | None
    expires_at: datetime | None
    cleanup_pending: bool


class GatewayDetailResponse(GatewayCatalogResponse):
    revision: str
    serial_number: str | None
    created_at: datetime | None
    current_session: GatewayCurrentSession | None
