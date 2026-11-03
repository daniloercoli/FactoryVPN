from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SSOStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    redirect_uri: str = Field(max_length=1024)
    code_challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    state: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class SSOStartResponse(BaseModel):
    authorization_url: str
    expires_in: int


class SSOProviderResponse(BaseModel):
    id: str
    label: str
    online_revocation: bool
    status: Literal["wip"] = "wip"


class SSOExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=32, max_length=128)
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")


class IdentityLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    subject: str = Field(min_length=1, max_length=255)


class IdentityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: UUID
    provider_id: str
    issuer: str
    subject: str
    created_at: datetime


class AuthenticationModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth_mode: Literal["local", "sso"]
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def credential(self):
        if (self.auth_mode == "local") != (self.password is not None):
            raise ValueError("A fresh password is required only for local authentication")
        return self


class LogoutResponse(BaseModel):
    message: str
    provider_logout_urls: list[str] = Field(default_factory=list)
