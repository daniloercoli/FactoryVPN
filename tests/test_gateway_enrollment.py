"""Consegna iniziale: crash, conferma persa, rifiuti e percorso manuale."""

from importlib import import_module
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import zipfile

import httpx
import pytest

from factoryvpn_common.certificates import write_private
from factoryvpn_common.pki import issue_profile
from scripts.lab_pki import initialize

enrollment = import_module("gateway-agent.enrollment")


@pytest.fixture
def package(tmp_path, monkeypatch):
    for key in (
        enrollment.PACKAGE_SETTINGS
        | enrollment.LOCAL_SETTINGS
        | set(enrollment.PRESENTATION_SETTINGS)
    ):
        monkeypatch.delenv(key, raising=False)
    # Anche gli update diretti di os.environ dell'installer vengono ripristinati dal fixture.
    for key in enrollment.PACKAGE_SETTINGS | enrollment.LOCAL_SETTINGS:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    gateway_id, job_id = str(uuid4()), str(uuid4())
    runtime = tmp_path / "runtime"
    initialize(runtime, "vpn.example.test")
    _, profile = issue_profile(runtime, "gateway", gateway_id, "vpn.example.test", 1194)
    env = {
        "GATEWAY_ID": gateway_id,
        "SERVER_URL": "https://vpn.example.test",
        "GATEWAY_ACTIVATION_MODE": "wifi",
        "GATEWAY_PHONE_NUMBER": "",
        "MAC_ADDRESS": "02:00:00:00:00:01",
        "GATEWAY_API_KEY": "a" * 43,
        "GATEWAY_SMS_KEY": "b" * 43,
        "MODEM_PATH": "/dev/ttyUSB0",
        "GATEWAY_WIFI_INTERFACE": "",
        "GATEWAY_HARDWARE_MODEL": "",
        "GATEWAY_HARDWARE_REVISION": "",
        "VPN_INTERFACE": "tun0",
        "OPENVPN_CONFIG": "/etc/factoryvpn/gateway.ovpn",
    }
    path = tmp_path / "device/enrollment.json"
    identity = {
        "format": 1,
        "gateway_id": gateway_id,
        "server_url": env["SERVER_URL"],
        "enrollment_key": "e" * 43,
    }
    write_private(path, json.dumps(identity))
    state = {
        "id": gateway_id,
        "state": "ready",
        "delivery_mode": "automatic",
        "configuration_version": 1,
        "activation_mode": "wifi",
        "phone_number": None,
        "mac_address": env["MAC_ADDRESS"],
        "job": {"id": job_id, "configuration_version": 1, "state": "ready", "can_download": True},
    }

    def bundle(values=None, contents=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("gateway.env", enrollment.env_text(values or env))
            archive.writestr("gateway.ovpn", contents or profile)
            archive.writestr("LEGGIMI.txt", "FactoryVPN")
        return stream.getvalue()

    return SimpleNamespace(
        path=path,
        identity=identity,
        state=state,
        env=env,
        profile=profile,
        bundle=bundle,
        runtime=runtime,
    )


def client_for(package, *, lost_confirm=False, confirm_status=None, register_status=None):
    active = [False]
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/register"):
            code = register_status or (200 if active[0] else 401)
            return httpx.Response(code, json={"id": package.identity["gateway_id"]})
        assert request.headers["X-Enrollment-Key"] == package.identity["enrollment_key"]
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=package.bundle())
        if request.url.path.endswith("/confirm"):
            if confirm_status:
                return httpx.Response(confirm_status)
            active[0] = True
            if lost_confirm:
                raise httpx.ReadTimeout("lost confirmation", request=request)
            return httpx.Response(
                200,
                json={
                    "activated": True,
                    "gateway_id": package.identity["gateway_id"],
                    "configuration_version": 1,
                },
            )
        return httpx.Response(200, json=package.state)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), active, calls


