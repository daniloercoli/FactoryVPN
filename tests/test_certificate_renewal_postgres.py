"""Real CSR/issuer/device journal, with lost replies and current authorization."""

import base64
import io
import zipfile
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization, hashes
from sqlalchemy import select

from api.models.config import Config
from api.models.gateway import Gateway
from api.models.provisioning import VPNCertificate, GatewayCredential
from api.time_utils import utc_now
from factoryvpn_common.certificates import PEM, new_key, name
from scripts.provisioning_worker import cycle
from test_provisioning_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
    browser as browser_fixture,
    pki as pki_fixture,
    login,
    new_gateway,
    pytestmark as database_mark,
)

access, encoded_password, browser, pki = (
    access_fixture,
    password_fixture,
    browser_fixture,
    pki_fixture,
)
pytestmark = database_mark

agent = import_module("gateway-agent.certificate_renewal")
BASE = "/api/v1/gateways/certificates"


@pytest.fixture
async def device(access, browser, pki, tmp_path):
    await login(browser, "admin_a")
    body, job, _ = await new_gateway(access, browser, pki)
    base = "/api/v1/provisioning/jobs/" + job
    response = await browser.post(base + "/download")
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        env = dict(
            line.split("=", 1)
            for line in bundle.read("gateway.env").decode().splitlines()
            if line and not line.startswith("#")
        )
        profile = bundle.read("gateway.ovpn")
    assert (await browser.post(base + "/activate")).status_code == 200
    headers = {"X-Gateway-Key": env["GATEWAY_API_KEY"]}
    registration = {k: v for k, v in body.items() if k not in {"display_name", "group_id"}}
    assert (
        await access.client.post("/api/v1/gateways/register", headers=headers, json=registration)
    ).status_code == 200
    path = tmp_path / "gateway.ovpn"
    path.write_bytes(profile)
    config = SimpleNamespace(
        OPENVPN_CONFIG=str(path),
        GATEWAY_ID=body["gateway_id"],
        API_TIMEOUT=2,
        SERVER_URL="https://test",
        GATEWAY_API_KEY=env["GATEWAY_API_KEY"],
    )
    return SimpleNamespace(
        config=config,
        headers=headers,
        path=path,
        env=env,
        id=UUID(body["gateway_id"]),
        profile=profile.decode(),
    )


async def prepare(access, device):
    status = (await access.client.get(BASE, headers=device.headers)).json()
    key = new_key()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(name("untrusted-name"))
        .sign(key, hashes.SHA256())
    )
    request = dict(
        previous_certificate_id=status["certificate_id"], csr_pem=csr.public_bytes(PEM).decode()
    )
    headers = {**device.headers, "Idempotency-Key": str(uuid4())}
    response = await access.client.post(BASE + "/renewals", headers=headers, json=request)
    assert response.status_code == 201, response.text
    return key, response.json(), request, headers


async def ready(access, device, pki):
    key, job, _, _ = await prepare(access, device)
    old = serialization.load_pem_private_key(agent.section(device.profile, "key"), None)
    response = await access.client.post(
        BASE + "/renewals/" + job["id"] + "/submit",
        headers=device.headers,
        json={"signature": agent.signed(old, base64.b64decode(job["challenge"]))},
    )
    assert response.status_code == 200, response.text
    await cycle(access.db, pki)
    job = (await access.client.get(BASE + "/renewals/" + job["id"], headers=device.headers)).json()
    assert job["state"] == "ready", job
    message = f"factoryvpn-certificate-activate-v1\n{job['id']}\n{job['bundle']['certificate_sha256']}\n".encode()
    proof = dict(
        signature=agent.signed(key, message), certificate_sha256=job["bundle"]["certificate_sha256"]
    )
    return key, job, proof


async def test_proof_idempotency_and_untrusted_csr_name(access, device, pki):
    key, job, request, headers = await prepare(access, device)
    replay = await access.client.post(BASE + "/renewals", headers=headers, json=request)
    assert replay.json()["id"] == job["id"]
    bad = await access.client.post(
        BASE + "/renewals/" + job["id"] + "/submit",
        headers=device.headers,
        json={"signature": agent.signed(key, base64.b64decode(job["challenge"]))},
    )
    assert bad.status_code == 403
    old = serialization.load_pem_private_key(agent.section(device.profile, "key"), None)
    proof = dict(signature=agent.signed(old, base64.b64decode(job["challenge"])))
    assert (
        await access.client.post(
            BASE + "/renewals/" + job["id"] + "/submit", headers=device.headers, json=proof
        )
    ).status_code == 200
    await cycle(access.db, pki)
    result = (
        await access.client.get(BASE + "/renewals/" + job["id"], headers=device.headers)
    ).json()
    profile = agent.validated_profile(device.profile, result["bundle"], key, device.id)
    cert = x509.load_pem_x509_certificate(agent.section(profile, "cert"))
    assert (cert.not_valid_after_utc - utc_now()).days == 3649
    assert "PRIVATE KEY" not in str(result)


