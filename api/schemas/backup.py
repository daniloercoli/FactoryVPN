"""Only public recovery metadata crosses the HTTP boundary."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class BackupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["verify", "restore", "delete"]
    confirmation: str = Field(default="", max_length=100)


class BackupReauthentication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class BackupRetention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1, le=100)


class BackupJobResponse(BaseModel):
    id: str
    kind: str
    backup_id: str | None
    state: str
    phase: str
    created_at: str
    finished_at: str | None
    error: str | None


class BackupEntry(BaseModel):
    id: str
    created_at: str
    size: int
    state: str
    verified_at: str | None
    tested_at: str | None
    protected: bool


class BackupStatus(BaseModel):
    enabled: bool
    coordinator_online: bool
    needs_reauthentication: bool
    sso_provider_id: str | None
    maximum_bytes: int
    retention: int
    backups: list[BackupEntry]
    jobs: list[BackupJobResponse]
