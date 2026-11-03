"""Recovery uses real authentication, group scopes, and security data in PostgreSQL."""

from datetime import timedelta
from types import SimpleNamespace
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from api.config import settings
from api.models import User, UserGroup, WebSession, VPNCertificate, GatewayCredential, Gateway
from api.services.auth_service import hash_password
from api.time_utils import utc_now
from factoryvpn_common.backup_catalog import Catalog, now
from scripts.backup_database import security_state, sanitize, authorized
from test_provisioning_postgres import (
    access as access_fixture,
    browser as browser_fixture,
    encoded_password as password_fixture,
    login,
)
from test_sso_postgres import provider as provider_fixture, enable
from test_web_portal_postgres import sso_login

access, browser, encoded_password = access_fixture, browser_fixture, password_fixture
provider = provider_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


@pytest.fixture
def control(tmp_path, monkeypatch):
    catalog = Catalog(tmp_path / "control")
    catalog.initialize()
    archives = tmp_path / "archives"
    archives.mkdir()
    monkeypatch.setattr(settings, "BACKUP_CONTROL_DIR", str(catalog.root))
    monkeypatch.setattr(settings, "BACKUP_ARCHIVE_DIR", str(archives))
    catalog.meta("heartbeat", now())
    return catalog


@pytest.mark.parametrize("username", ["admin_a", "admin_b", "tech_a", "shared", "outsider"])
async def test_backups_are_global_only_including_direct_requests(
    access, browser, control, username
):
    await login(browser, username)
    for method, path, data in [
        ("GET", "/backups", None),
        ("POST", "/backups", None),
        ("POST", f"/backups/{uuid4()}/commands", {"action": "verify"}),
        ("POST", f"/backups/{uuid4()}/download", None),
        ("PUT", "/backups/retention", {"count": 2}),
    ]:
        response = await browser.request(
            method, "/api/v1" + path, json=data, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 403, response.text
    assert control.jobs() == []


async def test_fresh_browser_access_idempotency_and_csrf(access, browser, control):
    await login(browser, "root")
    response = await browser.get("/api/v1/backups")
    assert response.status_code == 200 and response.json()["enabled"]
    key = {"Idempotency-Key": str(uuid4())}
    first = await browser.post("/api/v1/backups", headers=key)
    second = await browser.post("/api/v1/backups", headers=key)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert "web_session_id" not in first.text
    assert (await access.call("GET", "/backups")).status_code == 401
    async with access.db() as db:
        row = await db.scalar(
            select(WebSession).where(WebSession.user_id == access.users["root"].id)
        )
        row.reauthenticated_at = utc_now() - timedelta(minutes=6)
        await db.commit()
    response = await browser.post("/api/v1/backups", headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 428
    rejected = await browser.post(
        "/api/v1/backups/reauthenticate", json={"password": "incorrect-password"}
    )
    assert rejected.status_code == 401
    assert (await browser.get("/api/v1/backups")).json()["needs_reauthentication"]
    challenge = await browser.post(
        "/api/v1/backups/reauthenticate", json={"password": "test-password-for-users"}
    )
    assert challenge.status_code == 200
    assert (await browser.get("/api/v1/backups")).json()["needs_reauthentication"]
    from email_helpers import last_code

    verified = await browser.post(
        "/api/v1/web/email/verify",
        json={
            "challenge_token": challenge.json()["challenge_token"],
            "code": last_code(),
        },
    )
    assert verified.status_code == 200
    assert not (await browser.get("/api/v1/backups")).json()["needs_reauthentication"]
    response = await browser.post("/api/v1/backups", headers={**key, "X-CSRF-Token": "wrong"})
    assert response.status_code == 403


async def test_worker_rechecks_admin_after_queue(access, browser, control):
    await login(browser, "root")
    result = await browser.post("/api/v1/backups", headers={"Idempotency-Key": str(uuid4())})
    assert result.status_code == 202
    claim = json.loads(control.jobs()[0]["actor"])
    async with access.db() as db:
        await authorized(db, claim)
        root = await db.get(User, access.users["root"].id)
        root.is_admin = False
        await db.commit()
        with pytest.raises(ValueError, match="authorization_expired"):
            await authorized(db, claim)


async def test_maintenance_blocks_new_access_but_keeps_cleanup_available(access, browser, control):
    await login(browser, "root")
    control.meta("maintenance", str(uuid4()))
    assert (await browser.get("/api/v1/backups")).status_code == 503
    assert (await access.call("GET", "/gateways/")).status_code == 503
    response = await access.client.post(
        "/api/v1/internal/vpn/authorize", headers=access.worker, json={}
    )
    assert response.status_code == 503
    response = await access.client.get("/api/v1/internal/sessions/work", headers=access.worker)
    assert response.status_code == 200
    control.meta("maintenance", "")


async def test_upload_limit_confirmation_and_private_metadata(
    access, browser, control, monkeypatch
):
    await login(browser, "root")
    monkeypatch.setattr(settings, "BACKUP_MAX_BYTES", 16)
    result = await browser.post(
        "/api/v1/backups/import",
        content=b"x" * 17,
        headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/octet-stream"},
    )
    assert result.status_code == 413
    identifier = str(uuid4())
    control.add_backup(identifier, 16, "private-hash", {"secret": "must-not-leak"})
    result = await browser.get("/api/v1/backups")
    assert "must-not-leak" not in result.text and "private-hash" not in result.text
    result = await browser.post(
        f"/api/v1/backups/{identifier}/commands",
        json={"action": "restore"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert result.status_code == 422


async def test_upload_requires_free_space_before_reserving_a_job(
    access, browser, control, monkeypatch
):
    await login(browser, "root")
    monkeypatch.setattr("api.routers.backups.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    result = await browser.post(
        "/api/v1/backups/import",
        content=b"encrypted archive",
        headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/octet-stream"},
    )
    assert result.status_code == 507
    assert not control.jobs() and not list((control.root / "imports").iterdir())


async def test_duplicate_upload_cannot_replace_the_original(access, browser, control):
    await login(browser, "root")
    headers = {"Idempotency-Key": str(uuid4()), "Content-Type": "application/octet-stream"}
    result = await browser.post("/api/v1/backups/import", content=b"original", headers=headers)
    assert result.status_code == 202
    path = control.root / "imports" / (result.json()["id"] + ".upload")
    duplicate = await browser.post(
        "/api/v1/backups/import", content=b"replacement", headers=headers
    )
    assert duplicate.status_code == 409
    assert path.read_bytes() == b"original"
    assert control.jobs()[0]["state"] == "queued"


async def test_revocation_during_upload_discards_the_file(access, browser, control):
    await login(browser, "root")

    async def body():
        yield b"first chunk"
        async with access.db() as db:
            root = await db.get(User, access.users["root"].id)
            root.is_admin = False
            await db.commit()
        yield b"last chunk"

    result = await browser.post(
        "/api/v1/backups/import",
        content=body(),
        headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/octet-stream"},
    )
    assert result.status_code == 403
    assert control.jobs()[0]["state"] == "failed"
    assert not list((control.root / "imports").iterdir())


async def test_cancelled_upload_cannot_be_requeued_by_late_request(access, browser, control):
    await login(browser, "root")

    async def body():
        yield b"first chunk"
        control.update_job(control.jobs()[0]["id"], state="failed", error="upload_interrupted")
        yield b"late chunk"

    result = await browser.post(
        "/api/v1/backups/import",
        content=body(),
        headers={"Idempotency-Key": str(uuid4()), "Content-Type": "application/octet-stream"},
    )
    assert result.status_code == 409
    assert control.jobs()[0]["state"] == "failed"
    assert not list((control.root / "imports").iterdir())


@pytest.mark.parametrize("assignment", ["B", "missing_group", "deleted", "unassigned"])
async def test_restore_preserves_current_gateway_segregation(access, control, assignment):
    gateway_id = access.gateways["A"].id
    expected = access.groups["B"].id if assignment == "B" else None
    async with access.db() as db:
        security = await security_state(db)
        if assignment == "deleted":
            del security["gateways"][str(gateway_id)]
        else:
            security["gateways"][str(gateway_id)] = (
                str(expected)
                if expected
                else str(uuid4()) if assignment == "missing_group" else None
            )
        # The loaded archive still puts this gateway, its inventory and active keys in A.
        await sanitize(db, security, actor_id=str(access.users["root"].id))
        gateway = await db.get(Gateway, gateway_id)
        assert gateway.group_id == expected
        assert not (
            await db.scalars(
                select(GatewayCredential).where(
                    GatewayCredential.gateway_id == gateway_id, GatewayCredential.active.is_(True)
                )
            )
        ).all()
        assert not (
            await db.scalars(
                select(VPNCertificate).where(
                    VPNCertificate.peer_id == gateway_id, VPNCertificate.state == "active"
                )
            )
        ).all()
    for actor, visible in (("admin_a", False), ("admin_b", expected is not None), ("root", True)):
        await access.login(actor)
        for path in (f"/admin/gateways/{gateway_id}", f"/devices/gateway/{gateway_id}"):
            assert (await access.call("GET", path, actor)).status_code == (200 if visible else 404)


async def test_logout_interrupts_backup_download(access, browser, control, monkeypatch):
    from api.routers import backups
    from factoryvpn_common.backup_archive import CHUNK

    await login(browser, "root")
    identifier = str(uuid4())
    data = b"a" * (CHUNK * 2 + 11)
    (Path(settings.BACKUP_ARCHIVE_DIR) / (identifier + ".fvbackup")).write_bytes(data)
    control.add_backup(identifier, len(data), "test-hash", {})
    original, calls = backups.valid_web_session, 0

    async def revoke_between_chunks(db, session_id, actor):
        nonlocal calls
        calls += 1
        if calls == 2:
            async with access.db() as changed:
                session = await changed.get(WebSession, session_id)
                session.revoked_at = utc_now()
                await changed.commit()
        return await original(db, session_id, actor)

    monkeypatch.setattr(backups, "valid_web_session", revoke_between_chunks)
    result = await browser.post(f"/api/v1/backups/{identifier}/download")
    assert result.content == data[:CHUNK]
    assert int(result.headers["content-length"]) > len(result.content)


async def test_restore_cannot_revive_deleted_accounts_credentials_or_group_access(
    access, browser, control
):
    await login(browser, "root")
    tech_id = access.users["tech_a"].id
    other_id = access.users["tech_b"].id
    gateway_id = access.gateways["A"].id
    async with access.db() as db:
        # Save old authority as represented by a backup, then make security changes.
        old_user = await db.get(User, tech_id)
        old_password = old_user.password_hash
        new_password = hash_password("changed-after-backup")
        old_user.password_hash, old_user.permissions = new_password, []
        await db.execute(delete(UserGroup).where(UserGroup.user_id == tech_id))
        other = await db.get(User, other_id)
        other.is_active = False
        cert = await db.scalar(select(VPNCertificate).where(VPNCertificate.peer_id == tech_id))
        cert.state, cert.revoked_at = "revoked", utc_now()
        credential = await db.scalar(
            select(GatewayCredential).where(GatewayCredential.gateway_id == gateway_id)
        )
        credential.active, credential.revoked_at = False, utc_now()
        await db.commit()
        security = await security_state(db)
        # Simulate data restored from the older backup.
        old_user.password_hash, old_user.permissions = old_password, ["connect"]
        other.is_active = True
        db.add(UserGroup(user_id=tech_id, group_id=access.groups["A"].id))
        cert.state, cert.revoked_at = "active", None
        credential.active, credential.revoked_at = True, None
        await db.commit()
        await sanitize(db, security, actor_id=str(access.users["root"].id))
        await db.refresh(old_user)
        await db.refresh(other)
        await db.refresh(cert)
        await db.refresh(credential)
        assert old_user.password_hash == new_password and old_user.permissions == []
        assert not other.is_active and cert.state == "revoked" and not credential.active
        assert not (await db.scalars(select(UserGroup).where(UserGroup.user_id == tech_id))).all()
        assert not (await db.scalars(select(WebSession))).all()
    assert (await browser.get("/api/v1/backups")).status_code == 401


async def test_disaster_recovery_disables_old_authority(access, control):
    async with access.db() as db:
        await sanitize(
            db, {}, recovery={"username": "recovery-admin", "password": "new-recovery-password"}
        )
        users = (await db.scalars(select(User))).all()
        assert [user.username for user in users if user.is_active] == ["recovery-admin"]
        assert not (
            await db.scalars(select(GatewayCredential).where(GatewayCredential.active.is_(True)))
        ).all()
        assert not (
            await db.scalars(select(VPNCertificate).where(VPNCertificate.state == "active"))
        ).all()


@pytest.mark.parametrize("auth_age", [None, 600, 0])
async def test_sso_step_up_requires_new_signed_authentication(
    access, browser, control, provider, auth_age
):
    from urllib.parse import parse_qs, urlsplit

    # Keep the local root for emergency access; elevate an existing SSO test account.
    await enable(access, provider)
    async with access.db() as db:
        user = await db.get(User, access.users["tech_a"].id)
        user.is_admin = True
        await db.commit()
    await sso_login(browser, provider)
    original_cookies = dict(browser.cookies)
    session = (await browser.get("/api/v1/web/session")).json()
    browser.headers["X-CSRF-Token"] = session["csrf_token"]
    assert (await browser.get("/api/v1/backups")).json()["needs_reauthentication"]
    start = await browser.post(
        "/api/v1/web/sso/start", json={"provider_id": "cognito", "reauthenticate": True}
    )
    assert start.status_code == 200
    redirect = await browser.get(start.json()["authorization_url"])
    values = parse_qs(urlsplit(redirect.headers["location"]).query)
    assert values["prompt"] == ["login"] and values["max_age"] == ["0"]
    provider.nonce, provider.challenge = values["nonce"][0], values["code_challenge"][0]
    if auth_age is not None:
        provider.claims["auth_time"] = int(utc_now().timestamp()) - auth_age
    result = await browser.get(
        "/api/v1/auth/sso/cognito/callback",
        params={"state": values["state"][0], "code": "test-code"},
    )
    if auth_age == 0:
        assert result.headers["location"] == "/app/backups"
        assert all(browser.cookies.get(key) == value for key, value in original_cookies.items())
        assert not (await browser.get("/api/v1/backups")).json()["needs_reauthentication"]
    else:
        assert result.headers["location"] == "/app/login?error=sso"
        assert (await browser.get("/api/v1/backups")).json()["needs_reauthentication"]


async def test_restore_preserves_live_email_policy_and_discards_pending_codes(
    access, browser, control
):
    from api.models import EmailChallenge

    await login(browser, "root")
    pending = await browser.post(
        "/api/v1/web/email/confirm", json={"password": "test-password-for-users"}
    )
    assert pending.status_code == 200
    async with access.db() as db:
        user = await db.get(User, access.users["root"].id)
        user.email_two_step = True
        user.email = "current@example.test"
        verified = user.email_verified_at
        await db.commit()
        current = await security_state(db)
        user.email, user.email_verified_at, user.email_two_step = "old@example.test", None, False
        await db.commit()
        await sanitize(db, current, actor_id=str(user.id))
        await db.refresh(user)
        assert (
            user.email == "current@example.test"
            and user.email_verified_at == verified
            and user.email_two_step
        )
        assert not (await db.scalars(select(EmailChallenge))).all()
