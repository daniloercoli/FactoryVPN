"""Credenziali iniziali separate da utenti, worker e gateway già operativi."""

import json
from secrets import compare_digest
from uuid import UUID
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from api.config import settings
from api.db.session import get_db
from api.models.config import Config
from api.models.enrollment import GatewayEnrollment
from api.services.credentials import digest
from api.services.lifecycle import allocation_lock
from api.time_utils import utc_now

factory_header = APIKeyHeader(name="X-Factory-Key", scheme_name="FactoryKey", auto_error=False)
enrollment_header = APIKeyHeader(
    name="X-Enrollment-Key", scheme_name="EnrollmentKey", auto_error=False
)


async def rate_limit(db, identity, limit):
    # Il conteggio è persistente anche per una richiesta poi rifiutata. Dopo il
    # commit il chiamante riacquisisce il lock e ricontrolla la credenziale.
    key = "enrollment-rate:" + digest(identity)
    row = await db.get(Config, key)
    now = int(utc_now().timestamp())
    value = json.loads(row.value) if row else {"window": now, "count": 0}
    if now - value["window"] >= 60:
        value = {"window": now, "count": 0}
    value["count"] += 1
    if row:
        row.value = json.dumps(value)
    else:
        db.add(Config(key=key, value=json.dumps(value), updated_at=utc_now()))
    await db.commit()
    if value["count"] > limit:
        raise HTTPException(429, "Enrollment request limit exceeded", headers={"Retry-After": "60"})
    await allocation_lock(db)


async def factory_station(key: str | None = Depends(factory_header), db=Depends(get_db)):
    if not key or not 32 <= len(key) <= 256:
        raise HTTPException(401, "Invalid factory station credentials")
    hashed = digest(key)
    station = next(
        (
            name
            for name, expected in settings.FACTORY_STATIONS.items()
            if compare_digest(hashed, expected)
        ),
        None,
    )
    if station is None:
        raise HTTPException(401, "Invalid factory station credentials")
    await allocation_lock(db)
    await rate_limit(db, "station:" + station, settings.FACTORY_REQUESTS_PER_MINUTE)
    return station


async def gateway_enrollment(
    identifier: UUID, key: str | None = Depends(enrollment_header), db=Depends(get_db)
):
    if not key or not 32 <= len(key) <= 256:
        raise HTTPException(401, "Invalid enrollment credentials")
    await allocation_lock(db)
    row = await db.get(GatewayEnrollment, identifier, populate_existing=True)
    if (
        not row
        or not row.secret_hash
        or not compare_digest(digest(key), row.secret_hash)
        or row.blocked_at
        or row.completed_at
    ):
        raise HTTPException(401, "Invalid enrollment credentials")
    await rate_limit(db, "gateway:" + str(identifier), settings.ENROLLMENT_REQUESTS_PER_MINUTE)
    await db.refresh(row)
    if (
        not row.secret_hash
        or not compare_digest(digest(key), row.secret_hash)
        or row.blocked_at
        or row.completed_at
    ):
        raise HTTPException(401, "Invalid enrollment credentials")
    return row
