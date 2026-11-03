"""Authorization through real HTTP, JWTs and PostgreSQL, without dependency bypasses."""

import asyncio
from datetime import timedelta
import os
from types import SimpleNamespace
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.config import settings
from provisioning_helpers import seed_gateway, seed_certificate
from api.db.session import Base, get_db
from api.main import app
from api.models.audit_log import AuditLog
from api.models.device import Device
from api.models.gateway import Gateway
from api.models.group import Group, UserGroup
from api.models.session import Session
from api.models.user import User
from api.permissions import ALL_PERMISSIONS
from api.services.auth_service import create_access_token, hash_password, issue_access_token
from api.time_utils import utc_now
from factoryvpn_common.protocol import peer_name

pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)
PASSWORD = "test-password-for-users"
WORKER = "worker-access-test-" + "w" * 32


@pytest.fixture(scope="session")
def encoded_password():
    return hash_password(PASSWORD)


@pytest.fixture
async def access(monkeypatch, encoded_password):
    schema = "factoryvpn_access_" + uuid4().hex
    engine = create_async_engine(
        os.environ["FACTORYVPN_TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    groups = {name: Group(id=uuid4(), name=name) for name in ("A", "B")}
    users, gateways, devices, headers = {}, {}, {}, {}
    memberships = {
        "root": [],
        "admin_a": ["A"],
        "admin_b": ["B"],
        "tech_a": ["A"],
        "tech_b": ["B"],
        "shared": ["A", "B"],
        "outsider": [],
        "limited": ["A"],
        "observer": ["A"],
    }

    async def database():
        async with factory() as db:
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add_all(groups.values())
            for name in memberships:
                permissions = ALL_PERMISSIONS if name.startswith("admin") else ["connect"]
                if name == "limited":
                    permissions = ["connect", "users.manage"]
                if name == "observer":
                    permissions = ["audit.read"]
                users[name] = User(
                    id=uuid4(),
                    username=name,
                    email=name + "@example.test",
                    password_hash=encoded_password,
                    is_admin=name == "root",
                    permissions=list(permissions),
                )
            db.add_all(users.values())
            await db.flush()
            for name, names in memberships.items():
                db.add_all(
                    [UserGroup(user_id=users[name].id, group_id=groups[g].id) for g in names]
                )
            for index, name in enumerate(("A", "B", "unassigned"), start=2):
                gateways[name] = Gateway(
                    id=uuid4(),
                    mac_address=f"AA:BB:CC:DD:EE:{index:02d}",
                    phone_number=f"+39333123456{index}",
                    activation_mode="wifi",
                    group_id=groups[name].id if name in groups else None,
                    vpn_ip=f"10.100.0.{index}",
                    vpn_subnet="192.168.1.0/24",
                    status="ACTIVE",
                )
            db.add_all(gateways.values())
            await db.flush()
            for name in ("A", "B"):
                devices[name] = Device(id=uuid4(), gateway_id=gateways[name].id, name="PLC " + name)
                db.add(devices[name])
                db.add(
                    AuditLog(
                        event_type="seed",
                        group_id=groups[name].id,
                        gateway_id=gateways[name].id,
                        ip_address="192.0.2.10",
                        details={"site": name},
                    )
                )
            db.add(AuditLog(event_type="legacy", details={"private": True}))
            await db.commit()
        headers.update(
            {
                name: {"Authorization": "Bearer " + issue_access_token(user, email_verified=True)}
                for name, user in users.items()
            }
        )
        keys = {str(gw.id): "gateway-access-test-" + str(gw.id) for gw in gateways.values()}
        async with factory() as db:
            for user in users.values():
                await seed_certificate(db, "technician", user.id)
            for gateway in gateways.values():
                persisted = await db.get(Gateway, gateway.id)
                await seed_gateway(db, persisted, keys[str(gateway.id)])
            await db.commit()
        monkeypatch.setattr(settings, "WORKER_API_KEY", WORKER)
        app.dependency_overrides[get_db] = database
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

            async def call(method, path, actor="root", **kwargs):
                return await client.request(
                    method, "/api/v1" + path, headers=headers[actor], **kwargs
                )

            async def login(name, password=PASSWORD):
                result = await client.post(
                    "/api/v1/auth/login", json={"username": name, "password": password}
                )
                from email_helpers import complete

                result = await complete(client, result)
                assert result.status_code == 200, result.text
                headers[name] = {"Authorization": "Bearer " + result.json()["access_token"]}
                return result

            yield SimpleNamespace(
                client=client,
                call=call,
                login=login,
                db=factory,
                users=users,
                groups=groups,
                gateways=gateways,
                devices=devices,
                headers=headers,
                keys=keys,
                worker={"X-Worker-Key": WORKER},
            )
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.parametrize(
    "actor,visible",
    [
        ("tech_a", ["A"]),
        ("tech_b", ["B"]),
        ("shared", ["A", "B"]),
        ("outsider", []),
        ("root", ["A", "B", "unassigned"]),
    ],
)
async def test_inventory_is_scoped(access, actor, visible):
    a = access
    result = await a.call("GET", "/gateways/", actor)
    assert result.status_code == 200, result.text
    assert {row["id"] for row in result.json()} == {str(a.gateways[n].id) for n in visible}
    for name in ("A", "B"):
        result = await a.call("GET", f"/devices/gateway/{a.gateways[name].id}", actor)
        assert result.status_code == (200 if name in visible else 404), result.text
    result = await a.call("GET", "/groups/", actor)
    assert {row["name"] for row in result.json()} == set(visible) - {"unassigned"}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/admin/gateways/{gateway}", None),
        ("DELETE", "/admin/gateways/{gateway}", None),
        ("GET", "/admin/gateways/{gateway}/devices", None),
        ("DELETE", "/admin/gateways/{gateway}/devices/{device}", None),
        ("PUT", "/admin/gateways/{gateway}/group", {"group_id": None}),
        ("GET", "/admin/groups/{group}", None),
        ("PUT", "/admin/groups/{group}", {"name": "stolen"}),
        ("DELETE", "/admin/groups/{group}", None),
    ],
)
async def test_delegates_cannot_address_foreign_resources(access, method, path, body):
    a = access
    result = await a.call(
        method,
        path.format(gateway=a.gateways["B"].id, device=a.devices["B"].id, group=a.groups["B"].id),
        "admin_a",
        **({"json": body} if body is not None else {}),
    )
    assert result.status_code == 404, result.text


