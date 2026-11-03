import asyncio
import os
import threading
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select
from api.config import settings
from api.models.config import Config
from api.models.telemetry import TelemetrySnapshot
from api.models.server_status import SmtpCheck
from api.models.audit_log import AuditLog
from api.models.user import User
from api.services import server_status as service
from api.services.lifecycle import allocation_lock
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_telemetry_postgres import report

access = access_fixture
encoded_password = password_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


def request(mode="connection"):
    return {"request_id": str(uuid4()), "mode": mode}


async def test_status_is_global_safe_and_uses_actual_runtime_reports(access, monkeypatch):
    monkeypatch.setattr(service, "probe", lambda recipient: pytest.fail("GET must not probe SMTP"))
    monkeypatch.setattr(settings, "SMTP_HOST", "mail.example.test")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "private-user")
    monkeypatch.setattr(settings, "SMTP_CA_FILE", "/private/certificates/ca.pem")
    for actor in ("admin_a", "tech_a", "observer"):
        assert (await access.call("GET", "/admin/server", actor)).status_code == 403
        assert (
            await access.call("POST", "/admin/server/smtp-check", actor, json=request())
        ).status_code == 403
    response = await access.call("GET", "/admin/server")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["smtp"]["authentication"] and body["smtp"]["custom_ca"]
    assert "private-user" not in response.text and "/private/" not in response.text
    components = {r["name"]: r for r in body["components"]}
    assert components["api"]["version"] and components["python_api"]["version"]
    assert components["postgresql"]["version"] and components["worker"]["state"] == "unknown"
    body = report(
        True,
        runtime={
            "version": "3.2.1",
            "build": "worker-build",
            "python": "3.13.2",
            "openvpn": "2.6.15",
        },
    )
    assert (
        await access.client.post(
            "/api/v1/internal/telemetry/server", json=body, headers=access.worker
        )
    ).json()["recorded"]
    async with access.db() as db:
        db.add_all(
            [
                Config(key="provisioning_worker", value="ok", updated_at=utc_now()),
                Config(
                    key="provisioning_runtime", value='{"version":"3.1.0"}', updated_at=utc_now()
                ),
            ]
        )
        await db.commit()
    components = {
        r["name"]: r for r in (await access.call("GET", "/admin/server")).json()["components"]
    }
    assert components["worker"]["version"] == "3.2.1" and components["worker"]["state"] == "ok"
    assert components["openvpn"]["version"] == "2.6.15"
    assert components["provisioning"]["version"] == "3.1.0"
    async with access.db() as db:
        row = await db.get(TelemetrySnapshot, "server:worker")
        row.received_at -= timedelta(seconds=100)
        await db.commit()
    components = {
        r["name"]: r for r in (await access.call("GET", "/admin/server")).json()["components"]
    }
    assert components["worker"]["version"] == "3.2.1" and components["worker"]["state"] == "unknown"


async def test_certificate_and_backup_status_are_global_and_safe(access, tmp_path, monkeypatch):
    from test_server_readiness import write_certificate
    from factoryvpn_common.backup_catalog import Catalog

    path = tmp_path / "private-certificate.pem"
    write_certificate(path, utc_now() - timedelta(days=1), utc_now() + timedelta(days=10))
    catalog = Catalog(tmp_path / "private-control")
    catalog.initialize()
    catalog.meta("heartbeat", utc_now().isoformat())
    catalog.add_backup(uuid4(), 12, "private-hash", {"secret": "private-manifest"})
    monkeypatch.setattr(settings, "STATUS_HTTPS_CERTIFICATE_FILE", str(path))
    monkeypatch.setattr(settings, "BACKUP_CONTROL_DIR", str(catalog.root))
    monkeypatch.setattr(settings, "BACKUP_ARCHIVE_DIR", "/private-archives")
    response = await access.call("GET", "/admin/server")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["certificates"][0]["state"] == "expiring"
    assert body["backup"]["copies"] == 1 and body["backup"]["coordinator_state"] == "online"
    assert "private-" not in response.text
    assert catalog.jobs() == []
    assert (await access.call("GET", "/admin/server", "admin_a")).status_code == 403
    # Local failures degrade only the relevant section, not all server status.
    path.write_text("private-malformed-pem")
    catalog.path.unlink()
    response = await access.call("GET", "/admin/server")
    assert response.status_code == 200
    assert response.json()["certificates"][0]["state"] == "unreadable"
    assert response.json()["backup"]["state"] == "unavailable"
    assert not catalog.path.exists()