async def test_lost_confirmation_recovers_via_operational_registration(package):
    client, _, calls = client_for(package, lost_confirm=True)
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={})
        assert not await installer.step()
        assert installer.journal_path.exists()
        assert (
            installer.env_path.stat().st_mode & 0o777
            == installer.profile_path.stat().st_mode & 0o777
            == 0o600
        )
        with pytest.raises(httpx.ReadTimeout):
            await installer.step()
        resumed = enrollment.EnrollmentInstaller(package.path, client=client)
        assert await resumed.step()
        assert not resumed.journal_path.exists()
        assert json.loads(package.path.read_text())["completed"]
        assert "enrollment_key" not in package.path.read_text()
        assert os.environ["GATEWAY_API_KEY"] == package.env["GATEWAY_API_KEY"]
        previous = len(calls)
        assert await enrollment.EnrollmentInstaller(package.path, client=client).step()
        assert len(calls) == previous  # Nessun polling con la credenziale consumata.
    assert sum(request.url.path.endswith("/confirm") for request in calls) == 1


async def test_crash_between_env_and_profile_is_repaired_before_confirmation(package, monkeypatch):
    client, _, calls = client_for(package)
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={})
        real_write = enrollment.write_private

        def crash(path, *args, **kwargs):
            if path == installer.profile_path:
                raise OSError("simulated power loss")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(enrollment, "write_private", crash)
        with pytest.raises(OSError):
            await installer.step()
        assert installer.journal_path.exists() and installer.env_path.exists()
        assert not installer.profile_path.exists()
        assert not any(request.url.path.endswith("/confirm") for request in calls)
        monkeypatch.setattr(enrollment, "write_private", real_write)
        assert await enrollment.EnrollmentInstaller(package.path, client=client).step()
        assert installer.profile_path.read_bytes() == package.profile


async def test_expired_candidate_409_fetches_current_state_without_starting(package):
    client, _, calls = client_for(package, confirm_status=409)
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={})
        await installer.step()
        package.state["state"] = "awaiting_approval"
        assert not await installer.step()
        assert not installer.journal_path.exists()
        assert calls[-1].method == "GET"
        assert not json.loads(package.path.read_text()).get("completed")


async def test_operational_configuration_409_is_not_treated_as_inactive_key(package):
    client, _, calls = client_for(package, register_status=409)
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={})
        await installer.step()
        with pytest.raises(enrollment.EnrollmentBlocked, match="configuration"):
            await installer.step()
        assert not any(request.url.path.endswith("/confirm") for request in calls)


async def test_both_credentials_rejected_stop_with_durable_candidate(package):
    client, _, _ = client_for(package, confirm_status=401)
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={})
        await installer.step()
        with pytest.raises(enrollment.EnrollmentBlocked, match="credential rejected"):
            await installer.run()
        assert installer.journal_path.exists()
        assert "enrollment_key" in package.path.read_text()


def test_package_preserves_only_explicit_hardware_settings(package, monkeypatch):
    installer = enrollment.EnrollmentInstaller(package.path)
    write_private(
        installer.local_path,
        "MODEM_PATH=/dev/ttyUSB9\nVPN_INTERFACE=vpn0\nGATEWAY_HARDWARE_MODEL=Device\nGATEWAY_API_KEY=wrong\nSERVER_URL=https://wrong.test\nMAC_ADDRESS=02:00:00:00:00:99\n",
    )
    candidate = installer.candidate(package.bundle(), package.state)
    values = dict(enrollment.dotenv_values(stream=io.StringIO(candidate["env"]), interpolate=False))
    assert values["MODEM_PATH"] == "/dev/ttyUSB9"
    assert values["VPN_INTERFACE"] == "vpn0"
    for key in ("GATEWAY_API_KEY", "SERVER_URL", "MAC_ADDRESS"):
        assert values[key] == package.env[key]
    assert values["OPENVPN_CONFIG"] == str(installer.profile_path)


@pytest.mark.parametrize("damage", ["uuid", "url", "config", "version", "zip", "key"])
def test_invalid_package_is_rejected_before_any_operational_file(package, damage):
    installer = enrollment.EnrollmentInstaller(package.path)
    values = package.env.copy()
    if damage == "uuid":
        values["GATEWAY_ID"] = str(uuid4())
    if damage == "url":
        values["SERVER_URL"] = "https://other.test"
    if damage == "config":
        values["MAC_ADDRESS"] = "02:00:00:00:00:99"
    if damage == "version":
        package.state["job"]["configuration_version"] = 2
    if damage == "key":
        values["GATEWAY_API_KEY"] = "short"
    data = package.bundle(values)
    if damage == "zip":
        stream = io.BytesIO(data)
        with zipfile.ZipFile(stream, "a") as archive:
            archive.writestr("../outside", "bad")
        data = stream.getvalue()
    with pytest.raises(ValueError):
        installer.candidate(data, package.state)
    assert not installer.env_path.exists() and not installer.profile_path.exists()