async def test_both_device_write_paths_require_capability(access):
    a = access
    body = {"name": "HMI", "ip_address": "192.168.1.20", "mac_address": "AA:BB:CC:DD:EE:99"}
    for path, payload in [
        ("/devices/", {**body, "gateway_id": str(a.gateways["A"].id)}),
        (f"/admin/gateways/{a.gateways['A'].id}/devices", body),
    ]:
        assert (await a.call("POST", path, "tech_a", json=payload)).status_code == 403
        result = await a.call("POST", path, "admin_a", json=payload)
        assert result.status_code in (200, 201), result.text


@pytest.mark.parametrize("selector", ["gateway_id", "phone_number"])
async def test_session_selection_does_not_bypass_group_scope(access, selector):
    a = access
    for site, expected in [("B", 404), ("unassigned", 404), ("A", 200)]:
        gateway = a.gateways[site]
        value = str(gateway.id) if selector == "gateway_id" else gateway.phone_number
        result = await a.call("POST", "/sessions/", "tech_a", json={selector: value})
        assert result.status_code == expected, result.text
    result = await a.call(
        "POST", "/sessions/", "root", json={"gateway_id": str(a.gateways["unassigned"].id)}
    )
    assert result.status_code == 404


async def test_connect_permission_is_separate_from_membership(access):
    result = await access.call(
        "POST", "/sessions/", "observer", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert result.status_code == 404
    assert (await access.call("GET", "/gateways/", "observer")).status_code == 200


async def test_shared_accounts_are_not_administrable_by_one_group(access):
    a = access
    result = await a.call("GET", "/admin/users", "admin_a")
    assert result.status_code == 200, result.text
    names = {u["username"] for u in result.json()}
    assert "tech_a" in names and not names & {"root", "shared", "tech_b", "admin_b", "outsider"}
    for name in ("shared", "root", "tech_b", "outsider"):
        for method in ("GET", "PUT", "DELETE"):
            result = await a.call(
                method,
                f"/admin/users/{a.users[name].id}",
                "admin_a",
                **({"json": {"password": "new-test-password"}} if method == "PUT" else {}),
            )
            assert result.status_code == 404, result.text


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"is_admin": True}, 403),
        ({"permissions": ["audit.read"]}, 403),
        ({"group_ids": "foreign"}, 404),
        ({"group_ids": []}, 403),
    ],
)
async def test_delegation_cannot_grant_beyond_actor(access, change, expected):
    a = access
    body = {"username": "newtech", "password": PASSWORD, "group_ids": [str(a.groups["A"].id)]}
    if change.get("group_ids") == "foreign":
        change = {"group_ids": [str(a.groups["B"].id)]}
    result = await a.call("POST", "/admin/users", "limited", json={**body, **change})
    assert result.status_code == expected, result.text
    result = await a.call("PUT", f"/admin/users/{a.users['tech_a'].id}", "limited", json=change)
    assert result.status_code == expected, result.text
    result = await a.call(
        "PUT",
        f"/admin/users/{a.users['admin_a'].id}",
        "limited",
        json={"password": "new-test-password"},
    )
    assert result.status_code == 403, result.text


