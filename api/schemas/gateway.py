"""
Gateway schemas with input validation
"""

import ipaddress
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from uuid import UUID
from factoryvpn_common.network import VPN_NETWORK, TRANSIT_NETWORK

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
    mac_address: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
    lan_interface: str = Field(..., pattern=r"^[a-zA-Z0-9_.-]{1,15}$")
    lan_address: str
    lan_cidr: str

    @field_validator("mac_address")
    @classmethod
    def normalize_mac(cls, value):
        return value.upper().replace("-", ":") if value else None

    @field_validator("lan_address")
    @classmethod
    def validate_lan_address(cls, v: str) -> str:
        try:
            return str(ipaddress.IPv4Address(v))
        except ValueError:
            raise ValueError("Invalid IPv4 address")

    @field_validator("lan_cidr")
    @classmethod
    def validate_lan_cidr(cls, v: str) -> str:
        try:
            network = ipaddress.IPv4Network(v, strict=False)
            if network.overlaps(VPN_NETWORK) or network.overlaps(TRANSIT_NETWORK):
                raise ValueError("LAN overlaps reserved VPN networks")
            return str(network)
        except ValueError:
            raise ValueError("Invalid CIDR notation")

    @model_validator(mode="after")
    def validate_lan(self):
        if self.activation_mode == "wifi" and not self.mac_address:
            raise ValueError("Wi-Fi activation requires a MAC address")
        if self.activation_mode == "sms" and not self.phone_number:
            raise ValueError("SMS activation requires a phone number")
        if ipaddress.IPv4Address(self.lan_address) not in ipaddress.IPv4Network(self.lan_cidr):
            raise ValueError("LAN address is outside the LAN network")
        return self


class HeartbeatRequest(BaseModel):
    vpn_ip: str | None = None
    ready_session_id: UUID | None = None
    activation_token: str | None = Field(default=None, max_length=160)


class GatewayUpdateRequest(BaseModel):
    """Human-managed label, separate from machine registration and network settings."""

    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(max_length=100)

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value):
        return value.strip() or None if value is not None else None


class GatewayResponse(BaseModel):
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
    lan_cidr: str | None
    lan_interface: str | None
    lan_address: str | None
    created_at: datetime | None
    current_session: GatewayCurrentSession | None
