"""Emergency console proofs retain ordinary authorization and session lifetimes."""

import asyncio
from datetime import timedelta
import json
import os
from uuid import UUID

from fastapi import HTTPException
from pydantic import SecretStr
import pytest
from sqlalchemy import func, select

from api.config import settings
from api.models import AuditLog, ConsoleAccessCode, Session, User, WebSession
from api.services import console_access, mail_service
from api.services.auth_service import verify_token
from api.services.backup_access import fresh_authentication
from api.services.web_session_service import cookie_name
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
    PASSWORD,
)
from test_web_portal_postgres import browser as browser_fixture


access, encoded_password, browser = access_fixture, password_fixture, browser_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


async def emit(access, username="root", purpose="login"):
    async with access.db() as db:
        user = await db.get(User, access.users[username].id)
        row, code = await console_access.issue(
            db, user, operator="test-operator", reason="SMTP outage", purpose=purpose
        )
        await db.commit()
        return row.id, code


async def sign_in(client, code, username="root", password=PASSWORD, portal=True):
    return await client.post(
        "/api/v1/" + ("web" if portal else "auth") + "/login",
        json={"username": username, "password": password, "emergency_code": code},
    )


@pytest.fixture
def no_email(monkeypatch):
    calls = []

    async def unavailable(*args):
        calls.append(args)
        raise OSError("SMTP is unavailable")

    monkeypatch.setattr(mail_service, "send_code", unavailable)
    monkeypatch.setattr(settings, "EMAIL_CODE_KEY", SecretStr(""))
    return calls


@pytest.mark.parametrize("username", ["root", "tech_a"])
async def test_console_login_without_smtp_retains_role_and_normal_portal_lifetime(
    access, browser, monkeypatch, no_email, username
):
    monkeypatch.setattr(settings, "PORTAL_SESSION_MINUTES", 720)
    async with access.db() as db:
        user = await db.get(User, access.users[username].id)
        user.email_two_step = True
        user.email_locked_until = utc_now() + timedelta(minutes=15)
        await db.commit()
    identifier, code = await emit(access, username)
    started = utc_now()
    response = await sign_in(browser, code.upper(), username)
    assert response.status_code == 200, response.text
    assert "challenge_required" not in response.json()
    assert "access_token" not in response.json()
    assert response.json()["user"]["email_verified"] is False
    assert response.json()["user"]["is_admin"] is (username == "root")
    browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    assert no_email == []
    async with access.db() as db:
        user = await db.get(User, access.users[username].id)
        row = await db.scalar(select(WebSession).where(WebSession.user_id == user.id))
        assert row.second_factor_method == "console" and row.second_factor_at
        assert row.expires_at >= started + timedelta(minutes=719)
        assert row.expires_at <= utc_now() + timedelta(minutes=720)
        assert user.email_verified_at is None and user.email_two_step
        proof = await db.get(ConsoleAccessCode, identifier)
        assert proof.consumed_at
        assert code not in proof.code_hash and code.replace("-", "") not in proof.code_hash
    assert (await browser.get("/api/v1/web/session")).status_code == 200
    assert (await browser.get("/api/v1/admin/users")).status_code == (
        200 if username == "root" else 403
    )


async def test_technician_vpn_keeps_ordinary_duration_and_group_scope(
    access, browser, monkeypatch, no_email
):
    monkeypatch.setattr(settings, "PORTAL_SESSION_MINUTES", 720)
    monkeypatch.setattr(settings, "VPN_SESSION_MINUTES", 180)
    _, code = await emit(access, "tech_a")
    logged = await sign_in(browser, code, "tech_a")
    assert logged.status_code == 200
    browser.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
    foreign = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["B"].id)}
    )
    assert foreign.status_code == 404
    started = utc_now()
    opened = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert opened.status_code == 200, opened.text
    async with access.db() as db:
        session = await db.get(Session, UUID(opened.json()["session_id"]))
        assert session.web_session_id and session.technician_id == access.users["tech_a"].id
        assert started + timedelta(minutes=179) <= session.expires_at
        assert session.expires_at <= utc_now() + timedelta(minutes=180)
    assert no_email == []


