"""Adversarial browser/VPN lifetime checks through real API and PostgreSQL."""

import asyncio
import os
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from api.main import app
from api.models.session import Session
from api.models.session_request import SessionRequest
from api.models.web_session import WebSession
from api.routers import sessions as session_router
from api.services import lifecycle, session_service, web_session_service
from api.time_utils import utc_now
from factoryvpn_common.protocol import peer_name
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_sso_postgres import provider as provider_fixture, enable, sign_in
from test_vpn_operations_postgres import open_vpn, sms_gateway
from test_web_portal_postgres import browser as browser_fixture, login, sso_login

access = access_fixture
encoded_password = password_fixture
browser = browser_fixture
provider = provider_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


async def browser_login(browser, provider, mode):
    if mode == "local":
        await login(browser, "tech_a")
    else:
        assert (await sso_login(browser, provider)).status_code == 303
        meta = await browser.get("/api/v1/web/session")
        assert meta.status_code == 200
        browser.headers["X-CSRF-Token"] = meta.json()["csrf_token"]


async def machine_cannot_resume(access, identifier):
    gateway = access.gateways["A"]
    heartbeat = await access.client.post(
        "/api/v1/gateways/heartbeat",
        headers={"X-Gateway-Key": access.keys[str(gateway.id)]},
        json={"ready_session_id": str(identifier), "vpn_ip": gateway.vpn_ip},
    )
    assert heartbeat.status_code == 200 and not heartbeat.json()["connect"]
    assert heartbeat.json()["lease_seconds"] == 0
    for kind, peer in (("gateway", gateway), ("technician", access.users["tech_a"])):
        result = await access.client.post(
            "/api/v1/internal/vpn/authorize",
            headers=access.worker,
            json={"common_name": peer_name(kind, peer.id), "certificate_serial": str(peer.id.int)},
        )
        assert result.status_code == 403
    ready = await access.client.post(
        f"/api/v1/internal/sessions/{identifier}/ready", headers=access.worker
    )
    assert ready.status_code == 409


@pytest.mark.parametrize("mode", ["local", "sso"])
async def test_open_authenticated_before_logout_cannot_allocate_after_logout(
    access, browser, provider, monkeypatch, mode
):
    if mode == "sso":
        await enable(access, provider)
    await browser_login(browser, provider, mode)
    authenticated, proceed = asyncio.Event(), asyncio.Event()
    original = session_router.lock_actor

    async def delayed_lock(db, actor):
        authenticated.set()
        await proceed.wait()
        await original(db, actor)

    monkeypatch.setattr(session_router, "lock_actor", delayed_lock)
    opening = asyncio.create_task(
        browser.post(
            "/api/v1/sessions/",
            headers={"Idempotency-Key": str(uuid4())},
            json={"gateway_id": str(access.gateways["A"].id)},
        )
    )
    try:
        await asyncio.wait_for(authenticated.wait(), 5)
        result = await asyncio.wait_for(browser.post("/api/v1/web/logout"), 5)
        assert result.status_code == 200
    finally:
        proceed.set()
        result = await asyncio.wait_for(opening, 5)
    assert result.status_code == 401, result.text
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(SessionRequest)) == 0


