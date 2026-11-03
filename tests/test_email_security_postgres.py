"""Email second-step proofs through real PostgreSQL, HTTP cookies and Bearer tokens."""

import asyncio
import os
from datetime import timedelta
import pytest
from sqlalchemy import select
from api.models import EmailChallenge, User, WebSession
from api.services import email_security as security, mail_service
from api.services.auth_service import issue_access_token
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
    PASSWORD,
)
from test_web_portal_postgres import browser as browser_fixture, login
from email_helpers import last_code, messages

access = access_fixture
encoded_password = password_fixture
browser = browser_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


async def begin(client, username="root", portal=True):
    return await client.post(
        "/api/v1/" + ("web" if portal else "auth") + "/login",
        json={"username": username, "password": PASSWORD},
    )


def proof(result, code=None):
    return {"challenge_token": result.json()["challenge_token"], "code": code or last_code()}


@pytest.mark.parametrize("username", ["root", "admin_a", "limited"])
async def test_privileged_login_requires_code_without_granting_access(access, browser, username):
    result = await begin(browser, username)
    assert result.status_code == 200
    assert result.json()["challenge_required"] is True
    assert "access_token" not in result.json()
    assert (await browser.get("/api/v1/web/session")).status_code == 401
    async with access.db() as db:
        assert not await db.scalar(select(WebSession))
        row = await db.scalar(select(EmailChallenge))
        assert (
            last_code() not in row.code_hash and result.json()["challenge_token"] != row.secret_hash
        )
    verified = await browser.post("/api/v1/web/login/verify", json=proof(result))
    assert verified.status_code == 200
    assert verified.json()["user"]["email_verified"]
    assert (await browser.post("/api/v1/web/login/verify", json=proof(result))).status_code == 400


async def test_api_channel_and_browser_binding_cannot_be_swapped(access, browser):
    result = await begin(browser)
    assert (
        await access.client.post("/api/v1/auth/login/verify", json=proof(result))
    ).status_code == 400
    cookies = dict(browser.cookies)
    browser.cookies.clear()
    assert (await browser.post("/api/v1/web/login/verify", json=proof(result))).status_code == 400
    browser.cookies.update(cookies)
    assert (await browser.post("/api/v1/web/login/verify", json=proof(result))).status_code == 200
    result = await begin(access.client, portal=False)
    responses = await asyncio.gather(
        *[access.client.post("/api/v1/auth/login/verify", json=proof(result)) for _ in range(2)]
    )
    assert sorted(r.status_code for r in responses) == [200, 400]
    token = next(r.json()["access_token"] for r in responses if r.status_code == 200)
    assert (
        await access.client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + token})
    ).status_code == 200
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        unverified = issue_access_token(user)
    assert (
        await access.client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer " + unverified}
        )
    ).status_code == 401


async def test_wrong_attempts_resend_and_aggregate_lockout_are_durable(access, browser):
    for batch in range(2):
        result = await begin(browser)
        correct = last_code()
        wrong = "000000" if correct != "000000" else "111111"
        for _ in range(5):
            bad = await browser.post("/api/v1/web/login/verify", json=proof(result, wrong))
            assert bad.status_code == 400
        assert (
            await browser.post("/api/v1/web/login/verify", json=proof(result, correct))
        ).status_code == 400
        async with access.db() as db:
            user = await db.get(User, access.users["root"].id)
            user.email_last_sent_at = utc_now() - timedelta(seconds=61)
            await db.commit()
    assert (await begin(browser)).status_code == 429
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        user.email_locked_until = None
        await db.commit()
    result = await begin(browser)
    wrong = "000000" if last_code() != "000000" else "111111"
    await browser.post("/api/v1/web/login/verify", json=proof(result, wrong))
    assert (
        await browser.post(
            "/api/v1/web/login/resend", json={"challenge_token": result.json()["challenge_token"]}
        )
    ).status_code == 429
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        user.email_last_sent_at = utc_now() - timedelta(seconds=61)
        row = await db.scalar(
            select(EmailChallenge).where(
                EmailChallenge.secret_hash == security.digest(result.json()["challenge_token"])
            )
        )
        old_hash, expiry = row.code_hash, row.expires_at
        await db.commit()
    resent = await browser.post(
        "/api/v1/web/login/resend", json={"challenge_token": result.json()["challenge_token"]}
    )
    assert resent.status_code == 200
    async with access.db() as db:
        row = await db.scalar(
            select(EmailChallenge).where(
                EmailChallenge.secret_hash == security.digest(result.json()["challenge_token"])
            )
        )
        assert row.attempts == 1 and row.expires_at == expiry and row.code_hash != old_hash
        row.expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    assert (await browser.post("/api/v1/web/login/verify", json=proof(resent))).status_code == 400


