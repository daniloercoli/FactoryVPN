"""Real PostgreSQL lifecycle, unique reservations, revocation and retry tests."""

import asyncio
from datetime import timedelta
import os
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.config import settings
from provisioning_helpers import seed_certificate, seed_gateway
from api.db.session import Base, get_db
from api.main import app
from api.models.session import Session
from api.models.user import User
from api.models.group import Group, UserGroup
from api.models.gateway import Gateway
from api.services.auth_service import issue_access_token
from api.time_utils import utc_now
from factoryvpn_common.protocol import peer_name, verify_activation

pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


@pytest.mark.parametrize("mode", ["sms", "wifi"])
async def test_complete_session_lifecycle_and_concurrent_reservations(monkeypatch, mode):
    schema = "factoryvpn_lifecycle_" + uuid4().hex
    engine = create_async_engine(
        os.environ["FACTORYVPN_TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def db_dependency():
        async with factory() as db:
            yield db

    gateway_id = uuid4()
    key, phone, worker = "g" * 40, "+393331234567", "w" * 40
    monkeypatch.setattr(settings, "WORKER_API_KEY", worker)
    provider = AsyncMock(return_value=True)
    monkeypatch.setattr("api.routers.sessions.send_sms", provider)
    app.dependency_overrides[get_db] = db_dependency
    gh, wh = {"X-Gateway-Key": key}, {"X-Worker-Key": worker}
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            user = User(id=uuid4(), username="tech", password_hash="unused")
            group = Group(id=uuid4(), name="Test site")
            db.add_all([user, group])
            await db.flush()
            db.add(UserGroup(user_id=user.id, group_id=group.id))
            gateway = Gateway(
                id=gateway_id,
                activation_mode=mode,
                phone_number=phone if mode == "sms" else None,
                mac_address="AA:00:00:00:00:01",
            )
            db.add(gateway)
            await seed_gateway(db, gateway, key, registered=False)
            await seed_certificate(db, "technician", user.id)
            await db.commit()
        uh = {"Authorization": "Bearer " + issue_access_token(user)}
        async with AsyncClient(base_url="http://test", transport=ASGITransport(app=app)) as client:
            registration = dict(
                gateway_id=str(gateway_id),
                activation_mode=mode,
                phone_number=phone if mode == "sms" else None,
                mac_address="AA:00:00:00:00:01",
            )
            response = await client.post("/api/v1/gateways/register", headers=gh, json=registration)
            assert response.status_code == 200, response.text
            gateway = response.json()
            assert gateway["vpn_ip"] == "10.100.0.2"
            async with factory() as db:
                registered = await db.get(Gateway, gateway_id)
                registered.group_id = group.id
                await db.commit()
            requests = await asyncio.gather(
                *[
                    client.post(
                        "/api/v1/sessions/", headers=uh, json={"gateway_id": str(gateway_id)}
                    )
                    for _ in range(2)
                ]
            )
            assert sorted(r.status_code for r in requests) == [200, 409]
            identifier = next(r.json()["session_id"] for r in requests if r.status_code == 200)
            if mode == "sms":
                token = provider.call_args.kwargs["message"]
                assert verify_activation(token, key) == identifier
                assert (
                    await client.post("/api/v1/gateways/heartbeat", headers=gh, json={})
                ).json()["connect"] is False
            else:
                provider.assert_not_awaited()
                token = None
            path = f"/api/v1/internal/sessions/{identifier}"
            assert (await client.post(path + "/ready", headers=wh)).status_code == 409
            assert (await client.post(path + "/cleaned", headers=wh)).status_code == 409
            response = await client.post(
                "/api/v1/gateways/heartbeat", headers=gh, json={"activation_token": token}
            )
            state = response.json()
            assert state["connect"] and state["network"]["technician_ip"] == "10.100.128.2"
            assert (
                await client.post(
                    "/api/v1/gateways/heartbeat", headers=gh, json={"vpn_ip": "10.100.0.99"}
                )
            ).status_code == 409
            response = await client.post(
                "/api/v1/gateways/heartbeat",
                headers=gh,
                json={"vpn_ip": gateway["vpn_ip"], "ready_session_id": identifier},
            )
            assert response.status_code == 200
            assert (await client.post(path + "/ready", headers=wh)).json()["status"] == "ACTIVE"
            response = await client.post(
                "/api/v1/gateways/register",
                headers=gh,
                json={**registration, "mac_address": "AA:00:00:00:00:99"},
            )
            assert response.status_code == 409
            cn = peer_name("technician", user.id)
            response = await client.post(
                "/api/v1/internal/vpn/authorize",
                headers=wh,
                json={"common_name": cn, "certificate_serial": str(UUID(cn.split("-", 1)[1]).int)},
            )
            assert response.status_code == 200, response.text
            assert 'push "route 10.100.0.2 255.255.255.255"' in response.json()["config"]
            assert "192.168." not in response.json()["config"]
            response = await client.post(
                "/api/v1/internal/vpn/learn",
                headers=wh,
                json={"common_name": cn, "address": "10.100.0.2"},
            )
            assert response.status_code == 403  # technician cannot spoof another VPN peer
            assert (
                await client.delete(f"/api/v1/sessions/{identifier}", headers=uh)
            ).status_code == 200
            assert (await client.post(path + "/ready", headers=wh)).status_code == 409
            assert (await client.post("/api/v1/gateways/heartbeat", headers=gh, json={})).json()[
                "connect"
            ] is False
            assert (
                await client.post(
                    "/api/v1/internal/vpn/authorize",
                    headers=wh,
                    json={
                        "common_name": cn,
                        "certificate_serial": str(UUID(cn.split("-", 1)[1]).int),
                    },
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/v1/sessions/", headers=uh, json={"gateway_id": str(gateway_id)}
                )
            ).status_code == 409
            assert (await client.post(path + "/cleaned", headers=wh)).status_code == 200
            assert (await client.post(path + "/cleaned", headers=wh)).status_code == 200
            response = await client.post(
                "/api/v1/sessions/", headers=uh, json={"gateway_id": str(gateway_id)}
            )
            assert response.status_code == 200, response.text
            second = response.json()["session_id"]
            async with factory() as db:
                session = await db.get(Session, UUID(second))
                assert session.cleaned_at is None
                session.expires_at = utc_now() - timedelta(seconds=1)
                await db.commit()
            assert (await client.post("/api/v1/gateways/heartbeat", headers=gh, json={})).json()[
                "connect"
            ] is False
            assert (await client.get(f"/api/v1/sessions/{second}", headers=uh)).json()[
                "status"
            ] == "EXPIRED"
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
