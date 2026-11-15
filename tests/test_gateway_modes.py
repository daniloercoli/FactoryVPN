"""Activation channels, signed receipt, retries and provisioning identities."""

from datetime import timedelta
from importlib import import_module
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from httpx import ASGITransport, AsyncClient, ConnectError
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.credentials import encrypt, digest
from api.models.provisioning import GatewayCredential
from api.db.session import get_db
from api.main import app
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.user import User
from api.routers.auth import get_current_user
from api.schemas.gateway import GatewayRegisterRequest
from api.schemas.session import SessionCreateRequest
from api.time_utils import utc_now
from factoryvpn_common.network import SessionNetwork
from factoryvpn_common.protocol import activation_message


KEY = "gateway-secret-" + "k" * 40


@pytest.fixture
async def control(monkeypatch):
    gateway = Gateway(
        id=uuid4(),
        activation_mode="sms",
        phone_number="+393331234567",
        vpn_ip="10.100.0.2",
        group_id=uuid4(),
    )
    user = User(
        id=uuid4(), is_active=True, is_admin=False, permissions=["connect"], token_version=1
    )
    session = Session(
        id=uuid4(),
        gateway_id=gateway.id,
        group_id=gateway.group_id,
        technician_id=user.id,
        status="REQUESTED",
        activation_mode="sms",
        expires_at=utc_now() + timedelta(minutes=5),
        gateway_vpn_ip=gateway.vpn_ip,
        technician_vpn_ip="10.100.128.2",
        sms_sent_at=utc_now(),
    )
    db = Mock(spec=AsyncSession)
    db.scalar.return_value = user.id  # Explicit connection membership for this fixture.
    gw_result, session_result = Mock(), Mock()
    gw_result.scalar_one_or_none.return_value = gateway
    session_result.scalar_one_or_none.return_value = session
    db.execute.side_effect = [gw_result, session_result]
    db.get.side_effect = lambda model, identifier: user if model is User else gateway
    credential = GatewayCredential(
        gateway_id=gateway.id,
        secret_hash=digest(KEY),
        sms_secret_encrypted=encrypt(KEY.encode()),
        active=True,
    )

    async def scalar(statement):
        if "FROM gateway_credentials" in str(statement):
            return credential
        return user.id

    db.scalar.side_effect = scalar
    monkeypatch.setattr("api.services.machine_auth.allocation_lock", AsyncMock())

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, db, gateway, session
    finally:
        app.dependency_overrides.clear()


async def test_provider_acceptance_does_not_authorize_sms_before_receipt(control):
    client, db, gateway, session = control
    response = await client.post(
        "/api/v1/gateways/heartbeat", headers={"X-Gateway-Key": KEY}, json={}
    )
    assert response.status_code == 200
    assert response.json()["connect"] is False
    assert session.activation_received_at is None


@pytest.mark.parametrize("mode", ["sms", "wifi"])
async def test_gateway_can_activate_each_channel_without_provider_timestamp(control, mode):
    client, db, gateway, session = control
    gateway.activation_mode = session.activation_mode = mode
    gateway.phone_number = None if mode == "wifi" else gateway.phone_number
    session.sms_sent_at = None
    token = (
        activation_message(session.id, session.expires_at.timestamp(), KEY)
        if mode == "sms"
        else None
    )
    response = await client.post(
        "/api/v1/gateways/heartbeat",
        headers={"X-Gateway-Key": KEY},
        json={"activation_token": token},
    )
    assert response.status_code == 200, response.text
    assert response.json()["connect"] is True
    assert response.json()["network"]["session_id"] == str(session.id)
    assert session.activation_received_at is not None
    assert session.sms_sent_at is None
    assert (
        session.gateway_ready_at is None
    )  # receipt alone does not claim an authorized gateway firewall


@pytest.mark.parametrize("invalid", ["different_session", "expired", "different_key"])
async def test_sms_receipt_rejects_replay_expiry_and_wrong_signature(control, invalid):
    client, db, gateway, session = control
    token = activation_message(
        uuid4() if invalid == "different_session" else session.id,
        time.time() - 1 if invalid == "expired" else session.expires_at.timestamp(),
        "wrong-key" if invalid == "different_key" else KEY,
    )
    response = await client.post(
        "/api/v1/gateways/heartbeat",
        headers={"X-Gateway-Key": KEY},
        json={"activation_token": token},
    )
    assert response.status_code == 403
    assert session.activation_received_at is None
    db.commit.assert_not_awaited()


async def test_wifi_session_never_calls_sms_provider(control, monkeypatch):
    client, db, gateway, session = control
    gateway.activation_mode = session.activation_mode = "wifi"
    gateway.phone_number = None
    session.sms_sent_at = None
    create = AsyncMock(return_value=session)
    provider = AsyncMock()
    monkeypatch.setattr("api.routers.sessions.create_session", create)
    monkeypatch.setattr("api.routers.sessions.send_sms", provider)
    response = await client.post("/api/v1/sessions/", json={"gateway_id": str(gateway.id)})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "REQUESTED"
    create.assert_awaited_once_with(
        db=db, technician_id=session.technician_id, phone_number=None, gateway_id=gateway.id
    )
    provider.assert_not_awaited()
    assert session.sms_sent_at is None


