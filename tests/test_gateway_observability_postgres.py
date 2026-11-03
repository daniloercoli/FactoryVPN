import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, func, delete
from api.models.session import Session
from api.models.gateway import Gateway
from api.models.gateway_observability import GatewayTechnicalLog
from api.services import gateway_observability as service
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)

access, encoded_password = access_fixture, password_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


def event(code="agent_started", session_id=None, **changes):
    now = utc_now().isoformat()
    return dict(
        event_id=str(uuid4()),
        observed_at=now,
        first_observed_at=now,
        code=code,
        session_id=str(session_id) if session_id else None,
        **changes,
    )


async def post(access, events, group="A"):
    return await access.client.post(
        "/api/v1/gateways/technical-logs",
        json={"events": events},
        headers={"X-Gateway-Key": access.keys[str(access.gateways[group].id)]},
    )


def report(**changes):
    return dict(
        report_id=str(uuid4()),
        observed_at=utc_now().isoformat(),
        components={
            "agent": {"state": "reported", "version": "2.0.0", "origin": "build"},
            "modem": {"state": "reported", "imei": "123456789012345"},
        },
        **changes,
    )


async def system_post(access, body):
    return await access.client.post(
        "/api/v1/gateways/system-info",
        json=body,
        headers={"X-Gateway-Key": access.keys[str(access.gateways["A"].id)]},
    )


async def session(access, owner="tech_a"):
    row = Session(
        id=uuid4(),
        gateway_id=access.gateways["A"].id,
        group_id=access.groups["A"].id,
        technician_id=access.users[owner].id,
        status="CLOSED",
        requested_at=utc_now(),
        cleaned_at=utc_now(),
    )
    async with access.db() as db:
        db.add(row)
        await db.commit()
    return row


async def test_components_partial_failure_replay_scope_and_transfer(access):
    identifier = access.gateways["A"].id
    path = f"/gateways/{identifier}/system-info"
    body = report()
    assert (await system_post(access, body)).json() == {"recorded": True}
    assert (await system_post(access, body)).json() == {"recorded": False}
    failed = report()
    failed["components"] = {"modem": {"state": "collection_failed"}}
    assert (await system_post(access, failed)).json()["recorded"]
    view = (await access.call("GET", path, "tech_a")).json()
    assert view["components"]["modem"]["last_success"]["imei"] == "123456789012345"
    assert view["components"]["modem"]["latest"]["state"] == "collection_failed"
    assert view["components"]["agent"]["latest"]["version"] == "2.0.0"
    assert (await access.call("GET", path, "admin_b")).status_code == 404
    body["observed_at"] = (utc_now() + timedelta(minutes=1)).isoformat()
    assert (await system_post(access, body)).status_code == 422
    body["observed_at"] = (utc_now() - timedelta(minutes=6)).isoformat()
    assert not (await system_post(access, body)).json()["recorded"]
    await access.call(
        "PUT", f"/admin/gateways/{identifier}/group", json={"group_id": str(access.groups["B"].id)}
    )
    assert (await access.call("GET", path, "admin_a")).status_code == 404
    assert (await access.call("GET", path, "admin_b")).json()["components"] == {}
    assert (await system_post(access, report())).status_code == 401


