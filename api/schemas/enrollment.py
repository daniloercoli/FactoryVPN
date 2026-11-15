"""Contratti per postazioni, primo avvio e amministrazione dell'arruolamento."""

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from api.schemas.gateway import GatewayInventoryFields


class EnrollmentData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(None, max_length=100)
    activation_mode: Literal["sms", "wifi"] | None = None
    phone_number: str | None = Field(None, pattern=r"^\+[1-9]\d{9,14}$")
    mac_address: str | None = Field(None, pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
    imei: str | None = Field(None, pattern=r"^[0-9]{15}$")
    iccid: str | None = Field(None, pattern=r"^[0-9]{18,22}$")

    @field_validator("display_name", "phone_number", "mac_address", "imei", "iccid", mode="before")
    @classmethod
    def normalize(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("mac_address")
    @classmethod
    def normalize_mac(cls, value):
        return value.upper().replace("-", ":") if value else None


class FactoryCreateRequest(EnrollmentData):
    gateway_id: UUID
    serial_number: str = Field(min_length=1, max_length=100)
    secret_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_reference: str | None = Field(None, max_length=200)

    @field_validator("serial_number")
    @classmethod
    def serial(cls, value):
        if not value.strip():
            raise ValueError("Serial number is required")
        return value.strip()


class EnrollmentUpdateRequest(EnrollmentData, GatewayInventoryFields):
    serial_number: str | None = Field(None, min_length=1, max_length=100)
    external_reference: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def serial_required(self):
        if "serial_number" in self.model_fields_set and not self.serial_number:
            raise ValueError("Serial number is required for an enrolled gateway")
        return self


class EnrollmentApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: UUID


class EnrollmentManualRequest(EnrollmentApproval):
    expected_certificate_id: UUID | None = None


class EnrollmentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if not value.strip():
            raise ValueError("Provide a reason")
        return value.strip()


class EnrollmentVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    configuration_version: int = Field(ge=1)


class EnrollmentJob(BaseModel):
    id: UUID
    state: str
    expires_at: datetime
    configuration_version: int | None
    can_download: bool
    error_code: str | None


EnrollmentState = Literal[
    "prepared",
    "awaiting_approval",
    "awaiting_data",
    "approved",
    "preparing",
    "ready",
    "completed",
    "blocked",
    "manual",
]


class EnrollmentResponse(EnrollmentData):
    id: UUID
    gateway_id: UUID | None
    vpn_ip: str | None
    serial_number: str
    external_reference: str | None
    group_id: UUID | None
    state: EnrollmentState
    delivery_mode: Literal["automatic", "manual"]
    configuration_version: int
    revision: str
    created_at: datetime
    approved_at: datetime | None
    completed_at: datetime | None
    blocked_reason: str | None
    can_resume: bool
    missing_fields: list[str]
    job: EnrollmentJob | None


class EnrollmentConfirmation(BaseModel):
    activated: Literal[True] = True
    gateway_id: UUID
    configuration_version: int
