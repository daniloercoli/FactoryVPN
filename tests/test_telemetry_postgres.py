import asyncio
import os
from datetime import timedelta
from sqlalchemy import select, func
import pytest
from api.models.gateway import Gateway
from api.models.telemetry import TelemetrySnapshot
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)

access = access_fixture
encoded_password = password_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


def report(server=False, **changes):
    return dict(
        observed_at=utc_now().isoformat(),
        metrics={
            "cpu_percent": 25,
            "memory_total_bytes": 1000,
            "memory_available_bytes": 400,
            "disk_total_bytes": 5000,
            "disk_available_bytes": 2000,
            "uptime_seconds": 120,
        },
        services=(
            {"worker": "ok", "openvpn": "ok"}
            if server
            else {"agent": "ok", "control": "ok", "openvpn": "idle", "network": "idle"}
        ),
        **changes,
    )


async def post_gateway(access, group="A", body=None):
    return await access.client.post(
        "/api/v1/gateways/telemetry",
        json=body or report(),
        headers={"X-Gateway-Key": access.keys[str(access.gateways[group].id)]},
    )


async def test_human_reads_need_explicit_permission_and_server_is_global_only(access):
    assert (await post_gateway(access)).json() == {"recorded": True}
    assert (await post_gateway(access, "B")).json() == {"recorded": True}
    for user in ("tech_a", "observer", "limited"):
        assert (await access.call("GET", "/telemetry/gateways", user)).status_code == 403
    for user, group in (("admin_a", "A"), ("admin_b", "B")):
        result = await access.call("GET", "/telemetry/gateways", user)
        assert result.status_code == 200 and result.headers["X-Total-Count"] == "1"
        assert result.json()[0]["gateway_id"] == str(access.gateways[group].id)
        assert result.json()[0]["metrics"]["cpu_percent"] == 25
        assert (await access.call("GET", "/telemetry/server", user)).status_code == 403
    assert (
        await access.call("GET", f'/telemetry/gateways/{access.gateways["B"].id}', "admin_a")
    ).status_code == 404
    filtered = await access.call(
        "GET", "/telemetry/gateways", "admin_a", params={"group_id": str(access.groups["B"].id)}
    )
    assert filtered.json() == [] and filtered.headers["X-Total-Count"] == "0"
    root = await access.call("GET", "/telemetry/server")
    assert root.json()["freshness"] == "missing" and root.json()["metrics"] is None
    assert root.json()["services"] == {
        "worker": "unknown",
        "openvpn": "unknown",
        "api": "ok",
        "database": "ok",
    }


async def test_machine_identity_is_derived_from_key_and_payload_is_bounded(access):
    assert (
        await access.client.post(
            "/api/v1/gateways/telemetry", json=report(), headers=access.headers["root"]
        )
    ).status_code == 401
    assert (
        await post_gateway(access, body=report(gateway_id=str(access.gateways["B"].id)))
    ).status_code == 422
    body = report()
    body["metrics"]["disk_available_bytes"] = 9000
    assert (await post_gateway(access, body=body)).status_code == 422
    body = report()
    body["services"]["database"] = "ok"
    assert (await post_gateway(access, body=body)).status_code == 422
    path = "/api/v1/internal/telemetry/server"
    assert (
        await access.client.post(
            path,
            json=report(True),
            headers={"X-Gateway-Key": access.keys[str(access.gateways["A"].id)]},
        )
    ).status_code == 401
    assert (await access.client.post(path, json=report(True), headers=access.worker)).json()[
        "recorded"
    ]
    assert (await access.call("GET", "/telemetry/server")).json()["services"]["worker"] == "ok"


