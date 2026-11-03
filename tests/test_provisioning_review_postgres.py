"""Security review regressions for credential lifetime, access isolation and races."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from api.models.provisioning import ProvisioningJob, VPNCertificate
from api.time_utils import utc_now
from scripts.provisioning_worker import cycle
from test_provisioning_postgres import (
    access as access_fixture,
    browser as browser_fixture,
    encoded_password as password_fixture,
    pki as pki_fixture,
    login,
)

access = access_fixture
browser = browser_fixture
encoded_password = password_fixture
pki = pki_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


async def personal_job(access, browser, pki):
    await login(browser, "tech_a")
    peer_id = access.users["tech_a"].id
    path = f"/api/v1/provisioning/peers/technician/{peer_id}"
    old = (await browser.get(path)).json()["certificate"]
    response = await browser.post(
        path + "/prepare",
        json={"expected_certificate_id": old["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    await cycle(access.db, pki)
    job = "/api/v1/provisioning/jobs/" + response.json()["id"]
    assert (await browser.post(job + "/download")).status_code == 200
    return path, job, old


async def test_vpn_authorization_cannot_reuse_a_serial_checked_before_rotation(
    access, browser, pki, monkeypatch
):
    from api.routers import internal

    _, job, old = await personal_job(access, browser, pki)
    peer = access.users["tech_a"].id
    checked, release = asyncio.Event(), asyncio.Event()
    lookup = internal.active_certificate

    async def pause_after_lookup(db, kind, peer_id):
        cert = await lookup(db, kind, peer_id)
        checked.set()
        await release.wait()
        return cert

    monkeypatch.setattr(internal, "active_certificate", pause_after_lookup)
    authorization = asyncio.create_task(
        access.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=access.worker,
            json={"common_name": f"technician-{peer}", "certificate_serial": old["serial"]},
        )
    )
    await asyncio.wait_for(checked.wait(), 5)

    async def replace_and_open():
        activated = await browser.post(job + "/activate")
        assert activated.status_code == 200, activated.text
        opened = await browser.post(
            "/api/v1/sessions/",
            json={"gateway_id": str(access.gateways["A"].id)},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert opened.status_code == 200, opened.text

    rotation = asyncio.create_task(replace_and_open())
    try:
        # With correct serialization rotation waits for authorization to release
        # its lock. Before the fix it finishes while the old serial is cached.
        await asyncio.wait({rotation}, timeout=1)
    finally:
        release.set()
        response, _ = await asyncio.wait_for(asyncio.gather(authorization, rotation), 10)
    assert response.status_code == 403, response.text


async def test_distinct_bearer_logins_cannot_retrieve_each_others_delivery(
    access, pki, monkeypatch
):
    from api.services import auth_service

    instant = utc_now()
    monkeypatch.setattr(auth_service, "utc_now", lambda: instant)
    await access.login("tech_a")
    first = access.headers["tech_a"].copy()
    peer = access.users["tech_a"].id
    path = f"/api/v1/provisioning/peers/technician/{peer}"
    old = (await access.client.get(path, headers=first)).json()["certificate"]
    prepared = await access.client.post(
        path + "/prepare",
        headers={**first, "Idempotency-Key": str(uuid4())},
        json={"expected_certificate_id": old["id"]},
    )
    assert prepared.status_code == 202
    await cycle(access.db, pki)
    job = "/api/v1/provisioning/jobs/" + prepared.json()["id"]
    await access.login("tech_a")
    second = access.headers["tech_a"].copy()
    assert (await access.client.post(job + "/download", headers=second)).status_code == 404
    assert (await access.client.post(job + "/download", headers=first)).status_code == 200


async def test_disable_then_reenable_cannot_resurrect_certificate_with_issuer_stopped(
    access, browser, pki
):
    _, job, old = await personal_job(access, browser, pki)
    peer = access.users["tech_a"].id
    for active in (False, True):
        response = await access.call("PUT", f"/admin/users/{peer}", json={"is_active": active})
        assert response.status_code == 200, response.text
    # No issuer cycle between disable/re-enable: revocation is part of the API mutation.
    async with access.db() as db:
        cert = await db.get(VPNCertificate, UUID(old["id"]))
        assert cert.state == "revoked"
        pending = await db.get(ProvisioningJob, UUID(job.rsplit("/", 1)[1]))
        assert pending.artifact_encrypted is None
        assert (await db.get(VPNCertificate, pending.certificate_id)).state == "revoked"
    await access.login("tech_a")
    opened = await access.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert opened.status_code == 409 and "personal VPN profile" in opened.text


async def test_user_deletion_revokes_public_registry_and_clears_pending_private_material(
    access, browser, pki
):
    _, job, old = await personal_job(access, browser, pki)
    peer = access.users["tech_a"].id
    assert (await access.call("DELETE", f"/admin/users/{peer}")).status_code == 204
    async with access.db() as db:
        certs = (
            await db.scalars(select(VPNCertificate).where(VPNCertificate.peer_id == peer))
        ).all()
        assert certs and all(cert.state == "revoked" for cert in certs)
        pending = await db.get(ProvisioningJob, UUID(job.rsplit("/", 1)[1]))
        assert pending.artifact_encrypted is None


async def test_gateway_move_does_not_expose_old_group_delivery_metadata(access, browser, pki):
    from test_provisioning_postgres import new_gateway

    await login(browser, "admin_a")
    body, job, _ = await new_gateway(access, browser, pki)
    assert (await browser.post(f"/api/v1/provisioning/jobs/{job}/download")).status_code == 200
    moved = await access.call(
        "PUT",
        f'/admin/gateways/{body["gateway_id"]}/group',
        json={"group_id": str(access.groups["B"].id)},
    )
    assert moved.status_code == 200
    state = await access.call("GET", f'/provisioning/peers/gateway/{body["gateway_id"]}', "admin_b")
    assert state.status_code == 200
    assert state.json()["recent_job"] is None and state.json()["pending_job"] is None
    assert (await access.call("GET", f"/provisioning/jobs/{job}", "admin_b")).status_code == 404


async def test_bearer_expiry_is_rechecked_after_waiting_for_provisioning_lock(access, monkeypatch):
    from datetime import timedelta
    from api.routers import provisioning

    peer = access.users["tech_a"].id
    path = f"/provisioning/peers/technician/{peer}"
    old = (await access.call("GET", path, "tech_a")).json()["certificate"]
    locked = provisioning.lock_actor

    async def elapsed_wait(db, actor):
        await locked(db, actor)
        monkeypatch.setattr(provisioning, "utc_now", lambda: utc_now() + timedelta(hours=1))

    monkeypatch.setattr(provisioning, "lock_actor", elapsed_wait)
    response = await access.call(
        "POST", path + "/revoke", "tech_a", json={"expected_certificate_id": old["id"]}
    )
    assert response.status_code == 401
    async with access.db() as db:
        assert (await db.get(VPNCertificate, UUID(old["id"]))).state == "active"


@pytest.mark.parametrize("actor,denied", [("admin_b", 404), ("limited", 403)])
async def test_all_gateway_provisioning_commands_enforce_scope_and_capability(
    access, browser, pki, actor, denied
):
    from test_provisioning_postgres import new_gateway

    await login(browser, "admin_a")
    body, job_id, _ = await new_gateway(access, browser, pki)
    peer = f'/provisioning/peers/gateway/{body["gateway_id"]}'
    job = f"/provisioning/jobs/{job_id}"
    for method, path, payload in (
        ("GET", peer, None),
        ("POST", peer + "/prepare", {"expected_certificate_id": None}),
        ("POST", peer + "/revoke", {"expected_certificate_id": None}),
        ("GET", job, None),
        ("POST", job + "/download", None),
        ("POST", job + "/activate", None),
        ("POST", job + "/cancel", None),
    ):
        response = await access.client.request(
            method,
            "/api/v1" + path,
            headers={**access.headers[actor], "Idempotency-Key": str(uuid4())},
            **({"json": payload} if payload is not None else {}),
        )
        assert response.status_code == denied, (actor, path, response.text)
    async with access.db() as db:
        pending = await db.get(ProvisioningJob, UUID(job_id))
        assert pending.state == "ready" and pending.artifact_encrypted is not None
    assert (await browser.post("/api/v1" + job + "/download")).status_code == 200


async def test_candidate_and_cancelled_certificates_never_authorize_active_session(
    access, browser, pki
):
    _, job, old = await personal_job(access, browser, pki)
    async with access.db() as db:
        pending = await db.get(ProvisioningJob, UUID(job.rsplit("/", 1)[1]))
        candidate = await db.get(VPNCertificate, pending.certificate_id)
        serial = candidate.serial
    opened = await browser.post(
        "/api/v1/sessions/",
        json={"gateway_id": str(access.gateways["A"].id)},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert opened.status_code == 200

    async def authorize(value):
        return await access.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=access.worker,
            json={
                "common_name": f'technician-{access.users["tech_a"].id}',
                "certificate_serial": value,
            },
        )

    assert (await authorize(old["serial"])).status_code == 200
    assert (await authorize(serial)).status_code == 403
    assert (await browser.post(job + "/cancel")).status_code == 200
    assert (await authorize(serial)).status_code == 403
    assert (await browser.post(job + "/download")).status_code == 409
    assert (await browser.post(job + "/activate")).status_code == 409
    assert (await authorize(old["serial"])).status_code == 200
