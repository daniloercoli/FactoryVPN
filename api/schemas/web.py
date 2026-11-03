from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from api.schemas.client import CurrentUserResponse


class WebSessionResponse(BaseModel):
    user: CurrentUserResponse
    csrf_token: str
    expires_at: datetime


class WebSSORequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    reauthenticate: bool = False
