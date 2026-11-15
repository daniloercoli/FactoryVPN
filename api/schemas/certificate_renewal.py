from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, ConfigDict


class RenewalChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    previous_certificate_id: UUID
    csr_pem: str = Field(min_length=100, max_length=16384)


class RenewalProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signature: str = Field(min_length=8, max_length=512)


class RenewalActivation(RenewalProof):
    certificate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RenewalResponse(BaseModel):
    id: UUID
    gateway_id: UUID
    previous_certificate_id: UUID
    certificate_id: UUID | None
    state: str
    challenge: str
    signature_algorithm: str = "ecdsa-sha256-der"
    challenge_expires_at: datetime
    expires_at: datetime
    maintenance_until: datetime | None
    bundle: dict | None
    error_code: str | None


class RenewalStatus(BaseModel):
    gateway_id: UUID
    certificate_id: UUID
    serial: str
    expires_at: datetime
    renewal_due: bool
    renewal: RenewalResponse | None
