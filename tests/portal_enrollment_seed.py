# ruff: noqa: E402
"""Pratiche di fabbrica isolate per le prove browser, mai dati dell'installazione."""

import asyncio
from datetime import timedelta
import hashlib
import os
from uuid import UUID, uuid4

if (
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL")
    or os.environ.get("DATABASE_URL") != os.environ["FACTORYVPN_TEST_DATABASE_URL"]
):
    raise RuntimeError("A disposable test database is required")

from sqlalchemy import delete
from api.db.session import async_session_maker, engine
from api.models import Gateway
from api.models.enrollment import GatewayEnrollment
from api.models.provisioning import GatewayCredential, ProvisioningJob, VPNCertificate
from api.services.lifecycle import allocation_lock
from api.time_utils import utc_now


def identifier(number):
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


async def seed():
    ids = [identifier(number) for number in (801, 802, 803)]
    async with async_session_maker() as db:
        await allocation_lock(db)
        await db.execute(delete(ProvisioningJob).where(ProvisioningJob.peer_id.in_(ids)))
        await db.execute(delete(GatewayCredential).where(GatewayCredential.gateway_id.in_(ids)))
        await db.execute(delete(VPNCertificate).where(VPNCertificate.peer_id.in_(ids)))
        await db.execute(delete(Gateway).where(Gateway.id.in_(ids)))
        await db.execute(delete(GatewayEnrollment).where(GatewayEnrollment.id.in_(ids)))
        rows = [
            {
                "display_name": "Arruolamento SIM in attesa",
                "serial_number": "E2E-FACTORY-WAIT-001",
                "activation_mode": "sms",
                "mac_address": "02:AA:00:00:08:01",
            },
            {
                "display_name": "Arruolamento manuale",
                "serial_number": "E2E-FACTORY-MANUAL-002",
                "activation_mode": "wifi",
                "mac_address": "02:AA:00:00:08:02",
                "imei": "012345678901234",
                "iccid": "08912345678901234567",
            },
            {
                "display_name": "Arruolamento duplicati",
                "serial_number": "E2E-FACTORY-DUPLICATE-003",
                "activation_mode": "sms",
                "phone_number": "+393339998003",
                "mac_address": "02:AA:00:00:08:02",
                "imei": "012345678901234",
                "iccid": "08912345678901234567",
            },
        ]
        for identifier_, values in zip(ids, rows, strict=True):
            db.add(
                GatewayEnrollment(
                    id=identifier_,
                    station_id="browser-test-station",
                    request_key=uuid4(),
                    request_hash=hashlib.sha256(str(identifier_).encode()).hexdigest(),
                    secret_hash=hashlib.sha256(
                        ("browser-secret-" + str(identifier_)).encode()
                    ).hexdigest(),
                    original_serial_number=values["serial_number"],
                    external_reference="browser-fixture",
                    created_at=utc_now() - timedelta(days=180),
                    **values,
                )
            )
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