async def test_partial_presentation_is_not_replayed_over_hq_changes(package):
    client, _, calls = client_for(package)
    package.state["state"] = "awaiting_approval"
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={"imei": "012345678901234"})
        assert not await installer.step()
        assert not await installer.step()
    changes = [json.loads(request.content) for request in calls if request.method == "PUT"]
    assert changes == [{"imei": "012345678901234"}]
    assert "serial_number" not in changes[0] and "gateway_id" not in changes[0]


async def test_transient_errors_back_off_without_regenerating_identity(package):
    codes, waits = [503, 503, 401], []

    def handler(request):
        return httpx.Response(codes.pop(0))

    async def sleep(delay):
        waits.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        installer = enrollment.EnrollmentInstaller(
            package.path, client=client, sleep=sleep, jitter=lambda a, b: 1
        )
        with pytest.raises(enrollment.EnrollmentBlocked):
            await installer.run()
    assert waits == [120, 240]
    assert json.loads(package.path.read_text()) == package.identity


async def test_manual_path_without_factory_identity_is_unchanged(tmp_path):
    assert not await enrollment.bootstrap(tmp_path / "missing.json")


def test_reload_reads_new_operational_environment(monkeypatch):
    config = import_module("gateway-agent.config").Config()
    monkeypatch.setenv("GATEWAY_ID", str(uuid4()))
    monkeypatch.setenv("GATEWAY_API_KEY", "new" * 20)
    monkeypatch.setenv("GATEWAY_ACTIVATION_MODE", "wifi")
    config.reload()
    assert config.GATEWAY_ID == os.environ["GATEWAY_ID"]
    assert config.GATEWAY_API_KEY == "new" * 20 and config.ACTIVATION_MODE == "wifi"


async def test_hq_manual_pair_replaces_pending_candidate_without_being_overwritten(package):
    client, active, calls = client_for(package)
    async with client:
        installer = enrollment.EnrollmentInstaller(package.path, client=client)
        installer.presentation = AsyncMock(return_value={})
        await installer.step()
        values = package.env.copy()
        values["GATEWAY_API_KEY"] = "manual" * 10
        values["OPENVPN_CONFIG"] = str(installer.profile_path)
        _, profile = issue_profile(
            package.runtime, "gateway", package.identity["gateway_id"], "vpn.example.test", 1194
        )
        write_private(installer.env_path, enrollment.env_text(values), replace=True)
        write_private(installer.profile_path, profile, replace=True)
        active[0] = True
        assert await enrollment.EnrollmentInstaller(package.path, client=client).step()
        assert installer.profile_path.read_bytes() == profile
        assert os.environ["GATEWAY_API_KEY"] == values["GATEWAY_API_KEY"]
        assert not any(request.url.path.endswith("/confirm") for request in calls)
        assert calls[-1].headers["X-Gateway-Key"] == values["GATEWAY_API_KEY"]


async def test_completed_identity_missing_files_requires_hq_recovery(package):
    identity = {**package.identity, "completed": True}
    identity.pop("enrollment_key")
    write_private(package.path, json.dumps(identity), replace=True)
    installer = enrollment.EnrollmentInstaller(package.path)
    with pytest.raises(enrollment.EnrollmentBlocked, match="missing"):
        await installer.step()
    assert json.loads(package.path.read_text()) == identity


@pytest.mark.parametrize(
    "url", ["http://localhost", "https://user:pass@host", "https://host/path", "https://host:bad"]
)
def test_gateway_identity_requires_https_origin(package, url):
    identity = {**package.identity, "server_url": url}
    write_private(package.path, json.dumps(identity), replace=True)
    with pytest.raises(ValueError):
        enrollment.EnrollmentInstaller(package.path)