async def test_account_crud_search_pagination_and_identity(access):
    a = access
    body = {
        "username": "newtech",
        "password": PASSWORD,
        "email": "new@example.test",
        "group_ids": [str(a.groups["A"].id)],
    }
    result = await a.call("POST", "/admin/users", "limited", json=body)
    assert result.status_code == 201, result.text
    identifier = result.json()["id"]
    assert result.json()["permissions"] == ["connect"]
    await a.login("newtech")
    result = await a.call("GET", "/admin/users?q=newtech&limit=1", "limited")
    assert [u["id"] for u in result.json()] == [identifier]
    assert (await a.call("GET", "/admin/users?q=newtech&limit=1&offset=1", "limited")).json() == []
    result = await a.call("PUT", f"/admin/users/{identifier}", "limited", json={"email": None})
    assert result.status_code == 409  # Email changes require a verified recovery flow.
    assert (await a.call("DELETE", f"/admin/users/{identifier}", "limited")).status_code == 204
    assert (await a.call("GET", "/auth/me", "newtech")).status_code == 401
    # Reusing a username never makes an old token identify a different account.
    assert (await a.call("POST", "/admin/users", "limited", json=body)).status_code == 201
    assert (await a.call("GET", "/auth/me", "newtech")).status_code == 401


async def test_last_global_administrator_is_protected_under_concurrency(access):
    a = access
    for body in ({"is_active": False}, {"is_admin": False}):
        result = await a.call("PUT", f"/admin/users/{a.users['root'].id}", json=body)
        assert result.status_code == 409, result.text
    assert (await a.call("DELETE", f"/admin/users/{a.users['root'].id}")).status_code == 400
    result = await a.call(
        "POST",
        "/admin/users",
        json={
            "username": "root_two",
            "email": "root_two@example.test",
            "password": PASSWORD,
            "is_admin": True,
        },
    )
    assert result.status_code == 201, result.text
    second_id = result.json()["id"]
    await a.login("root_two")
    results = await asyncio.gather(
        a.call("PUT", f"/admin/users/{a.users['root'].id}", json={"is_admin": False}),
        a.call("PUT", f"/admin/users/{second_id}", "root_two", json={"is_admin": False}),
    )
    assert sorted(r.status_code for r in results) == [200, 409]


