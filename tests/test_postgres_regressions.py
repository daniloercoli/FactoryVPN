"""Optional smoke test against a dedicated PostgreSQL test database.

Set FACTORYVPN_TEST_DATABASE_URL to enable. Each run uses and drops a private schema.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.config import settings
from provisioning_helpers import seed_certificate, seed_gateway
from api.db.session import Base, get_db
from api.main import app
from api.models.device import Device
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.user import User
from api.models.group import Group, UserGroup
from api.services.auth_service import issue_access_token
from api.services.audit_service import log_event
from api.models.audit_log import AuditLog
from sqlalchemy import select


@pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)
async def test_gateway_registration_session_permissions_and_expiry_in_postgres(monkeypatch):
    schema = "factoryvpn_test_" + uuid4().hex
    engine = create_async_engine(
        os.environ["FACTORYVPN_TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    phone = "+393331234567"
    provisioned_id = uuid4()
    gateway_key, worker_key = "gateway-" + "g" * 32, "worker-" + "w" * 32
    monkeypatch.setattr(settings, "WORKER_API_KEY", worker_key)
    monkeypatch.setattr(settings, "SMS_PROVIDER_API_KEY", "")
    monkeypatch.setattr(settings, "SMS_PROVIDER_URL", "")

    # Exercise the regression even on CI hosts whose default timezone is UTC.
    original_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Europe/Rome")
    if hasattr(time, "tzset"):
        time.tzset()

    async def database():
        async with sessions() as db:
            yield db

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            owner = User(id=uuid4(), username="owner", password_hash="unused")
            other = User(id=uuid4(), username="other", password_hash="unused")
            group = Group(id=uuid4(), name="Owner group")
            db.add_all([owner, other, group])
            await db.flush()
            db.add(UserGroup(user_id=owner.id, group_id=group.id))
            gateway = Gateway(
                id=provisioned_id,
                activation_mode="sms",
                phone_number=phone,
                mac_address="AA:BB:CC:DD:EE:FF",
            )
            db.add(gateway)
            await seed_gateway(db, gateway, gateway_key, registered=False)
            await seed_certificate(db, "technician", owner.id)
            await seed_certificate(db, "technician", other.id)
            await db.commit()
        app.dependency_overrides[get_db] = database
        owner_headers = {"Authorization": "Bearer " + issue_access_token(owner)}
        other_headers = {"Authorization": "Bearer " + issue_access_token(other)}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/gateways/register",
                headers={
                    "X-Gateway-Key": gateway_key,
                },
                json={
                    "gateway_id": str(provisioned_id),
                    "phone_number": phone,
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                },
            )
            assert response.status_code == 200, response.text
            gateway_id = UUID(response.json()["id"])
            async with sessions() as db:
                registered = await db.get(Gateway, gateway_id)
                registered.group_id = group.id
                await db.commit()
            response = await client.post(
                "/api/v1/sessions/", headers=owner_headers, json={"phone_number": phone}
            )
            assert response.status_code == 200, response.text
            session_id = UUID(response.json()["session_id"])
            path = f"/api/v1/sessions/{session_id}"
            assert (await client.get(path, headers=other_headers)).status_code == 404
            assert (await client.delete(path, headers=other_headers)).status_code == 404
            assert (await client.get(path, headers=owner_headers)).status_code == 200
            assert (await client.delete(path, headers=owner_headers)).status_code == 200

            response = await client.post(
                f"/api/v1/internal/sessions/{session_id}/cleaned",
                headers={"X-Worker-Key": worker_key},
            )
            assert response.status_code == 200, response.text
            async with sessions() as db:
                closed = await db.get(Session, session_id)
                assert closed.status == "FAILED"
                assert closed.sms_sent_at is None
                assert abs((closed.requested_at - datetime.now(timezone.utc)).total_seconds()) < 60
                assert abs((closed.ended_at - datetime.now(timezone.utc)).total_seconds()) < 60
                # Tre ore di durata anche quando il processo usa un fuso diverso da UTC.
                duration = closed.expires_at - closed.requested_at
                assert abs(duration - timedelta(hours=3)) < timedelta(minutes=1)
                gateway = await db.get(Gateway, gateway_id)
                assert gateway.vpn_ip
                expired = Session(
                    id=uuid4(),
                    technician_id=owner.id,
                    gateway_id=gateway_id,
                    status="ACTIVE",
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
                second_gateway = Gateway(
                    id=uuid4(), phone_number="+393339999999", mac_address="AA:BB:CC:DD:EE:01"
                )
                db.add(second_gateway)
                await db.flush()
                fresh = Session(
                    id=uuid4(),
                    technician_id=other.id,
                    gateway_id=second_gateway.id,
                    status="REQUESTED",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
                device = Device(id=uuid4(), gateway_id=gateway_id, name="PLC")
                admin = User(id=uuid4(), username="admin", password_hash="unused", is_admin=True)
                db.add_all([expired, fresh, device, admin])
                await db.commit()

            response = await client.post(
                "/api/v1/internal/sessions/expire-stale", headers={"X-Worker-Key": worker_key}
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"expired": 1}
            response = await client.delete(
                f"/api/v1/admin/gateways/{uuid4()}/devices/{device.id}",
                headers={
                    "Authorization": "Bearer " + issue_access_token(admin, email_verified=True)
                },
            )
            assert response.status_code == 404
            async with sessions() as db:
                assert await db.get(Device, device.id) is not None
                assert (await db.get(Session, expired.id)).status == "EXPIRED"
                assert (await db.get(Session, fresh.id)).status == "REQUESTED"

            # Audit identifiers survive physical deletion; failed audit inserts
            # are isolated by a savepoint and do not poison subsequent queries.
            async with sessions() as db:
                removable = Gateway(
                    id=uuid4(), phone_number="+393338888888", mac_address="AA:BB:CC:DD:EE:02"
                )
                db.add(removable)
                await db.commit()
                assert await log_event(db, "device_added", gateway_id=removable.id)
                assert not await log_event(db, "bad_ip", ip_address="not-an-ip")
                assert await db.get(Gateway, removable.id) is not None
            response = await client.delete(
                f"/api/v1/admin/gateways/{removable.id}",
                headers={
                    "Authorization": "Bearer " + issue_access_token(admin, email_verified=True)
                },
            )
            assert response.status_code == 204, response.text
            async with sessions() as db:
                events = (
                    (await db.execute(select(AuditLog).where(AuditLog.gateway_id == removable.id)))
                    .scalars()
                    .all()
                )
                assert {event.event_type for event in events} == {
                    "device_added",
                    "vpn_credentials_revoked",
                    "gateway_deleted",
                }
    finally:
        app.dependency_overrides.clear()
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await engine.dispose()
            if original_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_timezone
            if hasattr(time, "tzset"):
                time.tzset()