async def test_smtp_probe_idempotent_throttled_and_audited_without_vpn_lock(access, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def probe(recipient):
        assert recipient is None
        started.set()
        assert release.wait(5)
        return "connected"

    monkeypatch.setattr(service, "probe", probe)
    body = request()
    job = asyncio.create_task(access.call("POST", "/admin/server/smtp-check", json=body))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        async with access.db() as db:
            await asyncio.wait_for(allocation_lock(db), 1)
            await db.rollback()
        duplicate = await access.call("POST", "/admin/server/smtp-check", json=body)
        assert duplicate.status_code == 200 and duplicate.json()["code"] == "running"
        rejected = await access.call("POST", "/admin/server/smtp-check", json=request())
        assert rejected.status_code == 429 and int(rejected.headers["Retry-After"]) > 0
    finally:
        release.set()
    response = await job
    assert response.status_code == 200 and response.json()["code"] == "connected"
    assert (
        await access.call("POST", "/admin/server/smtp-check", json=body)
    ).json() == response.json()
    assert (
        await access.call("POST", "/admin/server/smtp-check", json={**body, "mode": "send"})
    ).status_code == 409
    async with access.db() as db:
        rows = (
            await db.scalars(select(AuditLog).where(AuditLog.event_type.like("smtp_check_%")))
        ).all()
        assert {r.event_type for r in rows} == {"smtp_check_requested", "smtp_check_finished"}
        assert all(r.group_id is None for r in rows)


async def test_send_requires_own_verified_address_and_shared_hourly_limit(access, monkeypatch):
    recipients = []
    monkeypatch.setattr(
        service, "probe", lambda recipient: recipients.append(recipient) or "accepted"
    )
    assert (
        await access.call("POST", "/admin/server/smtp-check", json=request("send"))
    ).status_code == 409
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        user.email_verified_at = utc_now()
        await db.commit()
    assert (
        await access.call(
            "POST",
            "/admin/server/smtp-check",
            json={**request("send"), "recipient": "other@example.test"},
        )
    ).status_code == 422
    response = await access.call("POST", "/admin/server/smtp-check", json=request("send"))
    assert response.status_code == 200, response.text
    assert recipients == ["root@example.test"]
    async with access.db() as db:
        row = await db.get(SmtpCheck, 1)
        row.started_at -= timedelta(seconds=61)
        row.window_count = 6
        await db.commit()
    rejected = await access.call("POST", "/admin/server/smtp-check", json=request())
    assert rejected.status_code == 429 and int(rejected.headers["Retry-After"]) > 3000
    async with access.db() as db:
        row = await db.get(SmtpCheck, 1)
        row.window_at -= timedelta(hours=1)
        await db.commit()
    assert (
        await access.call("POST", "/admin/server/smtp-check", json=request())
    ).status_code == 200


async def test_timed_out_probe_does_not_accumulate_threads(access, monkeypatch):
    release = threading.Event()
    calls = []

    def probe(recipient):
        calls.append(recipient)
        assert release.wait(5)
        return "connected"

    monkeypatch.setattr(service, "probe", probe)
    monkeypatch.setattr(service, "PROBE_TIMEOUT", 0.03)
    try:
        response = await access.call("POST", "/admin/server/smtp-check", json=request())
        assert response.json()["code"] == "timeout"
        async with access.db() as db:
            row = await db.get(SmtpCheck, 1)
            row.started_at -= timedelta(seconds=61)
            await db.commit()
        for _ in range(3):
            assert (
                await access.call("POST", "/admin/server/smtp-check", json=request())
            ).status_code == 429
        assert len(calls) == 1
    finally:
        release.set()
        if service._active_probe:
            await service._active_probe


async def test_late_probe_returns_its_own_result_after_another_process_checks(access, monkeypatch):
    started, release = threading.Event(), threading.Event()
    clock = [utc_now()]
    calls = []

    def probe(recipient):
        calls.append(recipient)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            return "connection_failed"
        return "connected"

    monkeypatch.setattr(service, "probe", probe)
    monkeypatch.setattr(service, "utc_now", lambda: clock[0])
    first, second = request(), request()
    pending = asyncio.create_task(access.call("POST", "/admin/server/smtp-check", json=first))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        # A suspended process can resume after the shared cooldown. A second API
        # process has its own thread guard but uses the same database singleton.
        clock[0] += timedelta(seconds=61)
        monkeypatch.setattr(service, "_active_probe", None)
        following = await access.call("POST", "/admin/server/smtp-check", json=second)
        assert following.status_code == 200
        assert following.json()["code"] == "connected"
    finally:
        release.set()
    earlier = await pending
    assert earlier.status_code == 200
    assert earlier.json()["request_id"] == first["request_id"]
    assert earlier.json()["code"] == "connection_failed"
    async with access.db() as db:
        current = await db.get(SmtpCheck, 1)
        assert str(current.request_id) == second["request_id"] and current.code == "connected"