@pytest.mark.parametrize(
    "change",
    [
        {"group_ids": []},
        {"permissions": []},
        {"is_active": False},
        {"password": "new-test-password"},
    ],
)
async def test_access_revocation_terminates_live_vpn_and_invalidates_tokens(access, change):
    a = access
    result = await a.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    assert result.status_code == 200, result.text
    identifier = result.json()["session_id"]
    result = await a.client.post(
        "/api/v1/gateways/heartbeat",
        headers={"X-Gateway-Key": a.keys[str(a.gateways["A"].id)]},
        json={"vpn_ip": a.gateways["A"].vpn_ip, "ready_session_id": identifier},
    )
    assert result.status_code == 200, result.text
    result = await a.client.post(f"/api/v1/internal/sessions/{identifier}/ready", headers=a.worker)
    assert result.status_code == 200 and result.json()["status"] == "ACTIVE"
    result = await a.call("PUT", f"/admin/users/{a.users['tech_a'].id}", json=change)
    assert result.status_code == 200, result.text
    assert (await a.call("GET", "/auth/me", "tech_a")).status_code == 401
    result = await a.client.post(
        "/api/v1/gateways/heartbeat",
        json={},
        headers={"X-Gateway-Key": a.keys[str(a.gateways["A"].id)]},
    )
    assert result.json()["connect"] is False
    result = await a.client.post(
        "/api/v1/internal/vpn/authorize",
        headers=a.worker,
        json={
            "common_name": peer_name("technician", a.users["tech_a"].id),
            "certificate_serial": str(a.users["tech_a"].id.int),
        },
    )
    assert result.status_code == 403
    async with a.db() as db:
        session = await db.get(Session, UUID(identifier))
        assert session.status == "FAILED" and session.cleaned_at is None and session.network_slot
        event = await db.scalar(
            select(AuditLog).where(
                AuditLog.event_type == "session_revoked",
                AuditLog.session_id == session.id,
            )
        )
        assert event.user_id == a.users["root"].id
        assert event.group_id == a.groups["A"].id
    # A different authorized technician must still wait for cleanup.
    result = await a.call(
        "POST", "/sessions/", "admin_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    assert result.status_code == 409
    result = await a.client.post(
        f"/api/v1/internal/sessions/{identifier}/cleaned", headers=a.worker
    )
    assert result.status_code == 200
    result = await a.call(
        "POST", "/sessions/", "admin_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("boundary", ["heartbeat", "worker", "authorize"])
async def test_machine_paths_revalidate_membership_even_without_api_revocation(access, boundary):
    a = access
    result = await a.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    identifier = UUID(result.json()["session_id"])
    async with a.db() as db:
        await db.execute(delete(UserGroup).where(UserGroup.user_id == a.users["tech_a"].id))
        await db.commit()
    if boundary == "heartbeat":
        result = await a.client.post(
            "/api/v1/gateways/heartbeat",
            json={},
            headers={"X-Gateway-Key": a.keys[str(a.gateways["A"].id)]},
        )
        assert result.json()["connect"] is False
    elif boundary == "worker":
        result = await a.client.get("/api/v1/internal/sessions/work", headers=a.worker)
        assert result.json()[0]["status"] == "FAILED"
    else:
        result = await a.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=a.worker,
            json={
                "common_name": peer_name("gateway", a.gateways["A"].id),
                "certificate_serial": str(a.gateways["A"].id.int),
            },
        )
        assert result.status_code == 403
    async with a.db() as db:
        assert (await db.get(Session, identifier)).status == "FAILED"


async def test_historical_scope_does_not_follow_gateway_transfers(access):
    a = access
    gateway = a.gateways["A"]
    result = await a.call("POST", "/sessions/", "tech_a", json={"gateway_id": str(gateway.id)})
    identifier = result.json()["session_id"]
    transfer = {"group_id": str(a.groups["B"].id)}
    assert (
        await a.call("PUT", f"/admin/gateways/{gateway.id}/group", json=transfer)
    ).status_code == 409
    assert (await a.call("DELETE", f"/sessions/{identifier}", "tech_a")).status_code == 200
    assert (
        await a.call("PUT", f"/admin/gateways/{gateway.id}/group", json=transfer)
    ).status_code == 409
    await a.client.post(f"/api/v1/internal/sessions/{identifier}/cleaned", headers=a.worker)
    assert (
        await a.call("PUT", f"/admin/gateways/{gateway.id}/group", json=transfer)
    ).status_code == 200
    assert (await a.call("GET", f"/sessions/{identifier}", "tech_a")).status_code == 200
    for actor in ("tech_b", "admin_b"):
        assert (await a.call("GET", f"/sessions/{identifier}", actor)).status_code == 404
    result = await a.call("GET", "/admin/audit", "admin_b")
    assert not any(row["session_id"] == identifier for row in result.json())
    assert (await a.call("GET", "/admin/sessions", "admin_b")).json() == []
    assert (await a.call("GET", "/admin/stats", "admin_b")).json()["sessions_total"] == 0


async def test_profiles_password_rotation_logout_and_old_token_rejection(access):
    a = access
    assert (await a.call("PATCH", "/auth/me", "tech_a", json={"is_admin": True})).status_code == 422
    result = await a.call("PATCH", "/auth/me", "tech_a", json={"full_name": "Technician A"})
    assert result.status_code == 200 and result.json()["full_name"] == "Technician A"
    result = await a.call(
        "POST",
        "/auth/password",
        "tech_a",
        json={
            "current_password": "wrong-password",
            "new_password": "changed-password",
        },
    )
    assert result.status_code == 400
    result = await a.call(
        "POST",
        "/auth/password",
        "tech_a",
        json={
            "current_password": PASSWORD,
            "new_password": "changed-password",
        },
    )
    assert result.status_code == 200, result.text
    assert (await a.call("GET", "/auth/me", "tech_a")).status_code == 401
    a.headers["tech_a"] = {"Authorization": "Bearer " + result.json()["access_token"]}
    assert (await a.call("GET", "/auth/me", "tech_a")).status_code == 200
    assert (await a.call("POST", "/auth/logout", "tech_a")).status_code == 200
    assert (await a.call("GET", "/auth/me", "tech_a")).status_code == 401
    token = create_access_token({"sub": "tech_a"})
    result = await a.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
    assert result.status_code == 401


async def test_audit_and_counts_are_scoped_and_capabilities_are_enforced(access):
    a = access
    result = await a.call("GET", "/admin/audit", "observer")
    assert result.status_code == 200, result.text
    assert [row["details"] for row in result.json()] == [{"site": "A"}]
    assert result.json()[0]["ip_address"] == "192.0.2.10"
    result = await a.call("GET", "/admin/stats", "observer")
    assert result.status_code == 200 and result.json()["gateways_total"] == 1
    assert result.json()["users_admin"] == 0
    for path in ("/admin/users", "/admin/groups", "/admin/audit", "/admin/stats"):
        assert (await a.call("GET", path, "tech_a")).status_code == 403


async def test_groups_and_machine_registration_cannot_escape_scope(access):
    a = access
    result = await a.call("POST", "/admin/groups", "admin_a", json={"name": "New group"})
    assert result.status_code == 201, result.text
    group_id = result.json()["id"]
    assert group_id in {row["id"] for row in (await a.call("GET", "/groups/", "admin_a")).json()}
    assert (
        await a.call("DELETE", f"/admin/groups/{a.groups['A'].id}", "admin_a")
    ).status_code == 409
    assert (await a.call("DELETE", f"/admin/groups/{group_id}", "admin_a")).status_code == 204
    gateway = a.gateways["A"]
    registration = {
        "gateway_id": str(gateway.id),
        "mac_address": gateway.mac_address,
        "activation_mode": "wifi",
        "phone_number": gateway.phone_number,
        "lan_interface": "eth0",
        "lan_address": "192.168.1.1",
        "lan_cidr": gateway.vpn_subnet,
    }
    headers = {"X-Gateway-Key": a.keys[str(gateway.id)]}
    result = await a.client.post(
        "/api/v1/gateways/register",
        headers=headers,
        json={**registration, "group_id": str(a.groups["B"].id)},
    )
    assert result.status_code == 422
    result = await a.client.post("/api/v1/gateways/register", headers=headers, json=registration)
    assert result.status_code == 200 and result.json()["group_id"] == str(a.groups["A"].id)


async def test_legacy_user_registration_does_not_grant_resources(access):
    a = access
    body = {"username": "legacy_new", "password": PASSWORD}
    assert (await a.call("POST", "/auth/register", "admin_a", json=body)).status_code == 403
    result = await a.call("POST", "/auth/register", json=body)
    assert result.status_code == 200, result.text
    a.headers["legacy_new"] = {"Authorization": "Bearer " + result.json()["access_token"]}
    assert (await a.call("GET", "/gateways/", "legacy_new")).json() == []


async def test_security_audit_failure_rolls_back_account_creation(access, monkeypatch):
    a = access

    def reject(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("api.services.user_service.record_user_event", reject)
    with pytest.raises(Exception, match="audit unavailable|TaskGroup"):
        await a.call("POST", "/admin/users", json={"username": "rolled_back", "password": PASSWORD})
    async with a.db() as db:
        assert await db.scalar(select(User.id).where(User.username == "rolled_back")) is None


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/sessions/{id}"),
        ("DELETE", "/sessions/{id}"),
        ("POST", "/sessions/{id}/wake"),
        ("GET", "/admin/sessions/{id}"),
        ("DELETE", "/admin/sessions/{id}"),
    ],
)
async def test_foreign_session_is_hidden_on_every_human_route(access, method, path):
    a = access
    result = await a.call(
        "POST", "/sessions/", "tech_b", json={"gateway_id": str(a.gateways["B"].id)}
    )
    identifier = result.json()["session_id"]
    result = await a.call(method, path.format(id=identifier), "admin_a")
    assert result.status_code == 404, result.text
    assert (await a.call("GET", "/sessions/", "tech_a")).json() == []
    assert (await a.call("GET", "/admin/sessions", "admin_a")).json() == []


