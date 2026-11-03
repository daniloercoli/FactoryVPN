"""Real PostgreSQL aggregation, privacy, bounds and deduplication."""

import asyncio
import os
from datetime import datetime, timedelta
from uuid import UUID, uuid4
import pytest
from sqlalchemy import select, func, text
from api.models.telemetry import TelemetryBucket as Bucket, TelemetrySnapshot
from api.schemas.telemetry import ServerTelemetry
from api.services import telemetry_history as history
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_telemetry_postgres import report, post_gateway

access = access_fixture
encoded_password = password_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


def bucket(at, cpu=20, samples=1, gateway_id=None, group_id=None):
    values = dict(
        source=f"gateway:{gateway_id}" if gateway_id else "server:worker",
        gateway_id=gateway_id,
        group_id=group_id,
        context_id=UUID(int=0),
        bucket_at=at,
        first_observed_at=at,
        last_observed_at=at,
        received_at=at,
        samples=samples,
    )
    for name, value in (("cpu", cpu), ("memory", 60), ("disk", None)):
        values.update(
            {
                name + "_sum": (value or 0) * samples,
                name + "_count": samples if value is not None else 0,
                name + "_max": value,
            }
        )
    return Bucket(**values)


@pytest.mark.parametrize("period", ["1h", "24h", "7d", "30d"])
@pytest.mark.parametrize("phase", [0, 120])
async def test_history_includes_earliest_retained_sample_in_requested_period(
    access, monkeypatch, period, phase
):
    now = history.align(utc_now(), 3600) + timedelta(seconds=phase)
    monkeypatch.setattr(history, "utc_now", lambda: now)
    duration, resolution = history.PERIODS[period]
    observed = now - timedelta(seconds=duration - 10)
    row = bucket(history.align(observed, 300), cpu=75)
    row.first_observed_at = row.last_observed_at = row.received_at = observed
    async with access.db() as db:
        db.add(row)
        await db.commit()
    response = await access.call("GET", f"/telemetry/server/history?period={period}")
    assert response.status_code == 200
    value = response.json()
    assert sum(p["samples"] for p in value["points"]) == 1
    assert datetime.fromisoformat(value["since"]) <= now - timedelta(seconds=duration)
    assert len(value["points"]) <= duration // resolution + 1


async def test_live_reports_aggregate_once_with_gaps_and_nullable_metrics(access):
    identifier = access.gateways["A"].id
    observed = history.align(utc_now(), 300) + timedelta(seconds=1)
    body = report()
    body["observed_at"] = observed.isoformat()
    body["metrics"] = {"cpu_percent": 20}
    results = await asyncio.gather(*(post_gateway(access, body=body) for _ in range(4)))
    assert sum(r.json()["recorded"] for r in results) == 1
    async with access.db() as db:
        snapshot = await db.get(TelemetrySnapshot, f"gateway:{identifier}")
        snapshot.received_at -= timedelta(seconds=11)
        await db.commit()
    body["observed_at"] = (observed + timedelta(seconds=1)).isoformat()
    body["metrics"] = {"cpu_percent": 60, "memory_total_bytes": 100, "memory_available_bytes": 25}
    assert (await post_gateway(access, body=body)).json()["recorded"]
    body["observed_at"] = (observed - timedelta(seconds=1)).isoformat()
    assert not (await post_gateway(access, body=body)).json()["recorded"]
    response = await access.call(
        "GET", f"/telemetry/gateways/{identifier}/history?period=1h", "admin_a"
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["points"]) == 13
    points = [p for p in data["points"] if p["samples"]]
    assert len(points) == 1
    assert points[0]["samples"] == 2
    assert points[0]["cpu"] == {"mean": 40, "peak": 60, "samples": 2}
    assert points[0]["memory"] == {"mean": 75, "peak": 75, "samples": 1}
    assert points[0]["disk"] == {"mean": None, "peak": None, "samples": 0}
    assert data["points"][0]["cpu"]["mean"] is None


