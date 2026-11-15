"""Installer reale contro API ASGI, PostgreSQL ed emettitore PKI reali."""

from importlib import import_module
import io
import json
import os
from types import SimpleNamespace
from uuid import UUID, uuid4
import zipfile

import httpx
import pytest

from api.config import settings
from api.models.enrollment import GatewayEnrollment
from api.models.gateway import Gateway
from api.services.credentials import digest
from factoryvpn_common.certificates import write_private
from scripts.provisioning_worker import cycle
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_provisioning_postgres import pki as pki_fixture

access, encoded_password, pki = access_fixture, password_fixture, pki_fixture
enrollment = import_module("gateway-agent.enrollment")
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


async def device(access, pki, tmp_path, monkeypatch):
    key, secret = "station-" + "s" * 40, "initial-" + "i" * 40
    identifier = uuid4()
    monkeypatch.setattr(settings, "FACTORY_STATIONS", {"line": digest(key)})
    monkeypatch.setattr(settings, "ENROLLMENT_REQUESTS_PER_MINUTE", 120)
    for name in enrollment.PACKAGE_SETTINGS | enrollment.LOCAL_SETTINGS:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    response = await access.client.post(
        "/api/v1/factory/gateways",
        headers={"X-Factory-Key": key, "Idempotency-Key": str(uuid4())},
        json={
            "gateway_id": str(identifier),
            "serial_number": "SN-" + str(identifier),
            "secret_hash": digest(secret),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["gateway_id"] is None
    approved = await access.call(
        "POST",
        f"/admin/enrollments/{identifier}/approve",
        json={"group_id": str(access.groups["A"].id)},
    )
    assert (
        approved.status_code == 200 and approved.json()["state"] == "awaiting_data"
    ), approved.text
    path = tmp_path / "device/enrollment.json"
    write_private(
        path,
        json.dumps(
            {
                "format": 1,
                "gateway_id": str(identifier),
                "server_url": "https://test",
                "enrollment_key": secret,
            }
        ),
    )
    write_private(
        path.with_name("gateway.local.env"),
        "GATEWAY_ACTIVATION_MODE=wifi\nMAC_ADDRESS=02:11:22:33:44:55\nMODEM_PATH=/dev/custom-modem\nGATEWAY_IMEI=012345678901234\n",
    )
    installer = enrollment.EnrollmentInstaller(path, client=access.client)
    assert not await installer.step()  # Dati parziali accettati e job accodato.
    assert not installer.env_path.exists()
    await cycle(access.db, pki)
    assert not await installer.step()  # Download e coppia durabile, ancora inattiva.
    return SimpleNamespace(id=identifier, secret=secret, installer=installer, path=path)


@pytest.mark.parametrize("lost_confirmation", [False, True])
async def test_real_installer_activation_and_lost_confirmation_keep_uuid_and_fixed_ip(
    access, pki, tmp_path, monkeypatch, lost_confirmation
):
    d = await device(access, pki, tmp_path, monkeypatch)
    async with access.db() as db:
        gateway = await db.get(Gateway, d.id)
        assert gateway.vpn_ip is None and gateway.imei == "012345678901234"
    if lost_confirmation:

        class LoseConfirmation:
            async def request(self, method, url, **kwargs):
                result = await access.client.request(method, url, **kwargs)
                if url.endswith("/confirm"):
                    assert result.status_code == 200, result.text
                    raise httpx.ReadTimeout("response lost after server commit")
                return result

            async def post(self, *args, **kwargs):
                return await access.client.post(*args, **kwargs)

        d.installer.client = LoseConfirmation()
        with pytest.raises(httpx.ReadTimeout):
            await d.installer.step()
        d.installer = enrollment.EnrollmentInstaller(d.path, client=access.client)
    assert await d.installer.step()
    initial = await access.client.get(
        f"/api/v1/gateway-enrollment/{d.id}", headers={"X-Enrollment-Key": d.secret}
    )
    assert initial.status_code == 401
    async with access.db() as db:
        row, gateway = await db.get(GatewayEnrollment, d.id), await db.get(Gateway, d.id)
        assert row.completed_at and row.secret_hash is None
        assert gateway.id == d.id and gateway.vpn_ip
        address = gateway.vpn_ip
    assert await enrollment.EnrollmentInstaller(d.path, client=access.client).step()
    assert await d.installer.registered()
    async with access.db() as db:
        assert (await db.get(Gateway, d.id)).vpn_ip == address
    assert enrollment.read_env(d.installer.env_path)["MODEM_PATH"] == "/dev/custom-modem"
    assert not d.installer.journal_path.exists()


async def test_real_manual_takeover_supersedes_downloaded_automatic_candidate(
    access, pki, tmp_path, monkeypatch
):
    d = await device(access, pki, tmp_path, monkeypatch)
    pending = json.loads(d.installer.journal_path.read_text())
    response = await access.client.post(
        f"/api/v1/admin/enrollments/{d.id}/manual",
        headers={**access.headers["root"], "Idempotency-Key": str(uuid4())},
        json={"group_id": str(access.groups["A"].id), "expected_certificate_id": None},
    )
    assert response.status_code == 202, response.text
    job = response.json()["id"]
    assert job != pending["job_id"]
    await cycle(access.db, pki)
    response = await access.call("POST", f"/provisioning/jobs/{job}/download")
    assert response.status_code == 200, response.text
    payload = response.content
    activated = await access.call("POST", f"/provisioning/jobs/{job}/activate")
    assert activated.status_code == 200, activated.text
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in ("gateway.env", "gateway.ovpn"):
            write_private(d.path.with_name(name), archive.read(name), replace=True)
    assert await enrollment.EnrollmentInstaller(d.path, client=access.client).step()
    assert not d.installer.journal_path.exists()
    current = enrollment.read_env(d.installer.env_path)
    assert (
        current["GATEWAY_API_KEY"]
        != enrollment.dotenv_values(stream=io.StringIO(pending["env"]))["GATEWAY_API_KEY"]
    )
    assert current["GATEWAY_ID"] == str(d.id)
    async with access.db() as db:
        assert (await db.get(GatewayEnrollment, UUID(current["GATEWAY_ID"]))).completed_at