async def test_api_proof_retains_normal_jwt_duration_and_revocation(access, monkeypatch, no_email):
    monkeypatch.setattr(settings, "JWT_EXPIRE_MINUTES", 30)
    _, code = await emit(access)
    logged = await sign_in(access.client, code, portal=False)
    assert logged.status_code == 200, logged.text
    token = logged.json()["access_token"]
    claims = verify_token(token)
    assert claims["second_factor_method"] == "console"
    assert claims["email_verified"] is False
    assert 1799 <= claims["exp"] - claims["iat"] <= 1801
    headers = {"Authorization": "Bearer " + token}
    assert (await access.client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        assert user.email_verified_at is None
        user.token_version += 1
        await db.commit()
    assert (await access.client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    assert no_email == []


async def test_password_is_still_required_and_successful_code_cannot_be_replayed(access, browser):
    identifier, code = await emit(access)
    assert (await sign_in(browser, code, password="wrong-password")).status_code == 401
    async with access.db() as db:
        assert (await db.get(ConsoleAccessCode, identifier)).consumed_at is None
        assert await db.scalar(select(func.count()).select_from(WebSession)) == 0
    assert (await sign_in(browser, code)).status_code == 200
    assert (await sign_in(browser, code)).status_code == 400
    async with access.db() as db:
        assert await db.scalar(select(func.count()).select_from(WebSession)) == 1


async def test_same_code_grants_only_one_concurrent_login(access):
    _, code = await emit(access)
    responses = await asyncio.gather(
        sign_in(access.client, code, portal=False),
        sign_in(access.client, code, portal=False),
    )
    assert sorted(result.status_code for result in responses) == [200, 400]


async def test_password_change_preserves_console_proof_and_revokes_pending_codes(
    access, browser, no_email
):
    _, code = await emit(access)
    logged = await sign_in(browser, code)
    assert logged.status_code == 200
    browser.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
    _, pending = await emit(access)
    changed = await browser.post(
        "/api/v1/web/password",
        json={"current_password": PASSWORD, "new_password": "changed-console-password"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["user"]["email_verified"] is False
    assert (await browser.get("/api/v1/web/session")).status_code == 200
    async with access.db() as db:
        row = await db.scalar(select(WebSession).where(WebSession.revoked_at.is_(None)))
        assert row.second_factor_method == "console" and row.second_factor_at
    invalidated = await sign_in(
        access.client, pending, password="changed-console-password", portal=False
    )
    assert invalidated.status_code == 400
    assert no_email == []


async def test_account_and_purpose_are_not_interchangeable(access, browser):
    _, root_code = await emit(access)
    assert (await sign_in(browser, root_code, "tech_a")).status_code == 400
    assert (await sign_in(browser, root_code)).status_code == 200
    _, confirmation = await emit(access, purpose="confirm")
    assert (await sign_in(browser, confirmation)).status_code == 400


async def test_expiry_and_five_attempt_budget_are_durable(access, browser):
    identifier, expired = await emit(access)
    async with access.db() as db:
        row = await db.get(ConsoleAccessCode, identifier)
        row.expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    assert (await sign_in(browser, expired)).status_code == 400
    identifier, code = await emit(access)
    wrong = "000000-000000-000000-000000"
    if wrong == code:
        wrong = "111111-111111-111111-111111"
    for count in range(1, 6):
        assert (await sign_in(browser, wrong)).status_code == 400
        async with access.db() as db:
            row = await db.get(ConsoleAccessCode, identifier)
            assert row.attempts == count
    assert (await sign_in(browser, code)).status_code == 400
    async with access.db() as db:
        assert not await db.scalar(select(WebSession))


async def test_replacement_invalidates_pending_code_without_revoking_current_login(access, browser):
    _, first = await emit(access)
    logged = await sign_in(browser, first)
    assert logged.status_code == 200
    before = browser.cookies[cookie_name()]
    old_id, old_code = await emit(access)
    _, current_code = await emit(access)
    async with access.db() as db:
        assert (await db.get(ConsoleAccessCode, old_id)).consumed_at
    assert (await browser.get("/api/v1/web/session")).status_code == 200
    assert browser.cookies[cookie_name()] == before
    assert (await sign_in(access.client, old_code, portal=False)).status_code == 400
    assert (await sign_in(access.client, current_code, portal=False)).status_code == 200


@pytest.mark.parametrize("change", ["version", "disabled", "sso"])
async def test_account_revocation_invalidates_an_issued_code(access, browser, change):
    _, code = await emit(access)
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        if change == "version":
            user.token_version += 1
        elif change == "disabled":
            user.is_active = False
        else:
            user.auth_mode, user.password_hash = "sso", None
        await db.commit()
    assert (await sign_in(browser, code)).status_code in (400, 401)
    async with access.db() as db:
        assert not await db.scalar(select(WebSession))


@pytest.mark.parametrize("change", ["disabled", "sso"])
async def test_console_cannot_issue_for_an_ineligible_account(access, change):
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        if change == "disabled":
            user.is_active = False
        else:
            user.auth_mode, user.password_hash = "sso", None
        await db.commit()
    with pytest.raises((ValueError, HTTPException)):
        await emit(access)
    async with access.db() as db:
        assert not await db.scalar(select(ConsoleAccessCode))


async def test_emergency_path_is_explicit_and_never_disables_normal_email(access, browser, no_email):
    identifier, code = await emit(access)
    normal = await browser.post(
        "/api/v1/web/login", json={"username": "root", "password": PASSWORD}
    )
    assert normal.status_code == 503
    async with access.db() as db:
        assert (await db.get(ConsoleAccessCode, identifier)).consumed_at is None
    assert (await sign_in(browser, code)).status_code == 200
    normal = await access.client.post(
        "/api/v1/auth/login", json={"username": "root", "password": PASSWORD}
    )
    assert normal.status_code == 503


@pytest.mark.parametrize("endpoint", ["/web/email/confirm", "/backups/reauthenticate"])
async def test_confirmation_reuses_browser_without_extending_login_or_closing_vpn(
    access, browser, no_email, endpoint
):
    _, login_code = await emit(access)
    logged = await sign_in(browser, login_code)
    assert logged.status_code == 200
    browser.headers["X-CSRF-Token"] = logged.json()["csrf_token"]
    cookie = browser.cookies[cookie_name()]
    opened = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert opened.status_code == 200, opened.text
    session_id = UUID(opened.json()["session_id"])
    async with access.db() as db:
        row = await db.scalar(select(WebSession))
        identifier, expiration = row.id, row.expires_at
        row.reauthenticated_at = row.second_factor_at = utc_now() - timedelta(minutes=6)
        await db.commit()
    _, wrong_purpose = await emit(access)
    rejected = await browser.post(
        "/api/v1" + endpoint, json={"password": PASSWORD, "emergency_code": wrong_purpose}
    )
    assert rejected.status_code == 400
    _, code = await emit(access, purpose="confirm")
    response = await browser.post(
        "/api/v1" + endpoint, json={"password": PASSWORD, "emergency_code": code}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"completed": True}
    assert browser.cookies[cookie_name()] == cookie
    async with access.db() as db:
        row = await db.get(WebSession, identifier)
        user = await db.get(User, access.users["root"].id)
        assert row.expires_at == expiration and row.revoked_at is None
        assert row.second_factor_method == "console"
        assert fresh_authentication(row, user)
        assert user.email_verified_at is None
        session = await db.get(Session, session_id)
        assert session.status == "REQUESTED" and session.ended_at is None
    assert no_email == []
    assert (
        await browser.post(
            "/api/v1" + endpoint, json={"password": PASSWORD, "emergency_code": code}
        )
    ).status_code == 400


async def test_console_events_are_audited_without_disclosing_the_code(access, browser):
    _, code = await emit(access)
    wrong = ("a" if code[0].lower() != "a" else "b") + code[1:]
    assert (await sign_in(browser, wrong)).status_code == 400
    assert (await sign_in(browser, code)).status_code == 200
    async with access.db() as db:
        rows = (
            await db.scalars(select(AuditLog).where(AuditLog.event_type.like("console_%")))
        ).all()
        assert len(rows) >= 3
        details = json.dumps([row.details for row in rows], default=str)
        assert "test-operator" in details and "SMTP outage" in details
        assert code not in details and code.replace("-", "") not in details
        assert {row.user_id for row in rows} <= {None, access.users["root"].id}


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://evil.test"}, {"X-FactoryVPN-Portal": ""}]
)
async def test_console_login_retains_portal_origin_and_header_checks(access, browser, headers):
    identifier, code = await emit(access)
    response = await browser.post(
        "/api/v1/web/login",
        headers=headers,
        json={"username": "root", "password": PASSWORD, "emergency_code": code},
    )
    assert response.status_code == 403
    async with access.db() as db:
        assert (await db.get(ConsoleAccessCode, identifier)).consumed_at is None
        assert not await db.scalar(select(WebSession))


async def test_restore_discards_pending_console_proofs(access, browser):
    from scripts.backup_database import sanitize, security_state

    _, login_code = await emit(access)
    await emit(access, purpose="confirm")
    async with access.db() as db:
        state = await security_state(db)
        await sanitize(db, state)
        assert await db.scalar(select(func.count()).select_from(ConsoleAccessCode)) == 0
    assert (await sign_in(browser, login_code)).status_code == 400