async def test_weighted_rollups_and_all_periods_are_bounded(access):
    at = history.align(utc_now(), 3600) - timedelta(hours=1)
    async with access.db() as db:
        db.add_all(
            [
                bucket(at, 20, 3),
                bucket(at + timedelta(minutes=5), 80, 1),
                bucket(at - timedelta(days=31), 99),
            ]
        )
        await db.commit()
    for period, count, seconds in (
        ("1h", 13, 300),
        ("24h", 289, 300),
        ("7d", 337, 1800),
        ("30d", 721, 3600),
    ):
        response = await access.call("GET", f"/telemetry/server/history?period={period}")
        assert response.status_code == 200, response.text
        value = response.json()
        assert len(value["points"]) == count and value["resolution_seconds"] == seconds
        assert datetime.fromisoformat(value["available_from"]) == at
        if period in ("7d", "30d"):
            points = [p for p in value["points"] if p["samples"]]
            assert len(points) == 1
            assert points[0]["cpu"] == {"mean": 35, "peak": 80, "samples": 4}
    assert (await access.call("GET", "/telemetry/server/history?period=365d")).status_code == 422


async def test_history_requires_current_and_historical_group_and_cascades(access):
    await post_gateway(access)
    identifier = access.gateways["A"].id
    path = f"/telemetry/gateways/{identifier}/history"
    assert (await access.call("GET", path, "tech_a")).status_code == 403
    assert (await access.call("GET", path, "admin_b")).status_code == 404
    assert (await access.call("GET", "/telemetry/server/history", "admin_a")).status_code == 403
    assert (
        await access.call(
            "PUT",
            f"/admin/gateways/{identifier}/group",
            json={"group_id": str(access.groups["B"].id)},
        )
    ).status_code == 200
    assert (await access.call("GET", path, "admin_a")).status_code == 404
    data = (await access.call("GET", path, "admin_b")).json()
    assert data["available_from"] is None and sum(p["samples"] for p in data["points"]) == 0
    assert sum(p["samples"] for p in (await access.call("GET", path)).json()["points"]) == 1
    async with access.db() as db:
        db.add(
            bucket(
                history.align(utc_now(), 300), gateway_id=identifier, group_id=access.groups["B"].id
            )
        )
        await db.commit()
    assert (
        sum(p["samples"] for p in (await access.call("GET", path, "admin_b")).json()["points"]) == 1
    )
    await access.call("DELETE", f"/admin/gateways/{identifier}")
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(Bucket)) == 0


async def test_retention_hard_caps_and_nonblocking_history_lock(access, monkeypatch):
    monkeypatch.setattr(history, "SOURCE_LIMIT", 2)
    monkeypatch.setattr(history, "INSTANCE_LIMIT", 3)
    now = utc_now()
    at = history.align(now, 300)
    async with access.db() as db:
        db.add_all([bucket(at - timedelta(minutes=5 * i)) for i in range(1, 5)])
        extra = bucket(at - timedelta(days=31))
        extra.source, extra.context_id = "gateway:old", uuid4()
        db.add(extra)
        await db.commit()
        await history.record(
            db, "server:worker", None, ServerTelemetry.model_validate(report(True)), now
        )
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(Bucket)) == 3
        assert (
            await db.scalar(
                select(func.count()).select_from(Bucket).where(Bucket.source == "server:worker")
            )
            == 2
        )
        await history.prune(db)
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(Bucket)) == 2
    async with access.db() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": history.HISTORY_LOCK}
        )
        # An occupied history lock must not stall live observations/VPN authorization.
        result = await asyncio.wait_for(post_gateway(access), 2)
        assert result.json()["recorded"]
        await holder.rollback()
    async with access.db() as db:
        assert await db.get(TelemetrySnapshot, f'gateway:{access.gateways["A"].id}')
        assert await db.scalar(select(func.count()).select_from(Bucket)) == 2
