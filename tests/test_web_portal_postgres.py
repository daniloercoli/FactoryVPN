"""Real cookie, CSRF, resource authorization and database tests for browser clients."""

import os
import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from api.config import settings
from api.main import app
from api.models.web_session import WebSession
from api.models.user import User
from api.models.group import Group, UserGroup
from api.models.sso import SSOSession, SSOLoginAttempt
from api.services.web_session_service import cookie_name
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
    PASSWORD,
)
from test_sso_postgres import provider as provider_fixture, enable, sign_in, age

access = access_fixture
encoded_password = password_fixture
provider = provider_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


@pytest.fixture
async def browser(access, monkeypatch):
    monkeypatch.setattr(settings, "PORTAL_PUBLIC_URL", "https://test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        headers={"Origin": "https://test", "X-FactoryVPN-Portal": "1"},
    ) as client:
        yield client


async def login(browser, username="root"):
    result = await browser.post(
        "/api/v1/web/login", json={"username": username, "password": PASSWORD}
    )
    from email_helpers import complete

    result = await complete(browser, result, portal=True)
    assert result.status_code == 200, result.text
    browser.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return result


async def test_cookie_login_is_opaque_and_persists_across_page_reload(access, browser):
    result = await login(browser)
    assert "access_token" not in result.json()
    cookie = result.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "__Host-factoryvpn_portal=" in cookie and "Domain=" not in cookie
    assert result.headers["cache-control"] == "no-store"
    secret = browser.cookies[cookie_name()]
    async with access.db() as db:
        row = await db.scalar(select(WebSession))
        assert secret != row.secret_hash and secret not in row.secret_hash
    session = await browser.get("/api/v1/web/session")
    assert session.status_code == 200 and session.json()["user"]["username"] == "root"
    assert session.json()["csrf_token"] == result.json()["csrf_token"]
    assert (await browser.get("/api/v1/admin/users")).status_code == 200


@pytest.mark.parametrize("headers", [{"Origin": "https://evil.test"}, {"X-FactoryVPN-Portal": ""}])
async def test_login_requires_trusted_origin_and_custom_header(browser, headers):
    result = await browser.post(
        "/api/v1/web/login", json={"username": "root", "password": PASSWORD}, headers=headers
    )
    assert result.status_code == 403


@pytest.mark.parametrize(
    "headers", [{"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": ""}, {"Origin": "https://evil.test"}]
)
async def test_cookie_mutations_require_csrf_and_origin(browser, headers):
    await login(browser)
    result = await browser.patch("/api/v1/auth/me", json={"full_name": "changed"}, headers=headers)
    assert result.status_code == 403
    assert (await browser.get("/api/v1/auth/me")).json()["full_name"] is None


async def test_browser_logout_preserves_other_browser_and_bearer(access, browser):
    await login(browser, "tech_a")
    old_cookie = browser.cookies[cookie_name()]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        headers={"Origin": "https://test", "X-FactoryVPN-Portal": "1"},
    ) as other:
        await login(other, "tech_a")
        result = await browser.post("/api/v1/web/logout")
        assert result.status_code == 200
        assert (await browser.get("/api/v1/web/session")).status_code == 401
        assert (await other.get("/api/v1/web/session")).status_code == 200
        assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 200
    browser.cookies.set(cookie_name(), old_cookie)
    assert (await browser.get("/api/v1/web/session")).status_code == 401


async def test_global_logout_revokes_bearer_and_all_browsers(access, browser):
    await login(browser, "tech_a")
    assert (await browser.post("/api/v1/web/logout-all")).status_code == 200
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    async with access.db() as db:
        row = await db.scalar(select(WebSession))
        assert row.token_version != (await db.get(User, row.user_id)).token_version