@pytest.mark.parametrize("received", [False, True])
async def test_provider_failure_preserves_verified_receipt_racing_with_dispatch(
    control, monkeypatch, received
):
    client, db, gateway, session = control
    session.sms_sent_at = None
    monkeypatch.setattr("api.routers.sessions.create_session", AsyncMock(return_value=session))
    monkeypatch.setattr("api.routers.sessions.send_sms", AsyncMock(return_value=False))

    async def refresh(obj, **kwargs):
        if obj is not session:
            return  # The actor is refreshed separately before authorized mutations.
        assert kwargs["with_for_update"] is True
        if received:
            obj.activation_received_at = utc_now()

    db.refresh.side_effect = refresh
    response = await client.post("/api/v1/sessions/", json={"gateway_id": str(gateway.id)})
    assert response.status_code == 200
    assert session.status == ("REQUESTED" if received else "FAILED")
    assert session.sms_sent_at is None


async def test_disabled_technician_cannot_activate_wifi_session(control):
    client, db, gateway, session = control
    gateway.activation_mode = session.activation_mode = "wifi"
    user = User(id=session.technician_id, is_active=False)
    db.get.side_effect = lambda model, identifier: user if model is User else gateway
    response = await client.post(
        "/api/v1/gateways/heartbeat", headers={"X-Gateway-Key": KEY}, json={}
    )
    assert response.json()["connect"] is False
    assert session.status == "FAILED"
    assert session.activation_received_at is None


def agent_fixture(load_script, mode, sms=None):
    module = load_script("gateway-agent", "main")
    modem = Mock() if mode == "sms" else None
    if modem:
        modem.list_sms.return_value = [sms] if sms else []
    vpn = Mock()
    vpn.start, vpn.stop = AsyncMock(), AsyncMock()
    vpn.is_running.return_value = False
    vpn.get_tunnel_ip.return_value = "10.100.0.2"
    network, heartbeat = Mock(), Mock()
    heartbeat.send = AsyncMock()
    config = SimpleNamespace(
        ACTIVATION_MODE=mode,
        GATEWAY_API_KEY=KEY,
        GATEWAY_SMS_KEY=KEY,
        VPN_INTERFACE="tun0",
        POLL_INTERVAL=5,
        WIFI_CHECK_INTERVAL=30,
    )
    return module.GatewayAgent(modem, vpn, network, heartbeat, config)


async def test_sms_idle_and_invalid_messages_do_not_access_https(load_script):
    agent = agent_fixture(load_script, "sms", SimpleNamespace(id=1, body="not signed"))
    await agent.step()
    agent.modem.delete_sms.assert_called_once_with(1)
    agent.heartbeat.send.assert_not_awaited()
    agent.vpn.start.assert_not_awaited()


async def test_wifi_agent_connects_without_modem_and_changes_poll_interval(load_script):
    agent = agent_fixture(load_script, "wifi")
    spec = SessionNetwork(str(uuid4()), "10.100.128.2", "10.100.0.2")
    agent.heartbeat.send.return_value = {
        "connect": True,
        "network": spec.to_dict(),
        "lease_seconds": 20,
    }
    assert agent.interval == 30
    await agent.step()
    assert agent.spec == spec and agent.interval == 5
    agent.vpn.start.assert_awaited_once()
    agent.heartbeat.send.assert_awaited_with(spec.gateway_ip, spec.session_id, None)
    agent.heartbeat.send.return_value = {"connect": False}
    await agent.step()
    assert agent.spec is None and agent.interval == 30
    agent.vpn.stop.assert_awaited_once()


async def test_wifi_never_initializes_serial_modem(load_script, monkeypatch):
    module = load_script("gateway-agent", "main")
    modem = Mock(side_effect=AssertionError("No serial modem on Wi-Fi"))
    monkeypatch.setattr(module, "SMSHandler", modem)
    assert await module.create_modem(SimpleNamespace(ACTIVATION_MODE="wifi")) is None
    modem.assert_not_called()


async def test_sms_is_preserved_across_https_failure_and_deleted_only_after_ready(load_script):
    spec = SessionNetwork(str(uuid4()), "10.100.128.2", "10.100.0.2")
    sms = SimpleNamespace(id=3, body=activation_message(spec.session_id, time.time() + 300, KEY))
    agent = agent_fixture(load_script, "sms", sms)
    agent.heartbeat.send.side_effect = ConnectError("offline")
    with pytest.raises(ConnectError):
        await agent.step()
    agent.modem.delete_sms.assert_not_called()
    assert agent.activation_token == sms.body
    agent.heartbeat.send.side_effect = None
    agent.heartbeat.send.return_value = {
        "connect": True,
        "network": spec.to_dict(),
        "lease_seconds": 20,
    }
    await agent.step()
    agent.modem.delete_sms.assert_called_once_with(3)
    assert agent.spec == spec
    assert all(call.args[2] == sms.body for call in agent.heartbeat.send.call_args_list)
    agent.modem.list_sms.return_value = []
    agent.heartbeat.send.return_value = {"connect": False}
    await agent.step()
    assert agent.activation_token is None and not agent.awake
    agent.heartbeat.send.reset_mock()
    await agent.step()
    agent.heartbeat.send.assert_not_awaited()


