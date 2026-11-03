"""Real issuance, access segregation, staged delivery and revocation in PostgreSQL."""

import io
import os
import zipfile
from uuid import UUID, uuid4
from datetime import timedelta
import pytest
from cryptography import x509
from sqlalchemy import select, func
from api.config import settings
from api.models.provisioning import ProvisioningJob, VPNCertificate, GatewayCredential
from api.models.gateway import Gateway
from api.models.user import User
from api.models.session import Session
from api.time_utils import utc_now
from api.services.credentials import digest
from scripts.bootstrap import initialize
from scripts.provisioning_worker import cycle
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_web_portal_postgres import browser as browser_fixture, login, sso_login
from test_sso_postgres import provider as provider_fixture, enable

access = access_fixture
encoded_password = password_fixture
browser = browser_fixture
provider = provider_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


@pytest.fixture
def pki(tmp_path, monkeypatch):
    root = tmp_path / "pki"
    initialize(root, "vpn.example.test")
    monkeypatch.setattr(settings, "OPENVPN_SERVER_HOST", "vpn.example.test")
    monkeypatch.setattr(settings, "PORTAL_PUBLIC_URL", "https://test")
    return root


def registration(access, **changes):
    return {
        "gateway_id": str(uuid4()),
        "group_id": str(access.groups["A"].id),
        "display_name": "Nuovo gateway",
        "activation_mode": "wifi",
        "phone_number": None,
        "mac_address": "02:11:22:33:44:55",
        "lan_interface": "eth0",
        "lan_address": "192.168.1.1",
        "lan_cidr": "192.168.1.0/24",
        **changes,
    }


async def new_gateway(access, browser, pki, **changes):
    body = registration(access, **changes)
    key = str(uuid4())
    result = await browser.post(
        "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": key}
    )
    assert result.status_code == 202, result.text
    await cycle(access.db, pki)
    return body, result.json()["id"], key