async def test_portal_password_rotates_cookie_and_never_returns_jwt(access, browser):
    await login(browser, "tech_a")
    old_cookie = browser.cookies[cookie_name()]
    result = await browser.post(
        "/api/v1/web/password",
        json={"current_password": PASSWORD, "new_password": "new-password-portal"},
    )
    assert result.status_code == 200 and "access_token" not in result.json()
    assert browser.cookies[cookie_name()] != old_cookie
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    assert (await browser.get("/api/v1/web/session")).status_code == 200
    assert (
        await browser.post(
            "/api/v1/auth/password",
            json={"current_password": "new-password-portal", "new_password": PASSWORD},
        )
    ).status_code == 403


async def test_browser_credentials_reject_expiry_and_admin_revocation(access, browser):
    await login(browser, "tech_a")
    async with access.db() as db:
        row = await db.scalar(select(WebSession))
        row.expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    assert (await browser.get("/api/v1/web/session")).status_code == 401
    await login(browser, "tech_a")
    assert (
        await access.call(
            "PUT", f'/admin/users/{access.users["tech_a"].id}', json={"is_active": False}
        )
    ).status_code == 200
    assert (await browser.get("/api/v1/auth/me")).status_code == 401


async def test_cookie_auth_keeps_group_boundaries_and_rejects_mixed_credentials(access, browser):
    await login(browser, "admin_a")
    users = await browser.get("/api/v1/admin/users")
    assert users.status_code == 200
    assert "tech_b" not in [u["username"] for u in users.json()]
    assert (
        await browser.get(f'/api/v1/admin/users/{access.users["tech_b"].id}')
    ).status_code == 404
    assert (await browser.get("/api/v1/auth/me", headers=access.headers["root"])).status_code == 401
    assert (await browser.get("/api/v1/internal/sessions/work")).status_code != 200


async def sso_login(browser, provider):
    start = await browser.post("/api/v1/web/sso/start", json={"provider_id": "cognito"})
    assert start.status_code == 200, start.text
    authorize = await browser.get(start.json()["authorization_url"])
    assert authorize.status_code == 302
    values = parse_qs(urlsplit(authorize.headers["location"]).query)
    provider.nonce = values["nonce"][0]
    provider.challenge = values["code_challenge"][0]
    return await browser.get(
        "/api/v1/auth/sso/cognito/callback",
        params={"state": values["state"][0], "code": "test-code"},
    )


async def test_sso_callback_issues_cookie_without_client_handoff_or_tokens(
    access, browser, provider
):
    await enable(access, provider)
    result = await sso_login(browser, provider)
    assert result.status_code == 303 and result.headers["location"] == "/app/"
    session = await browser.get("/api/v1/web/session")
    assert session.status_code == 200 and session.json()["user"]["auth_mode"] == "sso"
    assert "access_token" not in session.json()
    async with access.db() as db:
        attempt = await db.scalar(select(SSOLoginAttempt))
        assert attempt.portal and attempt.consumed_at and not attempt.exchange_hash
        web = await db.scalar(select(WebSession))
        sso = await db.scalar(select(SSOSession))
        assert web.sso_session_id == sso.id and web.expires_at <= sso.expires_at