async def test_sms_gateway_refuses_server_assignment_for_another_session(load_script):
    sms = SimpleNamespace(id=1, body=activation_message(uuid4(), time.time() + 300, KEY))
    agent = agent_fixture(load_script, "sms", sms)
    spec = SessionNetwork(str(uuid4()), "10.100.128.2", "10.100.0.2")
    agent.heartbeat.send.return_value = {
        "connect": True,
        "network": spec.to_dict(),
        "lease_seconds": 20,
    }
    with pytest.raises(RuntimeError, match="differs"):
        await agent.step()
    agent.vpn.start.assert_not_awaited()
    agent.vpn.stop.assert_awaited_once()
    assert not agent.awake


@pytest.mark.parametrize(
    "selector", [{}, {"gateway_id": str(uuid4()), "phone_number": "+393331234567"}]
)
def test_session_selector_is_unambiguous(selector):
    with pytest.raises(ValueError, match="exactly one"):
        SessionCreateRequest(**selector)


def test_wifi_configuration_needs_no_phone_and_sms_is_default():
    payload = dict(
        gateway_id=uuid4(),
        mac_address="AA:BB:CC:DD:EE:FF",
    )
    assert GatewayRegisterRequest(**payload, activation_mode="wifi").phone_number is None
    assert GatewayRegisterRequest(**payload, phone_number="+393331234567").activation_mode == "sms"
    with pytest.raises(ValueError, match="phone number"):
        GatewayRegisterRequest(**payload)
    config = import_module("gateway-agent.config").Config
    assert config.ACTIVATION_MODE == "sms"


async def test_sms_permanent_rejection_stops_retries_but_transient_failure_keeps_command(
    load_script,
):
    import httpx

    sms = SimpleNamespace(id=2, body=activation_message(uuid4(), time.time() + 300, KEY))
    agent = agent_fixture(load_script, "sms", sms)
    await agent.collect_wakeups()
    await agent.handle_failure(ConnectError("offline"))
    assert agent.awake
    agent.modem.delete_sms.assert_not_called()
    request = httpx.Request("POST", "https://example.test/heartbeat")
    await agent.handle_failure(
        httpx.HTTPStatusError(
            "invalid command", request=request, response=httpx.Response(403, request=request)
        )
    )
    assert not agent.awake
    agent.modem.delete_sms.assert_called_once_with(2)


@pytest.mark.parametrize("mode", ["sms", "wifi"])
async def test_wake_endpoint_resends_only_live_sms_and_preserves_expiry(control, monkeypatch, mode):
    from factoryvpn_common.protocol import verify_activation

    client, db, gateway, session = control
    session.activation_mode = gateway.activation_mode = mode
    result = Mock()
    result.scalar_one_or_none.return_value = session
    db.execute.side_effect = None
    db.execute.return_value = result
    provider = AsyncMock(return_value=True)
    monkeypatch.setattr("api.routers.sessions.send_sms", provider)
    expires = session.expires_at
    response = await client.post(f"/api/v1/sessions/{session.id}/wake")
    assert response.status_code == (200 if mode == "sms" else 409), response.text
    assert session.expires_at == expires
    if mode == "sms":
        assert verify_activation(provider.call_args.kwargs["message"], KEY) == str(session.id)
        assert provider.call_args.kwargs["phone_number"] == gateway.phone_number
    else:
        provider.assert_not_awaited()


async def test_registration_requires_preprovisioned_gateway(control):
    client, db, gateway, session = control
    result = Mock()
    result.scalar_one_or_none.return_value = None
    result.first.return_value = None
    result.scalars.return_value.all.return_value = []
    db.execute.side_effect = None
    db.execute.return_value = result
    response = await client.post(
        "/api/v1/gateways/register",
        headers={"X-Gateway-Key": KEY},
        json={
            "gateway_id": str(gateway.id),
            "activation_mode": "wifi",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
    )
    assert response.status_code == 404
    db.add.assert_not_called()


async def test_failed_resend_keeps_live_session_available_for_another_attempt(control, monkeypatch):
    client, db, gateway, session = control
    result = Mock()
    result.scalar_one_or_none.return_value = session
    db.execute.side_effect = None
    db.execute.return_value = result
    monkeypatch.setattr("api.routers.sessions.send_sms", AsyncMock(return_value=False))
    expires, sent = session.expires_at, session.sms_sent_at
    response = await client.post(f"/api/v1/sessions/{session.id}/wake")
    assert response.status_code == 200
    assert session.status == "REQUESTED"
    assert session.expires_at == expires and session.sms_sent_at == sent
    assert "resend failed" in response.json()["message"]
