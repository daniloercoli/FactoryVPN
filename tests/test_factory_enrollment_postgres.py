"""Arruolamento HTTP reale: prenotazioni, consegna, revoche e controllo HQ."""

from datetime import timedelta
import io
import os
from uuid import UUID, uuid4
import zipfile

from dotenv import dotenv_values
import pytest
from sqlalchemy import select

from api.config import settings
from api.models.enrollment import GatewayEnrollment
from api.models.gateway import Gateway
from api.models.provisioning import ProvisioningJob, GatewayCredential
from api.models.session import Session
from api.services.credentials import digest
from api.services.provisioning import current_certificate
from api.time_utils import utc_now
from scripts.provisioning_worker import cycle
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_provisioning_postgres import pki as pki_fixture, registration
from test_web_portal_postgres import browser as browser_fixture, login

access = access_fixture
encoded_password = password_fixture
pki = pki_fixture
browser = browser_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)
FACTORY_KEY = "test-station-key-" + "f" * 40
BOOT_KEY = "test-enrollment-key-" + "g" * 40


@pytest.fixture(autouse=True)
def station(monkeypatch):
    monkeypatch.setattr(settings, "FACTORY_STATIONS", {"line-1": digest(FACTORY_KEY)})
    monkeypatch.setattr(settings, "FACTORY_REQUESTS_PER_MINUTE", 600)
    monkeypatch.setattr(settings, "ENROLLMENT_REQUESTS_PER_MINUTE", 300)


async def prepare(access, **changes):
    body = {
        "gateway_id": str(uuid4()),
        "serial_number": "SER-" + uuid4().hex,
        "secret_hash": digest(BOOT_KEY),
        **changes,
    }
    headers = {"X-Factory-Key": FACTORY_KEY, "Idempotency-Key": str(uuid4())}
    result = await access.client.post("/api/v1/factory/gateways", json=body, headers=headers)
    assert result.status_code == 201, result.text
    return body, headers, result.json()


async def machine(access, identifier, method="GET", suffix="", **kwargs):
    return await access.client.request(
        method,
        f"/api/v1/gateway-enrollment/{identifier}" + suffix,
        headers={"X-Enrollment-Key": BOOT_KEY},
        **kwargs,
    )


async def approve(access, identifier):
    result = await access.call(
        "POST",
        f"/admin/enrollments/{identifier}/approve",
        json={"group_id": str(access.groups["A"].id)},
    )
    assert result.status_code == 200, result.text
    return result.json()


async def issued(access, pki, **changes):
    body, _, _ = await prepare(
        access, activation_mode="wifi", mac_address="02:00:00:00:00:51", **changes
    )
    state = await approve(access, body["gateway_id"])
    result = await machine(
        access,
        body["gateway_id"],
        "POST",
        "/package",
        json={"configuration_version": state["configuration_version"]},
    )
    assert result.status_code == 202, result.text
    await cycle(access.db, pki)
    state = (await machine(access, body["gateway_id"])).json()
    assert state["state"] == "ready", state
    return body, state