async def test_delivery_failure_and_revocation_never_grant_access(access, browser, monkeypatch):
    async def fail(*args):
        raise OSError("private SMTP diagnostic")

    monkeypatch.setattr(mail_service, "send_code", fail)
    result = await begin(browser)
    assert result.status_code == 503 and "private" not in result.text
    async with access.db() as db:
        row = await db.scalar(select(EmailChallenge))
        assert row.consumed_at
        assert not await db.scalar(select(WebSession))
        user = await db.get(User, access.users["root"].id)
        user.email_last_sent_at = None
        await db.commit()
    from email_helpers import capture

    monkeypatch.setattr(mail_service, "send_code", capture)
    result = await begin(browser)
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        user.token_version += 1
        await db.commit()
    assert (await browser.post("/api/v1/web/login/verify", json=proof(result))).status_code == 400


async def test_optional_enable_old_new_email_and_disable(access, browser):
    logged = await login(browser, "tech_a")
    old_token = access.headers["tech_a"]
    start = await browser.post(
        "/api/v1/web/email/start", json={"action": "enable", "password": PASSWORD}
    )
    assert start.status_code == 200
    verified = await browser.post("/api/v1/web/email/verify", json=proof(start))
    assert verified.status_code == 200 and verified.json()["user"]["email_two_step"]
    browser.headers["X-CSRF-Token"] = verified.json()["csrf_token"]
    assert (await access.client.get("/api/v1/auth/me", headers=old_token)).status_code == 401
    direct = await browser.patch(
        "/api/v1/auth/me",
        headers={"If-Match": '"' + verified.json()["user"]["revision"] + '"'},
        json={"email": "bypass@example.test"},
    )
    assert direct.status_code == 409
    start = await browser.post(
        "/api/v1/web/email/start",
        json={"action": "change", "email": "new@example.test", "password": PASSWORD},
    )
    assert messages[-1]["recipient"] == "tech_a@example.test"
    next_step = await browser.post("/api/v1/web/email/verify", json=proof(start))
    assert (
        next_step.json()["challenge_required"] and messages[-1]["recipient"] == "new@example.test"
    )
    async with access.db() as db:
        assert (await db.get(User, access.users["tech_a"].id)).email == "tech_a@example.test"
    verified = await browser.post("/api/v1/web/email/verify", json=proof(next_step))
    assert verified.json()["user"]["email"] == "new@example.test"
    browser.headers["X-CSRF-Token"] = verified.json()["csrf_token"]
    start = await browser.post(
        "/api/v1/web/email/start", json={"action": "disable", "password": PASSWORD}
    )
    verified = await browser.post("/api/v1/web/email/verify", json=proof(start))
    assert verified.status_code == 200 and not verified.json()["user"]["email_two_step"]
    assert logged.json()["user"]["email_two_step"] is False


async def test_admin_disable_forbidden_and_recovery_scoped_fresh(access, browser):
    await login(browser, "admin_a")
    assert (
        await browser.post(
            "/api/v1/web/email/start", json={"action": "disable", "password": PASSWORD}
        )
    ).status_code == 403
    path = f"/api/v1/admin/users/{access.users['tech_a'].id}/email-recovery"
    assert (await browser.post(path, json={"email": "recovered@example.test"})).status_code == 403
    await login(browser)
    user = (await browser.get(f"/api/v1/admin/users/{access.users['tech_a'].id}")).json()
    assert (await browser.post(path, json={"email": "recovered@example.test"})).status_code == 428
    response = await browser.post(
        path,
        headers={"If-Match": '"' + user["revision"] + '"'},
        json={"email": "recovered@example.test"},
    )
    assert response.status_code == 200
    async with access.db() as db:
        target = await db.get(User, access.users["tech_a"].id)
        assert (
            target.email == "recovered@example.test"
            and target.email_two_step
            and not target.email_verified_at
        )
        root = await db.get(User, access.users["root"].id)
        sessions = (await db.scalars(select(WebSession).where(WebSession.user_id == root.id))).all()
        for session in sessions:
            session.reauthenticated_at = session.second_factor_at = utc_now() - timedelta(minutes=6)
        await db.commit()
    assert (
        await browser.post(
            path,
            headers={"If-Match": '"' + user["revision"] + '"'},
            json={"email": "other@example.test"},
        )
    ).status_code == 428