async def test_log_permissions_session_ownership_count_csv_and_deleted_session(access):
    identifier = access.gateways["A"].id
    path = f"/gateways/{identifier}/technical-logs"
    own = await session(access)
    other = await session(access, "shared")
    assert (
        await post(
            access, [event(), event("tunnel_timeout", own.id), event("tunnel_failed", other.id)]
        )
    ).json()["accepted"] == 3
    assert (await access.call("GET", path, "tech_a")).status_code == 403
    await access.call(
        "PUT",
        f'/admin/users/{access.users["tech_a"].id}',
        json={"permissions": ["connect", "logs.read"]},
    )
    await access.login("tech_a")
    result = await access.call("GET", path, "tech_a")
    assert result.headers["X-Total-Count"] == "2"
    assert {e["code"] for e in result.json()} == {"agent_started", "tunnel_timeout"}
    exported = await access.call("GET", path + ".csv", "tech_a")
    assert "tunnel_timeout" in exported.text and "tunnel_failed" not in exported.text
    assert (await access.call("GET", path, "admin_b")).status_code == 404
    assert (await access.call("GET", path, "admin_a")).headers["X-Total-Count"] == "3"
    async with access.db() as db:
        await db.execute(delete(Session).where(Session.id == other.id))
        await db.commit()
    assert (await access.call("GET", path, "tech_a")).headers["X-Total-Count"] == "2"
    hidden = await access.call("GET", path, "tech_a", params={"session_id": str(other.id)})
    assert hidden.json() == [] and hidden.headers["X-Total-Count"] == "0"


async def test_retries_concurrent_batches_and_ingestion_do_not_change_gateway(access):
    body = [event()]
    async with access.db() as db:
        gw = await db.get(Gateway, access.gateways["A"].id)
        before = (gw.last_seen, gw.status)
    results = await asyncio.gather(*(post(access, body) for _ in range(4)))
    assert all(r.status_code == 200 for r in results)
    assert sum(r.json()["accepted"] for r in results) == 1
    async with access.db() as db:
        gw = await db.get(Gateway, access.gateways["A"].id)
        assert (gw.last_seen, gw.status) == before
        assert await db.scalar(select(func.count()).select_from(GatewayTechnicalLog)) == 1


async def test_untrusted_payloads_wrong_sources_cross_gateway_and_time_bounds(access):
    own = await session(access)
    assert (await post(access, [event("tunnel_failed")])).status_code == 422
    assert (await post(access, [event("sms_failed", own.id)])).status_code == 422
    rejected = await post(
        access, [event("agent_started", parameters={"password": "never-store-this"})]
    )
    assert rejected.status_code == 422 and "never-store-this" not in rejected.text
    assert (await post(access, [event("tunnel_failed", own.id)], group="B")).json()[
        "discarded"
    ] == 1
    stale = event()
    stale["first_observed_at"] = stale["observed_at"] = (
        utc_now() - timedelta(hours=25)
    ).isoformat()
    assert (await post(access, [stale])).json()["discarded"] == 1
    request = await access.client.post("/api/v1/gateways/technical-logs", content=b" " * 65537)
    assert request.status_code == 413
    assert (
        await access.client.post(
            "/api/v1/gateways/technical-logs",
            json={"events": [event()]},
            headers=access.headers["root"],
        )
    ).status_code == 401


async def test_hard_caps_retention_filters_and_deletion(access, monkeypatch):
    monkeypatch.setattr(service, "GATEWAY_LIMIT", 3)
    monkeypatch.setattr(service, "INSTANCE_LIMIT", 4)
    await post(access, [event() for _ in range(5)])
    await post(access, [event() for _ in range(3)], "B")
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(GatewayTechnicalLog)) == 4
        row = await db.scalar(select(GatewayTechnicalLog).order_by(GatewayTechnicalLog.id))
        row.received_at = utc_now() - timedelta(days=31)
        await db.commit()
        await service.prune_logs(db)
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(GatewayTechnicalLog)) == 3
    path = f'/gateways/{access.gateways["B"].id}/technical-logs'
    assert (await access.call("GET", path, "admin_b", params={"q": "avviato", "limit": 1})).headers[
        "X-Total-Count"
    ] == "3"
    assert (await access.call("GET", path, "admin_b", params={"q": "%"})).json() == []
    assert (
        await access.call(
            "GET",
            path,
            "admin_b",
            params={
                "since": utc_now().isoformat(),
                "until": (utc_now() - timedelta(days=1)).isoformat(),
            },
        )
    ).status_code == 422
    await access.call("DELETE", f'/admin/gateways/{access.gateways["B"].id}')
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(GatewayTechnicalLog)) == 0


