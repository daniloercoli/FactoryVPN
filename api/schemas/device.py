"""Consistent IPv4 device inputs for technician and admin routes."""

from uuid import UUID
from datetime import datetime
from typing import Literal
from ipaddress import IPv4Address
from pydantic import BaseModel, ConfigDict, Field, field_validator

DeviceType = Literal["PLC", "HMI", "SERVER", "OTHER"]


class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    ip_address: str
    mac_address: str = Field(pattern=r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$")
    device_type: DeviceType = "OTHER"

    @field_validator("ip_address")
    @classmethod
    def ipv4(cls, value):
        return str(IPv4Address(value))

    @field_validator("mac_address")
    @classmethod
    def mac(cls, value):
        return value.replace("-", ":").upper()


class DeviceCreateRequest(DeviceInput):
    gateway_id: UUID


class DeviceResponse(BaseModel):
    revision: str
    id: UUID
    gateway_id: UUID
    name: str | None
    ip_address: str | None
    mac_address: str | None
    device_type: str
    status: str
    last_seen: datetime | None
