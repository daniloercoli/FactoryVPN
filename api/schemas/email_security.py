from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from api.services.mail_service import email_address


class EmailChallengeResponse(BaseModel):
    challenge_required: Literal[True] = True
    challenge_token: str
    destination: str
    expires_at: datetime
    resend_after: int = 60


class EmailProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_token: str = Field(min_length=32, max_length=128)
    code: str = Field(pattern=r"^[0-9]{6}$")


class EmailResend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_token: str = Field(min_length=32, max_length=128)


class EmailStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["enable", "disable", "change"]
    password: str = Field(min_length=1, max_length=128)
    email: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def address(cls, value):
        return email_address(value) if value is not None else None


class EmailRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=255)

    @field_validator("email")
    @classmethod
    def address(cls, value):
        return email_address(value)


class EmailStatus(BaseModel):
    enabled: bool
    required: bool
    verified: bool
    email: str | None
    smtp_configured: bool


class EmailComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: Literal[True]
