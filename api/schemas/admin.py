"""
Admin schemas - user management, audit log, statistics
"""

from datetime import datetime
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from api.permissions import Permission


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=3, max_length=50)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    auth_mode: Literal["local", "sso"] = "local"
    email: str | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=200)
    is_admin: bool = False
    permissions: list[Permission] = Field(default_factory=lambda: [Permission.CONNECT])
    group_ids: list[UUID] = Field(default_factory=list, max_length=200)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        from api.services.mail_service import email_address

        return email_address(value) if value is not None else None

    @model_validator(mode="after")
    def authentication(self):
        if (self.auth_mode == "local") != (self.password is not None):
            raise ValueError("A password is required only for local accounts")
        return self

    @field_validator("permissions", "group_ids")
    @classmethod
    def unique_values(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate values are not allowed")
        return value


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str | None = Field(default=None, max_length=255)
    full_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None
    is_admin: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    permissions: list[Permission] | None = None
    group_ids: list[UUID] | None = Field(default=None, max_length=200)

    @field_validator("permissions", "group_ids")
    @classmethod
    def unique_values(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Duplicate values are not allowed")
        return value

    @model_validator(mode="after")
    def non_nullable_security_fields(self):
        for field in ("is_active", "is_admin", "password", "permissions", "group_ids"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class UserResponse(BaseModel):
    revision: str = ""
    auth_mode: Literal["local", "sso"] = "local"
    id: str
    username: str
    email: str | None
    email_verified: bool = False
    email_two_step: bool = False
    email_two_step_required: bool = False
    full_name: str | None
    is_active: bool
    is_admin: bool
    permissions: list[str]
    group_ids: list[UUID]
    created_at: datetime | None
    last_login: datetime | None


class AuditLogResponse(BaseModel):
    id: int
    event_type: str
    group_id: UUID | None
    timestamp: datetime
    user_id: str | None
    session_id: str | None
    gateway_id: str | None
    ip_address: str | None
    details: dict | None


class StatsResponse(BaseModel):
    users_total: int
    users_active: int
    users_admin: int
    gateways_total: int
    gateways_active: int
    sessions_total: int
    sessions_active: int
    sessions_requested: int
    sessions_closed: int
    sessions_expired: int
    sessions_failed: int