async def test_sso_browser_logout_revokes_only_its_sso_session(access, browser, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    await sso_login(browser, provider)
    result = await browser.get("/api/v1/web/session")
    browser.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 200
    async with access.db() as db:
        sessions = (await db.scalars(select(SSOSession))).all()
        assert len(sessions) == 2 and sum(row.revoked_at is not None for row in sessions) == 1


async def test_sso_revocation_and_staleness_apply_to_browser(access, browser, provider):
    await enable(access, provider)
    await sso_login(browser, provider)
    await age(access, 125)
    assert (await browser.get("/api/v1/web/session")).status_code == 401
    assert (await browser.get("/api/v1/groups/")).status_code == 401


async def test_portal_sso_failure_returns_safe_login_page(access, browser, provider):
    provider.claims = {"nonce": "wrong"}
    result = await sso_login(browser, provider)
    assert result.status_code == 303
    assert result.headers["location"] == "/app/login?error=sso"
    assert cookie_name() not in browser.cookies
    async with access.db() as db:
        assert await db.scalar(select(WebSession)) is None


async def test_client_cannot_select_portal_redirect_or_override_attempt_kind(browser, provider):
    result = await browser.post(
        "/api/v1/web/sso/start",
        json={"provider_id": "cognito", "redirect_uri": "https://evil.test"},
    )
    assert result.status_code == 422

    result = await browser.post(
        "/api/v1/auth/sso/start",
        json={
            "provider_id": "cognito",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "code_challenge": "a" * 43,
            "state": "a" * 32,
            "portal": True,
        },
    )
    assert result.status_code == 422


async def test_portal_forms_require_revision_and_detect_legacy_api_changes(access, browser):
    await login(browser)
    path = f'/api/v1/admin/users/{access.users["tech_a"].id}'
    original = (await browser.get(path)).json()
    assert (await browser.put(path, json={"full_name": "stale"})).status_code == 428
    await access.call("PUT", path.removeprefix("/api/v1"), json={"group_ids": []})
    result = await browser.put(
        path,
        json={"group_ids": original["group_ids"]},
        headers={"If-Match": f'"{original["revision"]}"'},
    )
    assert result.status_code == 412
    assert (await browser.get(path)).json()["group_ids"] == []


async def test_concurrent_portal_edits_cannot_overwrite_each_other(access, browser):
    await login(browser)
    path = f'/api/v1/admin/users/{access.users["tech_a"].id}'
    original = (await browser.get(path)).json()
    results = await asyncio.gather(
        *(
            browser.put(
                path, json={"full_name": name}, headers={"If-Match": f'"{original["revision"]}"'}
            )
            for name in ("first", "second")
        )
    )
    assert sorted(r.status_code for r in results) == [200, 412]


async def test_group_and_gateway_forms_detect_stale_edits(access, browser):
    await login(browser)
    group_path = f'/api/v1/admin/groups/{access.groups["A"].id}'
    original = (await browser.get(group_path)).json()
    headers = {"If-Match": f'"{original["revision"]}"'}
    assert (
        await browser.put(group_path, json={"name": "Updated A"}, headers=headers)
    ).status_code == 200
    assert (await browser.delete(group_path, headers=headers)).status_code == 412
    gateway_path = f'/api/v1/admin/gateways/{access.gateways["A"].id}'
    gateway = (await browser.get(gateway_path)).json()
    await access.call(
        "PUT",
        gateway_path.removeprefix("/api/v1") + "/group",
        json={"group_id": str(access.groups["B"].id)},
    )
    assert (
        await browser.put(
            gateway_path + "/group",
            json={"group_id": str(access.groups["A"].id)},
            headers={"If-Match": f'"{gateway["revision"]}"'},
        )
    ).status_code == 412


async def test_user_filters_and_counts_cannot_expand_delegated_scope(access, browser):
    await login(browser, "admin_a")
    result = await browser.get(
        "/api/v1/admin/users", params={"group_id": str(access.groups["B"].id)}
    )
    assert result.json() == [] and result.headers["x-total-count"] == "0"
    result = await browser.get(
        "/api/v1/admin/users",
        params={"q": "tech_", "auth_mode": "local", "is_active": "true", "limit": 1},
    )
    assert [u["username"] for u in result.json()] == ["tech_a"]
    assert result.headers["x-total-count"] == "1"


async def test_profile_update_uses_same_revision_and_rejects_cross_origin_reads(browser):
    session = (await login(browser, "tech_a")).json()
    assert (
        await browser.get("/api/v1/web/session", headers={"Origin": "https://evil.test"})
    ).status_code == 403
    result = await browser.patch(
        "/api/v1/auth/me",
        json={"full_name": "My name"},
        headers={"If-Match": f'"{session["user"]["revision"]}"'},
    )
    assert result.status_code == 200 and result.json()["full_name"] == "My name"


async def test_identity_changes_invalidate_open_user_form(access, browser, provider):
    await login(browser)
    path = f'/api/v1/admin/users/{access.users["tech_a"].id}'
    original = (await browser.get(path)).json()
    headers = {"If-Match": f'"{original["revision"]}"'}
    result = await browser.post(
        path + "/identities",
        json={"provider_id": "cognito", "subject": provider.subject},
        headers=headers,
    )
    assert result.status_code == 201
    assert (
        await browser.put(path + "/authentication", json={"auth_mode": "sso"}, headers=headers)
    ).status_code == 412


async def test_provider_signed_out_returns_to_portal_when_enabled(browser):
    result = await browser.get("/api/v1/auth/sso/signed-out")
    assert result.status_code == 303
    assert result.headers["location"] == "/app/login"
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("invalidation", ["logout", "expiry"])
async def test_browser_mutation_rechecks_session_after_waiting_for_lock(
    access, browser, invalidation
):
    from api.services.lifecycle import allocation_lock

    await login(browser, "admin_a")
    async with access.db() as db:
        await allocation_lock(db)
        task = asyncio.create_task(
            browser.post("/api/v1/admin/groups", json={"name": "revoked-browser-group"})
        )
        try:
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
                pytest.fail("The browser request did not reach the authorization lock")
            row = await db.scalar(select(WebSession))
            if invalidation == "logout":
                row.revoked_at = utc_now()
            else:
                row.expires_at = utc_now() - timedelta(seconds=1)
            # No token_version change: this is specific to the current browser.
            await db.commit()
            result = await task
            assert result.status_code == 401, result.text
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with access.db() as db:
        assert (
            await db.scalar(select(Group.id).where(Group.name == "revoked-browser-group"))
        ) is None


async def test_portal_callback_rejects_missing_browser_binding_and_replay(
    access, browser, provider
):
    await enable(access, provider)
    start = await browser.post("/api/v1/web/sso/start", json={"provider_id": "cognito"})
    authorize = await browser.get(start.json()["authorization_url"])
    values = parse_qs(urlsplit(authorize.headers["location"]).query)
    provider.nonce = values["nonce"][0]
    provider.challenge = values["code_challenge"][0]
    params = {"state": values["state"][0], "code": "test-code"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as unbound:
        rejected = await unbound.get("/api/v1/auth/sso/cognito/callback", params=params)
        assert rejected.status_code == 400
        assert cookie_name() not in unbound.cookies
    accepted = await browser.get("/api/v1/auth/sso/cognito/callback", params=params)
    assert accepted.status_code == 303
    replay = await browser.get("/api/v1/auth/sso/cognito/callback", params=params)
    assert replay.status_code == 400
    async with access.db() as db:
        assert len((await db.scalars(select(WebSession))).all()) == 1


@pytest.mark.parametrize("protected", ["root", "shared", "admin_a"])
async def test_group_deletion_cannot_bypass_delegated_account_permissions(
    access, browser, protected
):
    group_id = uuid4()
    async with access.db() as db:
        actor = await db.get(User, access.users["limited"].id)
        actor.permissions = ["connect", "groups.manage", "users.manage"]
        db.add(Group(id=group_id, name="Group with protected member"))
        await db.flush()
        db.add_all(
            UserGroup(user_id=access.users[name].id, group_id=group_id)
            for name in ("limited", protected)
        )
        await db.commit()
    await login(browser, "limited")
    path = f"/api/v1/admin/groups/{group_id}"
    original = (await browser.get(path)).json()
    result = await browser.delete(path, headers={"If-Match": f'"{original["revision"]}"'})
    assert result.status_code == 403, result.text
    async with access.db() as db:
        assert await db.get(Group, group_id)
        assert await db.scalar(
            select(UserGroup.user_id).where(
                UserGroup.group_id == group_id, UserGroup.user_id == access.users[protected].id
            )
        )
    # The global administrator can still explicitly delete the populated group.
    assert (await access.call("DELETE", path.removeprefix("/api/v1"))).status_code == 204
