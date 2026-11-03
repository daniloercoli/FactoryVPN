"""Exercise real ASGI routing and authorization with an isolated database double."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.models.provisioning import GatewayCredential
from api.services.credentials import encrypt, digest
from api.db.session import get_db
from api.main import app
from api.models.gateway import Gateway
from api.models.session import Session
from api.routers.auth import get_current_user

WORKER_KEY = "worker-test-" + "w" * 32
GATEWAY_KEY = "gateway-test-" + "g" * 32
PHONE = "+393331234567"
GATEWAY_ID = uuid4()
REGISTRATION = {
    "gateway_id": str(GATEWAY_ID),
    "phone_number": PHONE,
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "lan_interface": "eth0",
    "lan_address": "192.168.1.1",
    "lan_cidr": "192.168.1.0/24",
}


@pytest.fixture
def db():
    database = Mock(spec=AsyncSession)
    # AsyncSession.execute is async, but the buffered Result methods are sync.
    database.execute.return_value = Mock()
    return database


@pytest.fixture
async def client(db, monkeypatch):
    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(settings, "WORKER_API_KEY", WORKER_KEY)
    credential = GatewayCredential(
        gateway_id=GATEWAY_ID,
        secret_hash=digest(GATEWAY_KEY),
        sms_secret_encrypted=encrypt(GATEWAY_KEY.encode()),
        active=True,
    )

    async def scalar(statement):
        if "FROM gateway_credentials" in str(statement):
            values = statement.compile().params.values()
            return (
                credential
                if "secret_hash" not in str(statement.whereclause) or digest(GATEWAY_KEY) in values
                else None
            )
        return db.scalar.return_value

    db.scalar.side_effect = scalar
    monkeypatch.setattr("api.services.machine_auth.allocation_lock", AsyncMock())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


def authenticate(user_id, admin=False):
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, is_admin=admin, is_active=True, token_version=1, permissions=["connect"]
    )


@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize("role", ["owner", "other", "admin"])
async def test_session_access_is_scoped(client, db, method, role):
    owner_id = uuid4()
    # The real database supplies this non-null default when inserting a session.
    session = Session(id=uuid4(), technician_id=owner_id, status="ACTIVE", activation_mode="sms")
    db.execute.return_value.scalar_one_or_none.return_value = session
    authenticate(owner_id if role == "owner" else uuid4(), admin=role == "admin")

    response = await getattr(client, method)(f"/api/v1/sessions/{session.id}")

    assert response.status_code == (404 if role == "other" else 200)
    if role == "other":
        db.commit.assert_not_awaited()
        assert session.status == "ACTIVE"
    elif method == "get":
        # Reading status now commits the transaction that holds its row lock;
        # an authorized read without maintenance must preserve the live state.
        db.commit.assert_awaited_once()
        assert session.status == "ACTIVE"
    else:
        assert session.status == "CLOSED"
        db.commit.assert_awaited_once()


@pytest.mark.parametrize("method", ["get", "delete"])
async def test_invalid_session_id_is_rejected_before_query(client, db, method):
    authenticate(uuid4())
    response = await getattr(client, method)("/api/v1/sessions/not-a-uuid")
    assert response.status_code == 422
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("headers", [{}, {"X-Gateway-Key": "wrong"}, {"X-Worker-Key": WORKER_KEY}])
async def test_gateway_rejects_missing_or_wrong_credentials(client, db, headers):
    response = await client.post("/api/v1/gateways/register", json=REGISTRATION, headers=headers)
    assert response.status_code == 401
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_gateway_key_cannot_impersonate_another_gateway(client, db):
    response = await client.post(
        "/api/v1/gateways/register",
        json={**REGISTRATION, "gateway_id": str(uuid4())},
        headers={"X-Gateway-Key": GATEWAY_KEY},
    )
    assert response.status_code == 403
    db.execute.assert_not_awaited()


async def test_gateway_cannot_change_provisioned_configuration(client, db):
    gateway = Gateway(
        id=GATEWAY_ID,
        activation_mode="sms",
        phone_number=PHONE,
        mac_address="00:00:00:00:00:01",
        vpn_subnet="192.168.1.0/24",
        extra_metadata={"lan_interface": "eth0", "lan_address": "192.168.1.1"},
        vpn_ip="10.100.0.2",
    )
    db.execute.return_value.first.return_value = None
    db.execute.return_value.scalar_one_or_none.return_value = gateway
    response = await client.post(
        "/api/v1/gateways/register", json=REGISTRATION, headers={"X-Gateway-Key": GATEWAY_KEY}
    )
    assert response.status_code == 409
    assert gateway.mac_address == "00:00:00:00:00:01"
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("service", ["gateway", "worker"])
async def test_unconfigured_machine_auth_fails_closed(client, db, monkeypatch, service):
    if service == "gateway":
        response = await client.post("/api/v1/gateways/register", json=REGISTRATION)
    else:
        monkeypatch.setattr(settings, "WORKER_API_KEY", "")
        response = await client.post("/api/v1/internal/sessions/expire-stale")
    assert response.status_code == (401 if service == "gateway" else 503)
    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "path,method",
    [
        ("expire-stale", "post"),
        ("namespace-states", "get"),
    ],
)
@pytest.mark.parametrize("headers", [{}, {"X-Worker-Key": "wrong"}, {"X-Gateway-Key": GATEWAY_KEY}])
async def test_worker_endpoints_require_worker_key(client, db, path, method, headers):
    response = await getattr(client, method)(f"/api/v1/internal/sessions/{path}", headers=headers)
    assert response.status_code == 401
    db.execute.assert_not_awaited()


async def test_worker_can_expire_sessions(client, db):
    session = Session(
        id=uuid4(), status="ACTIVE", expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc)
    )
    db.execute.return_value.scalars.return_value.all.return_value = [session]
    response = await client.post(
        "/api/v1/internal/sessions/expire-stale", headers={"X-Worker-Key": WORKER_KEY}
    )
    assert response.status_code == 200
    assert response.json() == {"expired": 1}
    assert session.status == "EXPIRED"
    db.commit.assert_awaited_once()


async def test_technician_cannot_expire_everyones_sessions(client, db):
    authenticate(uuid4())
    response = await client.post("/api/v1/sessions/expire-stale")
    assert response.status_code == 403
    db.execute.assert_not_awaited()


async def test_namespace_snapshot_protects_live_and_unknown_states_and_prefix_collisions(
    client, db
):
    db.execute.return_value.all.return_value = [
        (UUID("aaaaaaaa-0000-0000-0000-000000000001"), "ACTIVE"),
        (UUID("aaaaaaaa-0000-0000-0000-000000000002"), "CLOSED"),
        (UUID("bbbbbbbb-0000-0000-0000-000000000001"), "EXPIRED"),
        (UUID("cccccccc-0000-0000-0000-000000000001"), "REQUESTED"),
        (UUID("dddddddd-0000-0000-0000-000000000001"), "UNRECOGNIZED"),
    ]
    response = await client.get(
        "/api/v1/internal/sessions/namespace-states", headers={"X-Worker-Key": WORKER_KEY}
    )
    assert response.status_code == 200
    assert response.json() == {
        "protected": ["factoryvpn_aaaaaaaa", "factoryvpn_cccccccc", "factoryvpn_dddddddd"],
        "terminal": ["factoryvpn_bbbbbbbb"],
    }


async def test_admin_device_delete_filters_by_gateway(client, db):
    authenticate(uuid4(), admin=True)
    gateway_id, device_id = uuid4(), uuid4()
    db.scalar.side_effect = [Gateway(id=gateway_id), None]
    response = await client.delete(f"/api/v1/admin/gateways/{gateway_id}/devices/{device_id}")
    assert response.status_code == 404
    query = db.scalar.call_args.args[0]
    assert str(gateway_id) in [str(v) for v in query.compile().params.values()]
    assert str(device_id) in [str(v) for v in query.compile().params.values()]
    db.delete.assert_not_awaited()


async def test_disabled_user_cannot_login(client, db):
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(is_active=False)
    response = await client.post(
        "/api/v1/auth/login", json={"username": "disabled", "password": "test-password"}
    )
    assert response.status_code == 401
    db.commit.assert_not_awaited()


async def test_failed_sms_does_not_set_sent_timestamp(client, db, monkeypatch):
    from api.routers import sessions
    from api.models.user import User

    user_id, group_id = uuid4(), uuid4()
    authenticate(user_id)
    session = Session(
        id=uuid4(),
        technician_id=user_id,
        group_id=group_id,
        gateway_id=GATEWAY_ID,
        activation_mode="sms",
        status="REQUESTED",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    user = User(id=user_id, is_active=True, auth_mode="local", permissions=["connect"])
    gateway = Gateway(id=GATEWAY_ID, group_id=group_id, phone_number=PHONE, activation_mode="sms")
    db.get.side_effect = lambda model, identifier: user if model is User else gateway
    db.scalar.return_value = user_id
    monkeypatch.setattr(sessions, "create_session", AsyncMock(return_value=session))
    monkeypatch.setattr(settings, "SMS_PROVIDER_API_KEY", "")
    monkeypatch.setattr(settings, "SMS_PROVIDER_URL", "")
    response = await client.post("/api/v1/sessions/", json={"phone_number": PHONE})
    assert response.status_code == 200
    assert "failed" in response.json()["message"]
    assert session.sms_sent_at is None
    assert session.status == "FAILED"
    assert session.ended_at is not None
    assert db.commit.await_count == 2  # Persist the attempt before recording its outcome.


async def test_session_no_longer_reports_active_after_worker_lease_expires(client, db):
    from datetime import timedelta

    owner_id = uuid4()
    session = Session(
        id=uuid4(),
        technician_id=owner_id,
        status="ACTIVE",
        activation_mode="sms",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        worker_seen_at=datetime.now(timezone.utc) - timedelta(seconds=31),
    )
    db.execute.return_value.scalar_one_or_none.return_value = session
    authenticate(owner_id)
    response = await client.get(f"/api/v1/sessions/{session.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "REQUESTED"
    assert session.network_ready_at is None