@pytest.mark.parametrize("mode", ["local", "sso"])
@pytest.mark.parametrize("accepted", [True, False])
async def test_late_sms_outcome_cannot_undo_browser_logout(
    access, browser, provider, monkeypatch, mode, accepted
):
    if mode == "sso":
        await enable(access, provider)
    await browser_login(browser, provider, mode)
    await sms_gateway(access)
    sending, finish = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        sending.set()
        await finish.wait()
        return accepted

    monkeypatch.setattr(session_router, "send_sms", send)
    opening = asyncio.create_task(
        browser.post("/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)})
    )
    try:
        await asyncio.wait_for(sending.wait(), 5)
        result = await asyncio.wait_for(browser.post("/api/v1/web/logout"), 5)
        assert result.status_code == 200
    finally:
        finish.set()
        result = await asyncio.wait_for(opening, 5)
    assert result.status_code == 200 and result.json()["status"] == "CLOSED", result.text
    identifier = UUID(result.json()["session_id"])
    await machine_cannot_resume(access, identifier)
    work = await access.client.get("/api/v1/internal/sessions/work", headers=access.worker)
    assert work.status_code == 200 and work.json()[0]["status"] == "CLOSED"
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        assert vpn.error_reason == "Portal session signed out"
        assert vpn.cleaned_at is None and vpn.network_slot is not None
    # A terminal row remains reserved until the worker confirms actual cleanup.
    retry = await open_vpn(access, uuid4(), actor="admin_a")
    assert retry.status_code == 409


@pytest.mark.parametrize("mode", ["local", "sso"])
async def test_browser_deadline_bounds_worker_lease_and_prevents_reconnection(
    access, browser, provider, monkeypatch, mode
):
    if mode == "sso":
        await enable(access, provider)
    await browser_login(browser, provider, mode)
    deadline = utc_now() + timedelta(seconds=20)
    async with access.db() as db:
        web = await db.scalar(select(WebSession))
        web.expires_at = deadline
        await db.commit()
    result = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert result.status_code == 200, result.text
    identifier = UUID(result.json()["session_id"])
    async with access.db() as db:
        assert (await db.get(Session, identifier)).expires_at == deadline
    work = await access.client.get("/api/v1/internal/sessions/work", headers=access.worker)
    assert work.status_code == 200 and 0 < work.json()[0]["lease_seconds"] <= 20
    # Advance the service clocks without waiting or changing the stored deadlines.
    for service in (lifecycle, session_service, web_session_service):
        monkeypatch.setattr(service, "utc_now", lambda: deadline + timedelta(seconds=1))
    work = await access.client.get("/api/v1/internal/sessions/work", headers=access.worker)
    assert work.status_code == 200 and work.json()[0]["status"] == "EXPIRED"
    assert work.json()[0]["lease_seconds"] == 0
    assert (await browser.get("/api/v1/web/session")).status_code == 401
    await machine_cannot_resume(access, identifier)


@pytest.mark.parametrize("mode", ["local", "sso"])
async def test_idempotency_replay_from_another_browser_keeps_original_lifetime(
    access, browser, provider, mode
):
    if mode == "sso":
        await enable(access, provider)
    await browser_login(browser, provider, mode)
    key = {"Idempotency-Key": str(uuid4())}
    body = {"gateway_id": str(access.gateways["A"].id)}
    original = await browser.post("/api/v1/sessions/", headers=key, json=body)
    assert original.status_code == 200
    identifier = UUID(original.json()["session_id"])
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        original_browser = vpn.web_session_id
        original_sso = vpn.sso_session_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        headers={"Origin": "https://test", "X-FactoryVPN-Portal": "1"},
    ) as other:
        await browser_login(other, provider, mode)
        replay = await other.post("/api/v1/sessions/", headers=key, json=body)
        assert replay.status_code == 200 and replay.json()["replayed"]
        assert replay.json()["session_id"] == str(identifier)
        assert (await other.post("/api/v1/web/logout")).status_code == 200
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        assert vpn.status == "REQUESTED"
        assert (vpn.web_session_id, vpn.sso_session_id) == (original_browser, original_sso)
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    async with access.db() as db:
        assert (await db.get(Session, identifier)).status == "CLOSED"


@pytest.mark.parametrize("mode", ["local", "sso"])
@pytest.mark.parametrize("global_logout", [False, True])
async def test_only_global_browser_logout_closes_an_independent_bearer_vpn(
    access, browser, provider, mode, global_logout
):
    if mode == "sso":
        await enable(access, provider)
        await sign_in(access, provider)
    await browser_login(browser, provider, mode)
    opened = await open_vpn(access, uuid4())
    assert opened.status_code == 200
    identifier = UUID(opened.json()["session_id"])
    path = "/api/v1/web/logout-all" if global_logout else "/api/v1/web/logout"
    assert (await browser.post(path)).status_code == 200
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        assert vpn.web_session_id is None
        assert vpn.status == ("FAILED" if global_logout else "REQUESTED")
    identity = await access.call("GET", "/auth/me", "tech_a")
    assert identity.status_code == (401 if global_logout else 200)