@pytest.mark.parametrize("lost", ["challenge", "activate", "confirm"])
async def test_agent_restarts_after_lost_response_without_rotating_api_sms(
    access, device, pki, lost
):
    async with access.db() as db:
        db.add(Config(key="certificate-renewal:" + str(device.id), value="requested"))
        credential = await db.scalar(
            select(GatewayCredential).where(
                GatewayCredential.gateway_id == device.id, GatewayCredential.active.is_(True)
            )
        )
        original = (credential.id, credential.secret_hash, credential.sms_secret_encrypted)
        await db.commit()
    dropped = False

    def renewer():
        instance = agent.CertificateRenewer(device.config)

        async def request(method, suffix="", **kwargs):
            nonlocal dropped
            response = await access.client.request(
                method,
                BASE + suffix,
                headers={**device.headers, **kwargs.pop("headers", {})},
                **kwargs,
            )
            response.raise_for_status()
            match = method == "POST" and (
                suffix == "/renewals" if lost == "challenge" else suffix.endswith("/" + lost)
            )
            if match and not dropped:
                dropped = True
                raise httpx.ReadTimeout("Simulated lost successful response")
            return response.json()

        instance.request = request
        return instance

    for _ in range(9):
        # A new process on every iteration exercises durable state only.
        await renewer().tick(idle=True, awake=True)
        await cycle(access.db, pki)
        if dropped and not renewer().journal.exists():
            break
    assert dropped and not renewer().journal.exists()
    assert device.path.read_text() != device.profile
    assert device.path.stat().st_mode & 0o777 == 0o600
    async with access.db() as db:
        credential = await db.get(GatewayCredential, original[0])
        assert (credential.id, credential.secret_hash, credential.sms_secret_encrypted) == original
        rows = (
            await db.scalars(select(VPNCertificate).where(VPNCertificate.peer_id == device.id))
        ).all()
        assert sorted(row.state for row in rows) == ["active", "revoked"]
    assert (await access.client.get(BASE, headers=device.headers)).json()["renewal"] is None


@pytest.mark.parametrize("change", ["group", "revoke", "expired"])
async def test_current_identity_controls_renewal_but_expiry_is_recoverable(
    access, device, pki, change
):
    key, job, proof = await ready(access, device, pki)
    async with access.db() as db:
        if change == "group":
            (await db.get(Gateway, device.id)).group_id = access.groups["B"].id
        elif change == "revoke":
            row = await db.get(VPNCertificate, UUID(job["previous_certificate_id"]))
            row.state, row.revoked_at = "revoked", utc_now()
        else:
            row = await db.get(VPNCertificate, UUID(job["previous_certificate_id"]))
            row.not_after = utc_now() - timedelta(days=1)
        await db.commit()
    response = await access.client.post(
        BASE + "/renewals/" + job["id"] + "/activate", headers=device.headers, json=proof
    )
    assert response.status_code == (
        200 if change == "expired" else 404 if change == "group" else 409
    ), response.text


async def test_idle_sms_does_not_poll(access, device, pki, monkeypatch):
    instance = agent.CertificateRenewer(device.config)
    calls = []

    async def no_request(*args, **kwargs):
        calls.append(args)
        raise AssertionError("No request expected")

    instance.request = no_request
    assert not await instance.tick(idle=True, awake=False)
    assert not calls