async def test_session_management_capability_does_not_leak_to_ordinary_group_members(access):
    a = access
    result = await a.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    identifier = result.json()["session_id"]
    assert (await a.call("GET", f"/sessions/{identifier}", "shared")).status_code == 404
    assert (await a.call("GET", f"/sessions/{identifier}", "admin_a")).status_code == 200
    assert (await a.call("DELETE", f"/sessions/{identifier}", "admin_a")).status_code == 200


async def test_revoking_an_unrelated_group_preserves_authorized_session(access):
    a = access
    result = await a.call(
        "POST", "/sessions/", "shared", json={"gateway_id": str(a.gateways["B"].id)}
    )
    identifier = UUID(result.json()["session_id"])
    result = await a.call(
        "PUT", f"/admin/users/{a.users['shared'].id}", json={"group_ids": [str(a.groups["B"].id)]}
    )
    assert result.status_code == 200, result.text
    async with a.db() as db:
        assert (await db.get(Session, identifier)).status == "REQUESTED"
    result = await a.client.post(
        "/api/v1/gateways/heartbeat",
        json={},
        headers={"X-Gateway-Key": a.keys[str(a.gateways["B"].id)]},
    )
    assert result.json()["connect"] is True


@pytest.mark.parametrize("operation", ["create_group", "close_session"])
async def test_stale_actor_cannot_mutate_after_waiting_for_privilege_revocation(access, operation):
    a = access
    from api.services.lifecycle import allocation_lock

    identifier = None
    if operation == "close_session":
        created = await a.call(
            "POST", "/sessions/", "tech_a", json={"gateway_id": str(a.gateways["A"].id)}
        )
        assert created.status_code == 200, created.text
        identifier = UUID(created.json()["session_id"])
    async with a.db() as db:
        await allocation_lock(db)
        request = (
            a.call("DELETE", f"/sessions/{identifier}", "admin_a")
            if identifier
            else a.call("POST", "/admin/groups", "admin_a", json={"name": "forbidden"})
        )
        task = asyncio.create_task(request)
        try:
            # Wait for the real request to block on the allocation lock after it
            # has authenticated using the old version. No timing-only assertion.
            for _ in range(200):
                waiting = await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND objid=71702026 AND NOT granted"
                    )
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("The request did not reach the authorization lock")
            actor = await db.get(User, a.users["admin_a"].id)
            actor.permissions = ["connect"]
            actor.token_version += 1
            await db.commit()
            result = await task
            assert result.status_code == 401, result.text
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with a.db() as db:
        assert await db.scalar(select(Group.id).where(Group.name == "forbidden")) is None
        if identifier:
            assert (await db.get(Session, identifier)).status == "REQUESTED"