@pytest.mark.parametrize("mode", ["wifi", "sms"])
async def test_gateway_full_flow_is_staged_idempotent_and_secret_free(access, browser, pki, mode):
    await login(browser, "admin_a")
    options = (
        {"activation_mode": "sms", "mac_address": None, "phone_number": "+393331234567"}
        if mode == "sms"
        else {}
    )
    body, job, key = await new_gateway(access, browser, pki, **options)
    replay = await browser.post(
        "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 202 and replay.json()["id"] == job
    assert "PRIVATE KEY" not in replay.text
    base = f"/api/v1/provisioning/jobs/{job}"
    assert (await browser.post(base + "/activate")).status_code == 409
    downloaded = await browser.post(base + "/download")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
        config = dict(
            line.split("=", 1) for line in bundle.read("gateway.env").decode().splitlines()
        )
        assert "PRIVATE KEY" in bundle.read("gateway.ovpn").decode()
        assert config["GATEWAY_API_KEY"] != config["GATEWAY_SMS_KEY"]
        assert config["MAC_ADDRESS"] == (body["mac_address"] or "")
    headers = {"X-Gateway-Key": config["GATEWAY_API_KEY"]}
    machine = {k: v for k, v in body.items() if k not in {"display_name", "group_id"}}
    assert (
        await access.client.post("/api/v1/gateways/register", headers=headers, json=machine)
    ).status_code == 401
    assert (await browser.post(base + "/activate")).status_code == 200
    assert (await browser.post(base + "/download")).status_code == 409
    assert (
        await access.client.post(
            "/api/v1/gateways/register",
            headers=headers,
            json={**machine, "lan_cidr": "192.168.0.0/16"},
        )
    ).status_code == 409
    registered = await access.client.post(
        "/api/v1/gateways/register", headers=headers, json=machine
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["group_id"] == body["group_id"]
    state = (await browser.get(f'/api/v1/provisioning/peers/gateway/{body["gateway_id"]}')).json()
    assert state["registered_at"] and state["certificate"]["state"] == "active"
    async with access.db() as db:
        credential = await db.scalar(
            select(GatewayCredential).where(
                GatewayCredential.gateway_id == UUID(body["gateway_id"])
            )
        )
        assert credential.secret_hash == digest(config["GATEWAY_API_KEY"])
        assert config["GATEWAY_SMS_KEY"] not in credential.sms_secret_encrypted
        assert (await db.get(ProvisioningJob, UUID(job))).artifact_encrypted is None
        assert await db.scalar(select(func.count()).select_from(ProvisioningJob)) == 1


async def test_capability_scope_and_delivery_are_independent(access, browser, pki):
    await login(browser, "tech_a")
    body = registration(access)
    assert (
        await browser.post(
            "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
    ).status_code == 403
    await login(browser, "admin_b")
    assert (
        await browser.post(
            "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
    ).status_code == 404
    await login(browser, "admin_a")
    body, job, _ = await new_gateway(access, browser, pki)
    await login(browser, "root")
    assert (await browser.get(f"/api/v1/provisioning/jobs/{job}")).status_code == 200
    assert (await browser.post(f"/api/v1/provisioning/jobs/{job}/download")).status_code == 404
    await login(browser, "admin_b")
    assert (await browser.get(f"/api/v1/provisioning/jobs/{job}")).status_code == 404


async def test_logout_expires_pending_material_and_never_activates_it(access, browser, pki):
    await login(browser)
    body, job, _ = await new_gateway(access, browser, pki)
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    await cycle(access.db, pki)
    await login(browser)
    result = await browser.get(f"/api/v1/provisioning/jobs/{job}")
    assert result.json()["state"] == "expired"
    assert (await browser.post(f"/api/v1/provisioning/jobs/{job}/download")).status_code == 404
    async with access.db() as db:
        row = await db.get(ProvisioningJob, UUID(job))
        assert row.artifact_encrypted is None
        assert (await db.get(VPNCertificate, row.certificate_id)).state == "revoked"


async def test_personal_profile_replacement_requires_own_identity_and_cleanup(access, browser, pki):
    await login(browser, "tech_a")
    peer = access.users["tech_a"].id
    path = f"/api/v1/provisioning/peers/technician/{peer}"
    old = (await browser.get(path)).json()["certificate"]
    prepared = await browser.post(
        path + "/prepare",
        json={"expected_certificate_id": old["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert prepared.status_code == 202, prepared.text
    job = prepared.json()["id"]
    await cycle(access.db, pki)
    base = f"/api/v1/provisioning/jobs/{job}"
    assert "PRIVATE KEY" in (await browser.post(base + "/download")).text
    session = await browser.post(
        "/api/v1/sessions/",
        json={"gateway_id": str(access.gateways["A"].id)},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert session.status_code == 200, session.text
    assert (await browser.post(base + "/activate")).status_code == 409
    await browser.delete("/api/v1/sessions/" + session.json()["session_id"])
    assert (await browser.post(base + "/activate")).status_code == 409
    await access.client.post(
        f'/api/v1/internal/sessions/{session.json()["session_id"]}/cleaned', headers=access.worker
    )
    assert (await browser.post(base + "/activate")).status_code == 200
    current = (await browser.get(path)).json()
    assert current["certificate"]["id"] != old["id"] and current["revocation_pending"]
    await cycle(access.db, pki)
    assert not (await browser.get(path)).json()["revocation_pending"]
    crl = x509.load_pem_x509_crl((pki / "server/crl.pem").read_bytes())
    assert crl.get_revoked_certificate_by_serial_number(int(old["serial"]))
    await login(browser, "root")
    assert (
        await browser.post(
            path + "/prepare",
            json={"expected_certificate_id": current["certificate"]["id"]},
            headers={"Idempotency-Key": str(uuid4())},
        )
    ).status_code == 404


@pytest.mark.parametrize("change", ["group", "permission", "expiry"])
async def test_pending_delivery_rechecks_current_authorization(access, browser, pki, change):
    await login(browser, "admin_a")
    body, job, _ = await new_gateway(access, browser, pki)
    async with access.db() as db:
        if change == "group":
            gateway = await db.get(Gateway, UUID(body["gateway_id"]))
            gateway.group_id = access.groups["B"].id
        elif change == "permission":
            user = await db.get(User, access.users["admin_a"].id)
            user.permissions = ["connect", "gateways.manage"]
            user.token_version += 1
        else:
            (await db.get(ProvisioningJob, UUID(job))).expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    response = await browser.post(f"/api/v1/provisioning/jobs/{job}/download")
    assert response.status_code in (401, 403, 404, 409)
    await cycle(access.db, pki)
    async with access.db() as db:
        row = await db.get(ProvisioningJob, UUID(job))
        assert row.state == "expired" and row.artifact_encrypted is None


async def test_gateway_transfer_revokes_old_keys_and_pending_packages(access, browser, pki):
    await login(browser, "admin_a")
    gateway = access.gateways["A"]
    path = f"/api/v1/provisioning/peers/gateway/{gateway.id}"
    current = (await browser.get(path)).json()["certificate"]
    job = await browser.post(
        path + "/prepare",
        json={"expected_certificate_id": current["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert job.status_code == 202, job.text
    await cycle(access.db, pki)
    moved = await access.call(
        "PUT", f"/admin/gateways/{gateway.id}/group", json={"group_id": str(access.groups["B"].id)}
    )
    assert moved.status_code == 200, moved.text
    assert (
        await access.client.post(
            "/api/v1/gateways/heartbeat",
            headers={"X-Gateway-Key": access.keys[str(gateway.id)]},
            json={},
        )
    ).status_code == 401
    assert (
        await browser.post(f'/api/v1/provisioning/jobs/{job.json()["id"]}/download')
    ).status_code == 404
    result = await access.call("GET", f"/provisioning/peers/gateway/{gateway.id}", "admin_b")
    assert result.json()["certificate"] is None and result.json()["pending_job"] is None


async def test_serial_validation_and_revoke_close_active_session_before_crl_publication(
    access, browser, pki
):
    await login(browser, "tech_a")
    peer = access.users["tech_a"].id
    path = f"/api/v1/provisioning/peers/technician/{peer}"
    cert = (await browser.get(path)).json()["certificate"]
    created = await browser.post(
        "/api/v1/sessions/",
        json={"gateway_id": str(access.gateways["A"].id)},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert created.status_code == 200
    request = {"common_name": f"technician-{peer}", "certificate_serial": cert["serial"]}
    assert (
        await access.client.post(
            "/api/v1/internal/vpn/authorize", headers=access.worker, json=request
        )
    ).status_code == 200
    assert (
        await access.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=access.worker,
            json={**request, "certificate_serial": "123"},
        )
    ).status_code == 403
    assert (
        await access.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=access.worker,
            json={"common_name": request["common_name"]},
        )
    ).status_code == 422
    assert (
        await browser.post(path + "/revoke", json={"expected_certificate_id": cert["id"]})
    ).status_code == 200
    assert (
        await access.client.post(
            "/api/v1/internal/vpn/authorize", headers=access.worker, json=request
        )
    ).status_code == 403
    async with access.db() as db:
        session = await db.get(Session, UUID(created.json()["session_id"]))
        assert session.status == "CLOSED" and session.cleaned_at is None
    state = (await browser.get(path)).json()
    assert state["revocation_pending"] and state["cleanup_pending"]


async def test_issuer_failure_is_atomic_and_retry_does_not_duplicate_certificates(
    access, browser, pki, monkeypatch
):
    import scripts.provisioning_worker as worker

    await login(browser)
    body = registration(access)
    response = await browser.post(
        "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": str(uuid4())}
    )
    job_id = UUID(response.json()["id"])
    original = worker.encrypt

    def unavailable(_):
        raise RuntimeError("private failure detail must never reach the API")

    monkeypatch.setattr(worker, "encrypt", unavailable)
    with pytest.raises(RuntimeError):
        await cycle(access.db, pki)
    async with access.db() as db:
        row = await db.get(ProvisioningJob, job_id)
        assert (
            row.state == "failed" and row.certificate_id is None and row.artifact_encrypted is None
        )
        assert not await db.scalar(
            select(VPNCertificate.id).where(VPNCertificate.peer_id == UUID(body["gateway_id"]))
        )
    monkeypatch.setattr(worker, "encrypt", original)
    result = await browser.post(
        f'/api/v1/provisioning/peers/gateway/{body["gateway_id"]}/prepare',
        json={"expected_certificate_id": None},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert result.status_code == 202
    await cycle(access.db, pki)
    await cycle(access.db, pki)
    async with access.db() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(VPNCertificate)
                .where(VPNCertificate.peer_id == UUID(body["gateway_id"]))
            )
            == 1
        )


async def test_provisioning_mutations_require_csrf(access, browser, pki):
    await login(browser)
    response = await browser.post(
        "/api/v1/provisioning/gateways",
        json=registration(access),
        headers={"Idempotency-Key": str(uuid4()), "X-CSRF-Token": ""},
    )
    assert response.status_code == 403
    _, job, _ = await new_gateway(access, browser, pki)
    assert (
        await browser.post(
            f"/api/v1/provisioning/jobs/{job}/download", headers={"X-CSRF-Token": ""}
        )
    ).status_code == 403
    async with access.db() as db:
        assert (await db.get(ProvisioningJob, UUID(job))).downloaded_at is None


async def test_sso_identity_uses_same_personal_profile_and_revokes_pending_delivery(
    access, browser, provider, pki
):
    await enable(access, provider)
    assert (await sso_login(browser, provider)).status_code == 303
    session = (await browser.get("/api/v1/web/session")).json()
    browser.headers["X-CSRF-Token"] = session["csrf_token"]
    peer = session["user"]["id"]
    path = f"/api/v1/provisioning/peers/technician/{peer}"
    cert = (await browser.get(path)).json()["certificate"]
    response = await browser.post(
        path + "/prepare",
        json={"expected_certificate_id": cert["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    await cycle(access.db, pki)
    job = response.json()["id"]
    assert (await browser.post(f"/api/v1/provisioning/jobs/{job}/download")).status_code == 200
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    await cycle(access.db, pki)
    async with access.db() as db:
        assert (await db.get(ProvisioningJob, UUID(job))).state == "expired"


async def test_provisioning_quota_preserves_idempotency_and_does_not_create_gateway(
    access, browser
):
    await login(browser, "admin_a")
    body, key = registration(access), str(uuid4())
    created = await browser.post(
        "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": key}
    )
    assert created.status_code == 202
    job_id = created.json()["id"]
    assert (await browser.post(f"/api/v1/provisioning/jobs/{job_id}/cancel")).status_code == 200
    async with access.db() as db:
        original = await db.get(ProvisioningJob, UUID(job_id))
        receipt = {
            column.name: getattr(original, column.name) for column in original.__table__.columns
        }
        for _ in range(59):
            db.add(ProvisioningJob(**{**receipt, "id": uuid4(), "request_key": uuid4()}))
        await db.commit()
    replay = await browser.post(
        "/api/v1/provisioning/gateways", json=body, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 202 and replay.json()["id"] == job_id
    blocked_body = registration(access, mac_address="02:11:22:33:44:56")
    blocked = await browser.post(
        "/api/v1/provisioning/gateways",
        json=blocked_body,
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert blocked.status_code == 429 and blocked.headers["retry-after"] == "3600"
    async with access.db() as db:
        assert await db.get(Gateway, UUID(blocked_body["gateway_id"])) is None
        assert await db.scalar(select(func.count()).select_from(ProvisioningJob)) == 60
    await login(browser, "admin_b")
    allowed = await browser.post(
        "/api/v1/provisioning/gateways",
        json={**blocked_body, "group_id": str(access.groups["B"].id)},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert allowed.status_code == 202