async def test_stale_duplicate_and_out_of_order_reports_never_look_current(access):
    body = report()
    results = await asyncio.gather(*(post_gateway(access, body=body) for _ in range(4)))
    assert sum(r.json()["recorded"] for r in results) == 1
    async with access.db() as db:
        row = await db.scalar(select(TelemetrySnapshot))
        row.received_at = utc_now() - timedelta(seconds=100)
        row.observed_at = utc_now() - timedelta(seconds=100)
        await db.commit()
    result = (await access.call("GET", "/telemetry/gateways", "admin_a")).json()[0]
    assert result["freshness"] == "stale" and result["metrics"] is None
    assert result["valid_for_seconds"] == 0
    assert set(result["services"].values()) == {"unknown"}
    old = report()
    old["observed_at"] = (utc_now() - timedelta(minutes=6)).isoformat()
    assert not (await post_gateway(access, body=old)).json()["recorded"]
    old["observed_at"] = (utc_now() + timedelta(minutes=1)).isoformat()
    assert (await post_gateway(access, body=old)).status_code == 422
    assert (await post_gateway(access)).json()["recorded"]
    current = (await access.call("GET", "/telemetry/gateways", "admin_a")).json()[0]
    assert 85 < current["valid_for_seconds"] <= 90
    assert not (await post_gateway(access)).json()["recorded"]
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(TelemetrySnapshot)) == 1


async def test_gateway_move_revocation_and_deletion_do_not_leak_old_scope(access):
    await post_gateway(access)
    identifier = access.gateways["A"].id
    moved = await access.call(
        "PUT", f"/admin/gateways/{identifier}/group", json={"group_id": str(access.groups["B"].id)}
    )
    assert moved.status_code == 200
    assert (
        await access.call("GET", f"/telemetry/gateways/{identifier}", "admin_a")
    ).status_code == 404
    assert (
        await access.call("GET", f"/telemetry/gateways/{identifier}", "admin_b")
    ).status_code == 200
    deleted = await access.call("DELETE", f"/admin/gateways/{identifier}")
    assert deleted.status_code == 204
    assert (await post_gateway(access)).status_code == 401
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(TelemetrySnapshot)) == 0
        assert await db.get(Gateway, identifier) is None


async def test_telemetry_permission_can_be_granted_without_admin_and_revoked(access):
    user_id = access.users["tech_a"].id
    path = f"/admin/users/{user_id}"
    # A delegate who does not possess telemetry.read cannot grant it.
    rejected = await access.call(
        "PUT", path, "limited", json={"permissions": ["connect", "telemetry.read"]}
    )
    assert rejected.status_code == 403
    granted = await access.call("PUT", path, json={"permissions": ["connect", "telemetry.read"]})
    assert granted.status_code == 200
    assert (await access.call("GET", "/telemetry/gateways", "tech_a")).status_code == 401
    await access.login("tech_a")
    rows = await access.call("GET", "/telemetry/gateways", "tech_a")
    assert rows.status_code == 200 and rows.headers["X-Total-Count"] == "1"
    assert rows.json()[0]["gateway_id"] == str(access.gateways["A"].id)
    assert (await access.call("GET", "/telemetry/server", "tech_a")).status_code == 403
    removed = await access.call("PUT", path, json={"permissions": ["connect"]})
    assert removed.status_code == 200
    assert (await access.call("GET", "/telemetry/gateways", "tech_a")).status_code == 401
    await access.login("tech_a")
    assert (await access.call("GET", "/telemetry/gateways", "tech_a")).status_code == 403


async def test_telemetry_unassigned_and_multi_group_visibility(access):
    await access.call(
        "PUT",
        f'/admin/users/{access.users["shared"].id}',
        json={"permissions": ["telemetry.read"]},
    )
    await access.login("shared")
    rows = await access.call("GET", "/telemetry/gateways", "shared")
    assert {v["gateway_id"] for v in rows.json()} == {
        str(access.gateways[group].id) for group in ("A", "B")
    }
    unassigned = f'/telemetry/gateways/{access.gateways["unassigned"].id}'
    assert (await access.call("GET", unassigned, "shared")).status_code == 404
    assert (await access.call("GET", unassigned)).status_code == 200
    await access.call(
        "PUT",
        f'/admin/users/{access.users["shared"].id}',
        json={"group_ids": [str(access.groups["B"].id)]},
    )
    assert (await access.call("GET", "/telemetry/gateways", "shared")).status_code == 401
    await access.login("shared")
    assert (
        await access.call("GET", f'/telemetry/gateways/{access.gateways["A"].id}', "shared")
    ).status_code == 404