async def test_transfer_preserves_history_and_worker_uses_session_group(access):
    identifier = access.gateways["A"].id
    old = await session(access)
    await post(access, [event("tunnel_failed", old.id)])
    await access.call(
        "PUT", f"/admin/gateways/{identifier}/group", json={"group_id": str(access.groups["B"].id)}
    )
    result = await access.client.post(
        f"/api/v1/internal/gateways/{identifier}/technical-logs",
        json={"events": [event("cleanup_failed", old.id)]},
        headers=access.worker,
    )
    assert result.status_code == 200 and result.json()["accepted"] == 1
    path = f"/gateways/{identifier}/technical-logs"
    assert (await access.call("GET", path, "admin_a")).status_code == 404
    assert (await access.call("GET", path, "admin_b")).json() == []
    assert (await access.call("GET", path)).headers["X-Total-Count"] == "2"


async def test_rate_limit_accepts_retries_without_consuming_new_event_budget(access, monkeypatch):
    monkeypatch.setattr(service, "RATE_LIMIT", 3)
    events = [event() for _ in range(3)]
    assert (await post(access, events)).json()["accepted"] == 3
    assert (await post(access, events)).json()["duplicates"] == 3
    limited = await post(access, [event()])
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


async def test_sms_without_mac_can_be_found_in_telemetry_by_uuid(access):
    identifier = access.gateways["A"].id
    async with access.db() as db:
        gateway = await db.get(Gateway, identifier)
        gateway.activation_mode, gateway.mac_address, gateway.display_name = "sms", None, None
        await db.commit()
    result = await access.call(
        "GET", "/telemetry/gateways", "admin_a", params={"q": str(identifier)}
    )
    assert result.status_code == 200
    assert len(result.json()) == 1 and result.json()[0]["mac_address"] is None


async def test_credential_rotation_separates_deduplication_and_invalidates_snapshot(access):
    from api.models.provisioning import GatewayCredential
    from api.services.credentials import current_credential, digest, encrypt

    identifier = access.gateways["A"].id
    events = [event()]
    assert (await post(access, events)).json()["accepted"] == 1
    await system_post(access, report())
    replacement = str(uuid4())
    async with access.db() as db:
        previous = await current_credential(db, identifier)
        previous.active, previous.revoked_at = False, utc_now()
        await db.flush()
        db.add(
            GatewayCredential(
                gateway_id=identifier,
                secret_hash=digest(replacement),
                sms_secret_encrypted=encrypt(replacement.encode()),
                active=True,
            )
        )
        await db.commit()
    assert (await post(access, events)).status_code == 401
    assert (await access.call("GET", f"/gateways/{identifier}/system-info")).json()[
        "components"
    ] == {}
    access.keys[str(identifier)] = replacement
    assert (await post(access, events)).json()["accepted"] == 1
    assert (await post(access, events)).json()["duplicates"] == 1


async def test_api_log_writer_captures_session_scope_before_gateway_transfer(access, monkeypatch):
    from api.services import technical_log_writer as writer
    from factoryvpn_common.technical_logs import LogQueue

    monkeypatch.setattr(writer, "queue", LogQueue("api"))
    own = await session(access)
    writer.emit(own, "sms_failed")
    await writer.flush_once(access.db)
    identifier = access.gateways["A"].id
    path = f"/gateways/{identifier}/technical-logs"
    rows = (await access.call("GET", path, "admin_a")).json()
    assert len(rows) == 1 and rows[0]["source"] == "api"
    writer.emit(own, "sms_failed")
    await access.call(
        "PUT", f"/admin/gateways/{identifier}/group", json={"group_id": str(access.groups["B"].id)}
    )
    await writer.flush_once(access.db)
    assert (await access.call("GET", path, "admin_b")).json() == []
    assert (await access.call("GET", path)).headers["X-Total-Count"] == "1"