async def test_activation_waits_for_session_cleanup(access, browser, device, pki):
    _, job, proof = await ready(access, device, pki)
    await login(browser, "tech_a")
    session = await browser.post(
        "/api/v1/sessions/",
        json={"gateway_id": str(device.id)},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert session.status_code == 200, session.text
    url = BASE + "/renewals/" + job["id"] + "/activate"
    assert (await access.client.post(url, headers=device.headers, json=proof)).status_code == 409
    session_id = session.json()["session_id"]
    await browser.delete("/api/v1/sessions/" + session_id)
    assert (await access.client.post(url, headers=device.headers, json=proof)).status_code == 409
    await access.client.post(
        f"/api/v1/internal/sessions/{session_id}/cleaned", headers=access.worker
    )
    assert (await access.client.post(url, headers=device.headers, json=proof)).status_code == 200
    blocked = await browser.post(
        "/api/v1/sessions/",
        json={"gateway_id": str(device.id)},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert blocked.status_code == 409


async def test_disk_failure_never_activates_candidate(access, device, pki, monkeypatch):
    key, job, _ = await ready(access, device, pki)
    instance = agent.CertificateRenewer(device.config)
    instance.save(dict(id=job["id"], key=agent.key_bytes(key).decode()))
    calls = []

    async def request(method, suffix="", **kwargs):
        calls.append((method, suffix))
        response = await access.client.request(
            method, BASE + suffix, headers=device.headers, **kwargs
        )
        response.raise_for_status()
        return response.json()

    def no_space(*args, **kwargs):
        raise OSError("Simulated disk full")

    instance.request = request
    monkeypatch.setattr(agent, "write_private", no_space)
    with pytest.raises(OSError):
        await instance.tick(idle=True, awake=True)
    assert all(method == "GET" for method, _ in calls)
    assert device.path.read_text() == device.profile


async def test_remote_request_respects_group_and_capability(access, browser, device):
    path = f"/api/v1/provisioning/peers/gateway/{device.id}/renew-certificate"
    await login(browser, "tech_a")
    assert (await browser.post(path)).status_code == 403
    await login(browser, "admin_b")
    assert (await browser.post(path)).status_code == 404
    await login(browser, "admin_a")
    result = await browser.post(path)
    assert result.status_code == 200, result.text
    assert result.json()["remote_renewal_requested"]


async def test_confirmed_job_cannot_restore_a_revoked_certificate(access, device, pki):
    _, job, proof = await ready(access, device, pki)
    url = BASE + "/renewals/" + job["id"]
    assert (
        await access.client.post(url + "/activate", headers=device.headers, json=proof)
    ).status_code == 200
    assert (
        await access.client.post(
            url + "/confirm", headers=device.headers, json={"signature": proof["signature"]}
        )
    ).status_code == 200
    async with access.db() as db:
        row = await db.get(VPNCertificate, UUID(job["certificate_id"]))
        row.state, row.revoked_at = "revoked", utc_now()
        await db.commit()
    assert (await access.client.get(url, headers=device.headers)).status_code == 409


@pytest.mark.parametrize("activated", [False, True])
async def test_restore_reconciles_pending_renewals(access, device, pki, activated):
    from scripts.backup_database import security_state, sanitize

    _, job, proof = await ready(access, device, pki)
    url = BASE + "/renewals/" + job["id"]
    if activated:
        assert (
            await access.client.post(url + "/activate", headers=device.headers, json=proof)
        ).status_code == 200
    async with access.db() as db:
        security = await security_state(db)
        await sanitize(db, security)
    response = await access.client.get(url, headers=device.headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == ("active" if activated else "cancelled")
    if activated:
        assert (
            await access.client.post(
                url + "/confirm", headers=device.headers, json={"signature": proof["signature"]}
            )
        ).status_code == 200


async def test_revoked_api_key_cannot_request_recovery(access, device):
    async with access.db() as db:
        credential = await db.scalar(
            select(GatewayCredential).where(
                GatewayCredential.gateway_id == device.id, GatewayCredential.active.is_(True)
            )
        )
        credential.active, credential.revoked_at = False, utc_now()
        await db.commit()
    assert (await access.client.get(BASE, headers=device.headers)).status_code == 401


@pytest.mark.parametrize("expired", [False, True])
async def test_sms_idle_renews_near_expiry_and_expired_local_certificate(
    access, device, pki, expired
):
    import re
    from factoryvpn_common.certificates import certificate, load_issuer

    old_key = serialization.load_pem_private_key(agent.section(device.profile, "key"), None)
    issuer_key, issuer = load_issuer(pki)
    old = certificate(
        "gateway-" + str(device.id),
        old_key.public_key(),
        issuer_key,
        issuer.subject,
        days=1 if expired else 30,
        at=utc_now() - timedelta(days=2) if expired else None,
    )
    profile = re.sub(
        r"<cert>.*?</cert>",
        lambda _: "<cert>\n" + old.public_bytes(PEM).decode() + "</cert>",
        device.profile,
        flags=re.S,
    )
    device.path.write_text(profile)
    async with access.db() as db:
        row = await db.scalar(
            select(VPNCertificate).where(
                VPNCertificate.peer_id == device.id, VPNCertificate.state == "active"
            )
        )
        row.serial, row.certificate_pem = str(old.serial_number), old.public_bytes(PEM).decode()
        row.not_before, row.not_after = old.not_valid_before_utc, old.not_valid_after_utc
        await db.commit()
    for _ in range(4):
        instance = agent.CertificateRenewer(device.config)

        async def request(method, suffix="", **kwargs):
            response = await access.client.request(
                method,
                BASE + suffix,
                headers={**device.headers, **kwargs.pop("headers", {})},
                **kwargs,
            )
            response.raise_for_status()
            return response.json()

        instance.request = request
        await instance.tick(idle=True, awake=False)
        await cycle(access.db, pki)
    assert not instance.journal.exists()
    cert = x509.load_pem_x509_certificate(agent.section(device.path.read_text(), "cert"))
    assert (cert.not_valid_after_utc - utc_now()).days == 3649