async def test_local_https_trust_is_loaded_before_first_contact(package, monkeypatch):
    import ssl
    from unittest.mock import Mock

    installer = enrollment.EnrollmentInstaller(package.path)
    write_private(installer.local_path, f'SSL_CERT_FILE={package.runtime / "https/cert.pem"}\n')
    factory = Mock(return_value=SimpleNamespace())

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    factory.return_value = Client()
    monkeypatch.setattr(enrollment.httpx, "AsyncClient", factory)
    monkeypatch.setattr(enrollment.EnrollmentInstaller, "run", AsyncMock())
    assert await enrollment.bootstrap(package.path)
    context = factory.call_args.kwargs["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    # Il certificato HTTPS di laboratorio è autofirmato, non una root CA VPN.
    assert context.cert_store_stats()["x509"] == 1
    assert factory.call_args.kwargs["follow_redirects"] is False


async def test_stale_candidate_and_inherited_env_do_not_become_presentation(package, monkeypatch):
    installer = enrollment.EnrollmentInstaller(package.path)
    write_private(installer.env_path, enrollment.env_text(package.env))
    for key, value in package.env.items():
        monkeypatch.setenv(key, value)
    write_private(
        installer.local_path, "GATEWAY_IMEI=012345678901234\nMODEM_PATH=/nonexistent/modem\n"
    )
    assert await installer.presentation() == {"imei": "012345678901234"}


def completed_files(package, values=None, profile=None):
    identity = {**package.identity, "completed": True}
    identity.pop("enrollment_key")
    write_private(package.path, json.dumps(identity), replace=True)
    installer = enrollment.EnrollmentInstaller(package.path)
    write_private(installer.env_path, enrollment.env_text(values or package.env))
    write_private(installer.profile_path, profile or package.profile)
    return installer


@pytest.mark.parametrize("damage", ["uuid", "http", "url_path", "cert", "key"])
async def test_completed_marker_rejects_another_identity_or_mixed_profile(package, damage):
    from factoryvpn_common.certificates import new_key, key_bytes
    import re

    values, profile = package.env.copy(), package.profile
    if damage == "uuid":
        values["GATEWAY_ID"] = str(uuid4())
    if damage == "http":
        values["SERVER_URL"] = "http://vpn.example.test"
    if damage == "url_path":
        values["SERVER_URL"] = "https://vpn.example.test/api/v1"
    if damage == "cert":
        _, profile = issue_profile(package.runtime, "gateway", uuid4(), "vpn.example.test", 1194)
    if damage == "key":
        profile = re.sub(
            rb"<key>.*?</key>", b"<key>\n" + key_bytes(new_key()) + b"</key>", profile, flags=re.S
        )
    installer = completed_files(package, values, profile)
    with pytest.raises(enrollment.EnrollmentBlocked, match="identity"):
        await installer.step()
    assert "GATEWAY_API_KEY" not in os.environ


async def test_completed_marker_accepts_hq_zip_same_uuid_and_new_https_origin(package):
    values = {
        **package.env,
        "SERVER_URL": "https://new-vpn.example.test:443",
        "GATEWAY_API_KEY": "replacement" * 5,
        "OPENVPN_CONFIG": "/wrong/unvalidated.ovpn",
    }
    _, profile = issue_profile(
        package.runtime, "gateway", package.identity["gateway_id"], "new-vpn.example.test", 1194
    )
    installer = completed_files(package, values, profile)
    # Nessun client HTTP: il nuovo profilo è controllato esclusivamente in locale.
    assert installer.client is None and await installer.step()
    assert os.environ["SERVER_URL"] == values["SERVER_URL"]
    assert os.environ["GATEWAY_API_KEY"] == values["GATEWAY_API_KEY"]
    assert os.environ["OPENVPN_CONFIG"] == str(installer.profile_path)


async def test_completed_expired_certificate_can_reach_https_renewal(package):
    from datetime import timedelta
    import re
    from cryptography.hazmat.primitives import serialization
    from factoryvpn_common.certificates import certificate, load_issuer, now, PEM

    key = serialization.load_pem_private_key(
        enrollment.renewal.section(package.profile.decode(), "key"), password=None
    )
    issuer_key, issuer = load_issuer(package.runtime)
    expired = certificate(
        "gateway-" + package.identity["gateway_id"],
        key.public_key(),
        issuer_key,
        issuer.subject,
        days=1,
        at=now() - timedelta(days=2),
    )
    profile = re.sub(
        rb"<cert>.*?</cert>",
        b"<cert>\n" + expired.public_bytes(PEM) + b"</cert>",
        package.profile,
        flags=re.S,
    )
    installer = completed_files(package, profile=profile)
    assert expired.not_valid_after_utc < now()
    assert await installer.step()
    assert os.environ["GATEWAY_ID"] == package.identity["gateway_id"]
