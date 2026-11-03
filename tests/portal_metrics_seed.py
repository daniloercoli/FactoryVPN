"""Synthetic time series only for the isolated browser test database."""

import asyncio
import os
import sys
from datetime import timedelta
from uuid import UUID
from sqlalchemy import delete, select
from api.db.session import async_session_maker, engine
from api.models.telemetry import TelemetryBucket
from api.models.gateway import Gateway
from api.models.server_status import SmtpCheck
from api.services.credentials import current_credential
from api.services.telemetry_history import align
from api.time_utils import utc_now
from test_metrics_history_postgres import bucket

if (
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL")
    or os.environ.get("DATABASE_URL") != os.environ["FACTORYVPN_TEST_DATABASE_URL"]
):
    raise RuntimeError("Only isolated browser fixtures are supported")


async def seed():
    async with async_session_maker() as db:
        await db.execute(delete(SmtpCheck))
        if "--reset-smtp" not in sys.argv:
            await db.execute(delete(TelemetryBucket))
            end = align(utc_now(), 300)
            gateways = (
                await db.scalars(
                    select(Gateway).where(
                        Gateway.id.in_(
                            [UUID(f"00000000-0000-4000-8000-{n:012d}") for n in (20, 23)]
                        )
                    )
                )
            ).all()
            for gateway in [None, *gateways]:
                credential = await current_credential(db, gateway.id) if gateway else None
                for n in range(72):
                    if 15 <= n <= 25:
                        continue
                    row = bucket(
                        end - timedelta(minutes=5 * n),
                        20 + n % 8 * 5,
                        10,
                        gateway.id if gateway else None,
                        gateway.group_id if gateway else None,
                    )
                    row.cpu_max += 12
                    row.disk_sum, row.disk_count, row.disk_max = 710, 10, 72
                    row.context_id = credential.id if credential else UUID(int=0)
                    db.add(row)
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
