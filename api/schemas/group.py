"""Group management contracts. Machine registration never accepts these fields."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GroupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class GroupUpdateRequest(GroupCreateRequest):
    pass


class GroupResponse(GroupCreateRequest):
    revision: str = ""
    id: UUID
    created_at: datetime


class GatewayGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: UUID | None
