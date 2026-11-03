"""Select a gateway by stable identity, or by its optional SMS phone number."""

from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["gateway_id"],
                    "properties": {
                        "gateway_id": {"type": "string"},
                        "phone_number": {"type": "null"},
                    },
                },
                {
                    "required": ["phone_number"],
                    "properties": {
                        "phone_number": {"type": "string"},
                        "gateway_id": {"type": "null"},
                    },
                },
            ],
            "examples": [{"gateway_id": "11111111-1111-4111-8111-111111111111"}],
        }
    )
    gateway_id: UUID | None = None
    phone_number: str | None = Field(default=None, pattern=r"^\+[1-9]\d{9,14}$")

    @model_validator(mode="after")
    def validate_selector(self):
        if (self.gateway_id is None) == (self.phone_number is None):
            raise ValueError("Provide exactly one of gateway_id or phone_number")
        return self


class SessionCreateResponse(BaseModel):
    session_id: str
    status: str
    message: str
    replayed: bool = False