@pytest.mark.parametrize("field", ["is_active", "is_admin", "password", "permissions", "group_ids"])
async def test_null_security_changes_are_rejected_without_revoking_tokens(access, field):
    a = access
    result = await a.call("PUT", f"/admin/users/{a.users['tech_a'].id}", json={field: None})
    assert result.status_code == 422
    assert (await a.call("GET", "/auth/me", "tech_a")).status_code == 200


async def stale_active_session(a):
    result = await a.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    assert result.status_code == 200, result.text
    identifier = UUID(result.json()["session_id"])
    result = await a.client.post(
        "/api/v1/gateways/heartbeat",
        headers={"X-Gateway-Key": a.keys[str(a.gateways["A"].id)]},
        json={"vpn_ip": a.gateways["A"].vpn_ip, "ready_session_id": str(identifier)},
    )
    assert result.status_code == 200, result.text
    result = await a.client.post(f"/api/v1/internal/sessions/{identifier}/ready", headers=a.worker)
    assert result.status_code == 200 and result.json()["status"] == "ACTIVE", result.text
    async with a.db() as db:
        row = await db.get(Session, identifier)
        row.worker_seen_at = utc_now() - timedelta(seconds=31)
        await db.commit()
    return identifier


@pytest.mark.parametrize("reader", ["human", "worker"])
@pytest.mark.parametrize("revocation", ["reset_password", "logout"])
async def test_revocation_survives_concurrent_status_reads(access, monkeypatch, reader, revocation):
    a = access
    identifier = await stale_active_session(a)
    loaded, resume = asyncio.Event(), asyncio.Event()
    if reader == "human":
        import api.routers.sessions as router

        original = router.get_session_status

        async def delayed(db, session_id):
            # The authorization query has already loaded the old ACTIVE state.
            loaded.set()
            await resume.wait()
            return await original(db, session_id)

        monkeypatch.setattr(router, "get_session_status", delayed)
        request = a.call("GET", f"/sessions/{identifier}", "tech_a")
    else:
        import api.routers.internal as router

        original = router.validate_live

        async def delayed(db, session, **kwargs):
            loaded.set()
            await resume.wait()
            return await original(db, session, **kwargs)

        monkeypatch.setattr(router, "validate_live", delayed)
        request = a.client.get("/api/v1/internal/sessions/work", headers=a.worker)
    task = asyncio.create_task(request)
    try:
        await asyncio.wait_for(loaded.wait(), timeout=5)
        if revocation == "reset_password":
            result = await a.call(
                "PUT",
                f"/admin/users/{a.users['tech_a'].id}",
                json={"password": "new-test-password"},
            )
        else:
            result = await a.call("POST", "/auth/logout", "tech_a")
        assert result.status_code == 200, result.text
        async with a.db() as db:
            revoked = await db.get(Session, identifier)
            assert revoked.status == "FAILED"
            ended_at, reason = revoked.ended_at, revoked.error_reason
        resume.set()
        response = await asyncio.wait_for(task, timeout=5)
        assert response.status_code == 200, response.text
        snapshot = response.json() if reader == "human" else response.json()[0]
        assert snapshot["status"] == "FAILED"
        async with a.db() as db:
            session = await db.get(Session, identifier)
            assert (session.status, session.ended_at, session.error_reason) == (
                "FAILED",
                ended_at,
                reason,
            )
            assert session.cleaned_at is None and session.network_slot is not None
        authorized = await a.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=a.worker,
            json={
                "common_name": peer_name("technician", a.users["tech_a"].id),
                "certificate_serial": str(a.users["tech_a"].id.int),
            },
        )
        assert authorized.status_code == 403, authorized.text
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_revocation_waits_for_worker_activation_to_commit(access, monkeypatch):
    a = access
    identifier = await stale_active_session(a)
    import api.routers.internal as router

    original = router.validate_live
    validated, resume = asyncio.Event(), asyncio.Event()

    async def delayed(db, session, **kwargs):
        result = await original(db, session, **kwargs)
        validated.set()
        await resume.wait()
        return result

    monkeypatch.setattr(router, "validate_live", delayed)
    activation = asyncio.create_task(
        a.client.post(f"/api/v1/internal/sessions/{identifier}/ready", headers=a.worker)
    )
    revocation = None
    try:
        await asyncio.wait_for(validated.wait(), timeout=5)
        revocation = asyncio.create_task(a.call("POST", "/auth/logout", "tech_a"))
        # The competing request must actually reach the row lock. If validation
        # commits early, revocation completes and the stale activation can win.
        waiting = False
        async with a.db() as db:
            for _ in range(200):
                if revocation.done():
                    break
                waiting = await db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks "
                        "WHERE locktype='transactionid' AND NOT granted)"
                    )
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("The revocation did not reach the session row lock")
        resume.set()
        ready, revoked = await asyncio.wait_for(asyncio.gather(activation, revocation), timeout=5)
        assert ready.status_code == 200, ready.text
        assert revoked.status_code == 200, revoked.text
        async with a.db() as db:
            assert (await db.get(Session, identifier)).status == "FAILED"
        assert waiting, "Validation released the row lock before activation committed"
        result = await a.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=a.worker,
            json={
                "common_name": peer_name("technician", a.users["tech_a"].id),
                "certificate_serial": str(a.users["tech_a"].id.int),
            },
        )
        assert result.status_code == 403, result.text
    finally:
        resume.set()
        tasks = [task for task in (activation, revocation) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("operation", ["read", "close", "wake", "ready", "authorize"])
async def test_status_maintenance_persists_even_when_operation_is_rejected(access, operation):
    a = access
    identifier = await stale_active_session(a)
    async with a.db() as db:
        session = await db.get(Session, identifier)
        session.expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    if operation == "authorize":
        response = await a.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=a.worker,
            json={
                "common_name": peer_name("technician", a.users["tech_a"].id),
                "certificate_serial": str(a.users["tech_a"].id.int),
            },
        )
        expected = 403
    elif operation == "ready":
        response = await a.client.post(
            f"/api/v1/internal/sessions/{identifier}/ready",
            headers=a.worker,
        )
        expected = 409
    else:
        method, suffix, expected = {
            "read": ("GET", "", 200),
            "close": ("DELETE", "", 200),
            "wake": ("POST", "/wake", 409),
        }[operation]
        response = await a.call(method, f"/sessions/{identifier}{suffix}", "tech_a")
    assert response.status_code == expected, response.text
    async with a.db() as db:
        session = await db.get(Session, identifier)
        assert session.status == "EXPIRED" and session.ended_at is not None
        assert session.cleaned_at is None and session.network_slot is not None
