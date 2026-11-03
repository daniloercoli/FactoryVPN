"""Durable retries and shared SMS cooldown against real concurrent transactions."""

import asyncio
import os
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from unittest.mock import AsyncMock
import pytest
import httpx
from sqlalchemy import select, func

from api.models.gateway import Gateway
from api.models.session import Session
from api.models.session_request import SessionRequest
from api.models.web_session import WebSession
from api.services.lifecycle import validate_live
from api.main import app
from api.time_utils import utc_now
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


async def open_vpn(access, key, actor="tech_a", group="A"):
    return await access.client.post(
        "/api/v1/sessions/",
        headers={**access.headers[actor], "Idempotency-Key": str(key)},
        json={"gateway_id": str(access.gateways[group].id)},
    )


async def sms_gateway(access):
    async with access.db() as db:
        (await db.get(Gateway, access.gateways["A"].id)).activation_mode = "sms"
        await db.commit()


async def test_concurrent_open_retries_create_one_session_and_recover_terminal_outcome(access):
    key = uuid4()
    results = await asyncio.gather(*(open_vpn(access, key) for _ in range(3)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert sum(not r.json()["replayed"] for r in results) == 1
    identifier = results[0].json()["session_id"]
    assert {r.json()["session_id"] for r in results} == {identifier}
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        assert await db.scalar(select(func.count()).select_from(SessionRequest)) == 1
    await access.call("DELETE", f"/sessions/{identifier}", "tech_a")
    replay = await open_vpn(access, key)
    assert replay.json()["status"] == "CLOSED" and replay.json()["replayed"]
    changed = await open_vpn(access, key, group="B")
    assert changed.status_code == 409
    other = await open_vpn(access, key, "tech_b", "B")
    assert other.status_code == 200 and other.json()["session_id"] != identifier


async def test_open_retry_rechecks_scope_after_membership_revocation(access):
    key = uuid4()
    assert (await open_vpn(access, key)).status_code == 200
    await access.call(
        "PUT",
        f'/admin/users/{access.users["tech_a"].id}',
        json={"group_ids": [str(access.groups["B"].id)]},
    )
    await access.login("tech_a")
    assert (await open_vpn(access, key)).status_code == 404


async def test_browser_open_uses_cookie_csrf_and_same_idempotency_key(access, browser):
    await login(browser, "tech_a")
    headers = {"Idempotency-Key": str(uuid4())}
    body = {"gateway_id": str(access.gateways["A"].id)}
    assert (
        await browser.post("/api/v1/sessions/", headers={**headers, "X-CSRF-Token": ""}, json=body)
    ).status_code == 403
    first = await browser.post("/api/v1/sessions/", headers=headers, json=body)
    second = await browser.post("/api/v1/sessions/", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    assert second.json()["replayed"]


async def test_sms_request_retry_never_resends_even_while_provider_is_waiting(access, monkeypatch):
    await sms_gateway(access)
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        started.set()
        await finish.wait()
        return True

    provider = AsyncMock(side_effect=send)
    monkeypatch.setattr("api.routers.sessions.send_sms", provider)
    key = uuid4()
    pending = asyncio.create_task(open_vpn(access, key))
    try:
        await asyncio.wait_for(started.wait(), 5)
        replay = await asyncio.wait_for(open_vpn(access, key), 5)
        assert replay.status_code == 200 and replay.json()["replayed"]
        blocked = await access.call(
            "POST", f'/sessions/{replay.json()["session_id"]}/wake', "admin_a"
        )
        assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
        provider.assert_awaited_once()
        await access.call("DELETE", f'/sessions/{replay.json()["session_id"]}', "tech_a")
    finally:
        finish.set()
        result = await pending
    assert result.json()["status"] == "CLOSED"


async def test_sms_failed_initial_request_is_durable_and_not_sent_again(access, monkeypatch):
    await sms_gateway(access)
    provider = AsyncMock(return_value=False)
    monkeypatch.setattr("api.routers.sessions.send_sms", provider)
    key = uuid4()
    first = await open_vpn(access, key)
    second = await open_vpn(access, key)
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "FAILED"
    assert second.json()["replayed"]
    provider.assert_awaited_once()


async def test_sms_cooldown_shared_between_users_and_failed_resends(access, monkeypatch):
    await sms_gateway(access)
    provider = AsyncMock(return_value=True)
    monkeypatch.setattr("api.routers.sessions.send_sms", provider)
    result = await open_vpn(access, uuid4())
    identifier = result.json()["session_id"]
    async with access.db() as db:
        row = await db.get(Session, UUID(identifier))
        expiry = row.expires_at
        row.sms_last_attempt_at = utc_now() - timedelta(seconds=61)
        await db.commit()
    provider.return_value = False
    results = await asyncio.gather(
        access.call("POST", f"/sessions/{identifier}/wake", "tech_a"),
        access.call("POST", f"/sessions/{identifier}/wake", "admin_a"),
    )
    assert sorted(r.status_code for r in results) == [200, 429]
    assert provider.await_count == 2
    detail = await access.call("GET", f"/sessions/{identifier}", "tech_a")
    assert detail.json()["status"] == "REQUESTED"
    assert datetime.fromisoformat(detail.json()["expires_at"]) == expiry
    assert detail.json()["sms_last_attempt_at"]
    assert (await access.call("POST", f"/sessions/{identifier}/wake", "tech_b")).status_code == 404


async def test_invalid_idempotency_key_never_opens_session(access):
    response = await open_vpn(access, "invalid")
    assert response.status_code == 422
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0


@pytest.mark.parametrize("mode", ["local", "sso"])
async def test_browser_logout_closes_only_its_vpn_for_local_and_sso(
    access, browser, provider, mode
):
    if mode == "sso":
        await enable(access, provider)
        assert (await sso_login(browser, provider)).status_code == 303
        meta = (await browser.get("/api/v1/web/session")).json()
        browser.headers["X-CSRF-Token"] = meta["csrf_token"]
    else:
        await login(browser, "tech_a")
    result = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert result.status_code == 200, result.text
    identifier = UUID(result.json()["session_id"])
    independent = await open_vpn(access, uuid4(), "tech_b", "B")
    assert independent.status_code == 200
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        web = await db.get(WebSession, vpn.web_session_id)
        assert vpn.web_session_id and vpn.expires_at <= web.expires_at
        assert bool(vpn.sso_session_id) == (mode == "sso")
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        assert vpn.status == "CLOSED" and vpn.cleaned_at is None
        assert (await db.get(Session, UUID(independent.json()["session_id"]))).status == "REQUESTED"


async def test_logout_another_browser_of_same_user_does_not_close_opening_browser_vpn(
    access, browser
):
    await login(browser, "tech_a")
    created = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        headers={"Origin": "https://test", "X-FactoryVPN-Portal": "1"},
    ) as other:
        await login(other, "tech_a")
        assert (await other.post("/api/v1/web/logout")).status_code == 200
    async with access.db() as db:
        assert (await db.get(Session, UUID(created.json()["session_id"]))).status == "REQUESTED"
    assert (await browser.get("/api/v1/web/session")).status_code == 200


@pytest.mark.parametrize("invalid", ["expired", "deleted", "revoked"])
async def test_worker_revalidation_fails_closed_for_invalid_opening_browser(
    access, browser, invalid
):
    await login(browser, "tech_a")
    result = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    identifier = UUID(result.json()["session_id"])
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        web = await db.get(WebSession, vpn.web_session_id)
        if invalid == "deleted":
            await db.delete(web)
        elif invalid == "expired":
            web.expires_at = utc_now() - timedelta(seconds=1)
        else:
            web.revoked_at = utc_now()
        await db.commit()
    async with access.db() as db:
        vpn = await db.get(Session, identifier)
        assert not await validate_live(db, vpn, require_activation=False)
        assert vpn.status == "FAILED" and "Portal session" in vpn.error_reason
        assert vpn.cleaned_at is None