async def download_and_confirm(access, body, state):
    identifier, job = body["gateway_id"], state["job"]["id"]
    result = await machine(access, identifier, "POST", f"/package/{job}/download")
    assert result.status_code == 200, result.text
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        env = dotenv_values(stream=io.StringIO(archive.read("gateway.env").decode()))
        assert set(archive.namelist()) == {"gateway.env", "gateway.ovpn", "LEGGIMI.txt"}
        assert not any(key.startswith("LAN_") for key in env)
    confirmed = await machine(
        access,
        identifier,
        "POST",
        f"/package/{job}/confirm",
        json={"configuration_version": state["configuration_version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    return env


async def test_factory_idempotency_and_reservations_across_manual_paths(access):
    body, headers, state = await prepare(access)
    replay = await access.client.post("/api/v1/factory/gateways", json=body, headers=headers)
    assert replay.status_code == 201 and replay.json()["id"] == state["id"]
    assert state["gateway_id"] is None and state["vpn_ip"] is None
    assert BOOT_KEY not in replay.text and body["secret_hash"] not in replay.text
    conflict = await access.client.post(
        "/api/v1/factory/gateways", json={**body, "serial_number": "other"}, headers=headers
    )
    assert conflict.status_code == 409
    wrong = await access.client.post(
        "/api/v1/factory/gateways",
        json=body,
        headers={**headers, "X-Factory-Key": digest(FACTORY_KEY)},
    )
    assert wrong.status_code == 401
    for values in ({"gateway_id": body["gateway_id"]}, {"serial_number": body["serial_number"]}):
        result = await access.client.post(
            "/api/v1/provisioning/gateways",
            headers={**access.headers["root"], "Idempotency-Key": str(uuid4())},
            json=registration(access, **values),
        )
        assert result.status_code == 409, result.text
    manual = registration(access, serial_number="manual-first")
    result = await access.client.post(
        "/api/v1/provisioning/gateways",
        headers={**access.headers["root"], "Idempotency-Key": str(uuid4())},
        json=manual,
    )
    assert result.status_code == 202, result.text
    result = await access.client.post(
        "/api/v1/factory/gateways",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            **body,
            "gateway_id": str(uuid4()),
            "serial_number": "manual-first",
            "secret_hash": digest("different"),
        },
    )
    assert result.status_code == 409


async def test_incomplete_factory_approval_survives_logout_then_assigns_ip_on_registration(
    access, browser, pki
):
    body, _, _ = await prepare(access)
    identifier = body["gateway_id"]
    await login(browser)
    detail = (await browser.get(f"/api/v1/admin/enrollments/{identifier}")).json()
    result = await browser.post(
        f"/api/v1/admin/enrollments/{identifier}/approve",
        headers={"If-Match": '"' + detail["revision"] + '"'},
        json={"group_id": str(access.groups["A"].id)},
    )
    assert result.status_code == 200 and result.json()["state"] == "awaiting_data"
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    async with access.db() as db:
        assert await db.get(Gateway, UUID(identifier)) is None
    result = await machine(
        access,
        identifier,
        "PUT",
        json={"activation_mode": "wifi", "mac_address": "02:00:00:00:00:51"},
    )
    state = result.json()
    assert result.status_code == 200 and state["state"] == "approved"
    result = await machine(
        access,
        identifier,
        "POST",
        "/package",
        json={"configuration_version": state["configuration_version"]},
    )
    assert result.status_code == 202, result.text
    await cycle(access.db, pki)
    state = (await machine(access, identifier)).json()
    assert state["state"] == "ready" and state["vpn_ip"] is None
    # Né un certificato candidato né il segreto iniziale concedono accesso operativo.
    assert (
        await access.client.post(
            "/api/v1/gateways/register",
            headers={"X-Gateway-Key": BOOT_KEY},
            json={
                "gateway_id": identifier,
                "activation_mode": "wifi",
                "mac_address": "02:00:00:00:00:51",
            },
        )
    ).status_code == 401
    async with access.db() as db:
        job = await db.get(ProvisioningJob, UUID(state["job"]["id"]))
        assert (
            job.delivery_mode == "enrollment" and job.actor_id is None and job.access_hash is None
        )
    env = await download_and_confirm(access, body, state)
    assert (await machine(access, identifier)).status_code == 401
    assert (
        await machine(
            access,
            identifier,
            "POST",
            f'/package/{state["job"]["id"]}/confirm',
            json={"configuration_version": state["configuration_version"]},
        )
    ).status_code == 401
    request = {
        "gateway_id": identifier,
        "activation_mode": "wifi",
        "mac_address": env["MAC_ADDRESS"],
        "phone_number": None,
    }
    registered = await access.client.post(
        "/api/v1/gateways/register", headers={"X-Gateway-Key": env["GATEWAY_API_KEY"]}, json=request
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["vpn_ip"].startswith("10.100.")
    again = await access.client.post(
        "/api/v1/gateways/register", headers={"X-Gateway-Key": env["GATEWAY_API_KEY"]}, json=request
    )
    assert again.json()["vpn_ip"] == registered.json()["vpn_ip"]


@pytest.mark.parametrize("source", ["gateway", "hq"])
async def test_inventory_does_not_cancel_candidate_but_mac_does(access, pki, source):
    body, state = await issued(access, pki)
    identifier, job = body["gateway_id"], state["job"]["id"]
    manual_state = (await access.call("GET", f"/provisioning/peers/gateway/{identifier}")).json()
    assert manual_state["enrollment_pending"] and manual_state["pending_job"] is None
    assert (await access.call("POST", f"/provisioning/jobs/{job}/cancel")).status_code == 404

    async def edit(values):
        if source == "gateway":
            return await machine(access, identifier, "PUT", json=values)
        return await access.call("PATCH", f"/admin/enrollments/{identifier}", json=values)

    inventory = await edit({"imei": "012345678901234", "iccid": "08912345678901234567"})
    assert (
        inventory.status_code == 200
        and inventory.json()["configuration_version"] == state["configuration_version"]
    )
    assert inventory.json()["job"]["id"] == job and inventory.json()["job"]["can_download"]
    changed = await edit({"mac_address": "02:00:00:00:00:99"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["configuration_version"] == state["configuration_version"] + 1
    assert changed.json()["job"]["state"] == "cancelled"
    assert (
        await machine(access, identifier, "POST", f"/package/{job}/download")
    ).status_code == 409
    forbidden = await machine(access, identifier, "PUT", json={"serial_number": "spoofed"})
    assert forbidden.status_code == 422
    async with access.db() as db:
        row = await db.get(GatewayEnrollment, UUID(identifier))
        assert row.secret_hash and row.approved_at


async def test_manual_takeover_resume_and_activation_consume_initial_key(access, pki):
    body, state = await issued(access, pki)
    identifier = body["gateway_id"]
    for attempt in range(2):
        headers = {**access.headers["root"], "Idempotency-Key": str(uuid4())}
        result = await access.client.post(
            f"/api/v1/admin/enrollments/{identifier}/manual",
            headers=headers,
            json={"group_id": str(access.groups["A"].id)},
        )
        assert result.status_code == 202, result.text
        job_id = result.json()["id"]
        denied = await machine(
            access,
            identifier,
            "POST",
            "/package",
            json={"configuration_version": state["configuration_version"]},
        )
        assert denied.status_code == 409
        if attempt == 0:
            resumed = await access.call("POST", f"/admin/enrollments/{identifier}/resume")
            assert resumed.status_code == 200 and resumed.json()["delivery_mode"] == "automatic"
    await cycle(access.db, pki)
    result = await access.call("POST", f"/provisioning/jobs/{job_id}/download")
    assert result.status_code == 200, result.text
    result = await access.call("POST", f"/provisioning/jobs/{job_id}/activate")
    assert result.status_code == 200, result.text
    assert (await machine(access, identifier)).status_code == 401
    assert (await access.call("POST", f"/admin/enrollments/{identifier}/resume")).status_code == 409
    detail = (await access.call("GET", f"/admin/enrollments/{identifier}")).json()
    assert detail["state"] == "completed" and not detail["can_resume"]


async def test_manual_retry_after_lost_response_keeps_body_and_browser_binding(access, browser):
    body, _, _ = await prepare(access, activation_mode="wifi", mac_address="02:00:00:00:00:51")
    identifier = body["gateway_id"]
    await login(browser)
    original = (await browser.get(f"/api/v1/admin/enrollments/{identifier}")).json()
    headers = {"Idempotency-Key": str(uuid4()), "If-Match": '"' + original["revision"] + '"'}
    request = {"group_id": str(access.groups["A"].id)}
    path = f"/api/v1/admin/enrollments/{identifier}/manual"
    first = await browser.post(path, headers=headers, json=request)
    assert first.status_code == 202, first.text
    # Il client ritenta gli stessi byte dopo aver perso la risposta 202.
    replay = await browser.post(path, headers=headers, json=request)
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    different = await browser.post(
        path, headers=headers, json={"group_id": str(access.groups["B"].id)}
    )
    assert different.status_code == 409
    stale_new = await browser.post(
        path, headers={**headers, "Idempotency-Key": str(uuid4())}, json=request
    )
    assert stale_new.status_code == 412
    await login(browser)
    other_access = await browser.post(path, headers=headers, json=request)
    assert other_access.status_code == 409


async def test_admin_scope_revision_search_and_block(access, browser):
    body, _, _ = await prepare(access, mac_address="AA:BB:CC:DD:EE:FF", imei="012345678901234")
    identifier = body["gateway_id"]
    assert (await access.call("GET", "/admin/enrollments", actor="admin_a")).status_code == 403
    assert (
        await access.call("GET", f"/admin/enrollments/{identifier}", actor="tech_a")
    ).status_code == 403
    for query in (identifier, "aabb.ccdd.eeff", "012345678901234", body["serial_number"]):
        result = await access.call(
            "GET", "/admin/enrollments", params={"q": query, "limit": 1, "state": "prepared"}
        )
        assert result.status_code == 200, result.text
        assert result.headers["X-Total-Count"] == "1" and result.json()[0]["id"] == identifier
    await login(browser)
    detail = (await browser.get(f"/api/v1/admin/enrollments/{identifier}")).json()
    assert (
        await browser.patch(
            f"/api/v1/admin/enrollments/{identifier}", json={"display_name": "no revision"}
        )
    ).status_code == 428
    revision = {"If-Match": '"' + detail["revision"] + '"'}
    result = await browser.patch(
        f"/api/v1/admin/enrollments/{identifier}",
        headers=revision,
        json={"display_name": "updated"},
    )
    assert result.status_code == 200
    assert (
        await browser.post(
            f"/api/v1/admin/enrollments/{identifier}/approve",
            headers=revision,
            json={"group_id": str(access.groups["A"].id)},
        )
    ).status_code == 412
    result = await access.call(
        "POST", f"/admin/enrollments/{identifier}/block", json={"reason": "Hardware rejected"}
    )
    assert result.status_code == 200 and result.json()["state"] == "blocked"
    assert (await machine(access, identifier)).status_code == 401
    async with access.db() as db:
        assert (await db.get(GatewayEnrollment, UUID(identifier))).secret_hash is None


async def test_operational_edits_are_global_and_wait_for_cleanup(access):
    gateway = access.gateways["A"]
    path = f"/admin/gateways/{gateway.id}"
    assert (
        await access.call("PATCH", path, actor="admin_a", json={"mac_address": "02:00:00:00:00:99"})
    ).status_code == 403
    async with access.db() as db:
        session = Session(
            id=uuid4(),
            gateway_id=gateway.id,
            technician_id=access.users["tech_a"].id,
            status="CLOSED",
            activation_mode="wifi",
            expires_at=utc_now() + timedelta(minutes=5),
        )
        db.add(session)
        await db.commit()
    assert (
        await access.call("PATCH", path, json={"mac_address": "02:00:00:00:00:99"})
    ).status_code == 409
    assert (
        await access.call("PATCH", path, actor="admin_a", json={"imei": "012345678901234"})
    ).status_code == 200
    async with access.db() as db:
        (await db.get(Session, session.id)).cleaned_at = utc_now()
        await db.commit()
    assert (
        await access.call("PATCH", path, json={"activation_mode": "sms", "phone_number": None})
    ).status_code == 422
    assert (
        await access.call("PATCH", path, json={"mac_address": "02:00:00:00:00:99"})
    ).status_code == 200
    async with access.db() as db:
        cert = await current_certificate(db, "gateway", gateway.id)
        assert cert is None
        assert not await db.scalar(
            select(GatewayCredential.id).where(
                GatewayCredential.gateway_id == gateway.id, GatewayCredential.active.is_(True)
            )
        )


async def test_blocked_delivery_cannot_confirm_and_hq_recovers_without_restoring_secret(
    access, pki
):
    body, state = await issued(access, pki)
    identifier, job_id = body["gateway_id"], state["job"]["id"]
    assert (
        await machine(access, identifier, "POST", f"/package/{job_id}/download")
    ).status_code == 200
    blocked = await access.call(
        "POST", f"/admin/enrollments/{identifier}/block", json={"reason": "review required"}
    )
    assert blocked.status_code == 200
    assert (
        await machine(
            access,
            identifier,
            "POST",
            f"/package/{job_id}/confirm",
            json={"configuration_version": state["configuration_version"]},
        )
    ).status_code == 401
    result = await access.client.post(
        f"/api/v1/admin/enrollments/{identifier}/manual",
        headers={**access.headers["root"], "Idempotency-Key": str(uuid4())},
        json={"group_id": str(access.groups["A"].id)},
    )
    assert result.status_code == 202, result.text
    detail = (await access.call("GET", f"/admin/enrollments/{identifier}")).json()
    assert detail["state"] == "manual" and not detail["can_resume"]
    assert (await access.call("POST", f"/admin/enrollments/{identifier}/resume")).status_code == 409
    assert (await machine(access, identifier)).status_code == 401


@pytest.mark.parametrize("action", ["move", "delete"])
async def test_gateway_group_change_and_deletion_stop_initial_delivery(access, pki, action):
    body, state = await issued(access, pki)
    identifier = body["gateway_id"]
    if action == "move":
        result = await access.call(
            "PUT",
            f"/admin/gateways/{identifier}/group",
            json={"group_id": str(access.groups["B"].id)},
        )
        assert result.status_code == 200, result.text
    else:
        result = await access.call("DELETE", f"/admin/gateways/{identifier}")
        assert result.status_code == 204, result.text
    assert (await machine(access, identifier)).status_code == 401
    async with access.db() as db:
        row = await db.get(GatewayEnrollment, UUID(identifier))
        assert row.blocked_at and row.secret_hash is None
        assert (await db.get(ProvisioningJob, UUID(state["job"]["id"]))).artifact_encrypted is None
    # Identità e matricola restano riservate anche dopo la cancellazione operativa.
    result = await access.client.post(
        "/api/v1/factory/gateways",
        headers={"X-Factory-Key": FACTORY_KEY, "Idempotency-Key": str(uuid4())},
        json={**body, "gateway_id": str(uuid4()), "secret_hash": digest("other-random-secret")},
    )
    assert result.status_code == 409


async def test_restore_blocks_incomplete_enrollment_but_keeps_completed_gateway(access, pki):
    from api.models.config import Config
    from scripts.backup_database import security_state, sanitize

    completed, state = await issued(access, pki)
    await download_and_confirm(access, completed, state)
    pending, _, _ = await prepare(access, secret_hash=digest("different-pending-key"))
    await approve(access, pending["gateway_id"])
    async with access.db() as db:
        security = await security_state(db)
        await sanitize(db, security, actor_id=str(access.users["root"].id))
        first = await db.get(GatewayEnrollment, UUID(completed["gateway_id"]))
        second = await db.get(GatewayEnrollment, UUID(pending["gateway_id"]))
        assert first.completed_at and first.blocked_at is None and first.secret_hash is None
        assert second.secret_hash is None and second.approved_at is None
        assert second.blocked_reason == "installation_restored"
        assert not await db.scalar(select(Config.key).where(Config.key.like("enrollment-rate:%")))
        assert await db.scalar(
            select(GatewayCredential.id).where(
                GatewayCredential.gateway_id == first.id, GatewayCredential.active.is_(True)
            )
        )
    assert settings.FACTORY_STATIONS == {"line-1": digest(FACTORY_KEY)}


async def test_station_and_device_limits_apply_only_to_authenticated_identity(access, monkeypatch):
    body, _, _ = await prepare(access)
    identifier = body["gateway_id"]
    monkeypatch.setattr(settings, "ENROLLMENT_REQUESTS_PER_MINUTE", 1)
    assert (await machine(access, identifier)).status_code == 200
    wrong = await access.client.get(
        f"/api/v1/gateway-enrollment/{identifier}", headers={"X-Enrollment-Key": "x" * 40}
    )
    assert wrong.status_code == 401
    limited = await machine(access, identifier)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    monkeypatch.setattr(settings, "FACTORY_REQUESTS_PER_MINUTE", 1)
    limited = await access.client.get(
        f"/api/v1/factory/gateways/{identifier}", headers={"X-Factory-Key": FACTORY_KEY}
    )
    assert limited.status_code == 429


async def test_pending_package_is_reused_and_new_emissions_are_throttled(access, pki):
    body, state = await issued(access, pki)
    identifier = body["gateway_id"]
    result = await machine(
        access,
        identifier,
        "POST",
        "/package",
        json={"configuration_version": state["configuration_version"]},
    )
    assert result.status_code == 202 and result.json()["job"]["id"] == state["job"]["id"]
    changed = (
        await machine(access, identifier, "PUT", json={"mac_address": "02:00:00:00:00:99"})
    ).json()
    result = await machine(
        access,
        identifier,
        "POST",
        "/package",
        json={"configuration_version": changed["configuration_version"]},
    )
    assert result.status_code == 429
    async with access.db() as db:
        (await db.get(ProvisioningJob, UUID(state["job"]["id"]))).created_at -= timedelta(
            seconds=61
        )
        await db.commit()
    result = await machine(
        access,
        identifier,
        "POST",
        "/package",
        json={"configuration_version": changed["configuration_version"]},
    )
    assert result.status_code == 202 and result.json()["job"]["id"] != state["job"]["id"]


async def test_operational_gateway_key_cannot_be_used_as_initial_enrollment_key(access):
    body, _, _ = await prepare(access)
    operational = access.keys[str(access.gateways["A"].id)]
    result = await access.client.get(
        f'/api/v1/gateway-enrollment/{body["gateway_id"]}',
        headers={"X-Enrollment-Key": operational},
    )
    assert result.status_code == 401
