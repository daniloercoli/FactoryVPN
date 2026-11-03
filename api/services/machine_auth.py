"""Credentials for machine endpoints, separate from technician JWTs."""

from secrets import compare_digest

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from api.config import settings
from api.db.session import get_db
from sqlalchemy import select
from api.models.provisioning import GatewayCredential
from api.services.credentials import digest
from api.services.lifecycle import allocation_lock

worker_key_header = APIKeyHeader(name="X-Worker-Key", scheme_name="WorkerKey", auto_error=False)
gateway_key_header = APIKeyHeader(name="X-Gateway-Key", scheme_name="GatewayKey", auto_error=False)


def require_worker(key: str | None = Depends(worker_key_header)) -> None:
    if not settings.WORKER_API_KEY:
        raise HTTPException(status_code=503, detail="Worker authentication is not configured")
    if not key or not compare_digest(key.encode(), settings.WORKER_API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid worker credentials")


async def get_gateway_identity(
    key: str | None = Depends(gateway_key_header), db=Depends(get_db)
) -> str:
    """Authenticate and retain the revocation lock until the endpoint commits."""
    if not key or len(key) > 256:
        raise HTTPException(status_code=401, detail="Invalid gateway credentials")
    await allocation_lock(db)
    credential = await db.scalar(
        select(GatewayCredential).where(
            GatewayCredential.secret_hash == digest(key),
            GatewayCredential.active.is_(True),
            GatewayCredential.revoked_at.is_(None),
        )
    )
    if credential is None:
        raise HTTPException(status_code=401, detail="Invalid gateway credentials")
    return str(credential.gateway_id)
