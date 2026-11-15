"""Real DB/API authentication and signed OIDC tokens; only provider HTTP is simulated."""

import asyncio
import os
import secrets
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from cryptography.fernet import Fernet
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import select

from api.config import settings
from api.models.sso import ExternalIdentity, SSOSession
from api.models.session import Session
from api.models.user import User
from api.routers.sso import pkce
from api.routers import sso as sso_router
from api.services import oidc
from api.services.sso_service import maintenance_pass, decrypt
from api.services.auth_service import issue_access_token
from api.sso_config import OIDCProvider
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
    WORKER,
    PASSWORD,
)  # noqa: F401

access = access_fixture
encoded_password = password_fixture

pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


@pytest.fixture
def provider(monkeypatch):
    key = RSAKey.generate_key(2048, parameters={"kid": "first"})
    p = SimpleNamespace(
        key=key,
        claims={},
        revoked=False,
        unavailable=False,
        subject="stable-cognito-user",
        userinfo_subject=None,
        nonce=None,
        challenge=None,
        requests=[],
        revoke_count=0,
        expires_in=3600,
    )
    config = OIDCProvider(
        kind="cognito",
        label="Cognito test",
        issuer="https://idp.test/pool",
        client_id="factoryvpn-test",
        client_secret="test-secret",
        logout_endpoint="https://idp.test/logout",
    )
    monkeypatch.setattr(settings, "SSO_PROVIDERS", {"cognito": config})
    monkeypatch.setattr(settings, "SSO_PUBLIC_URL", "https://test")
    monkeypatch.setattr(
        settings, "SSO_TOKEN_KEY", type(settings.SSO_TOKEN_KEY)(Fernet.generate_key().decode())
    )
    oidc.OIDCAdapter.metadata_cache.clear()
    oidc.OIDCAdapter.jwks_cache.clear()

    def respond(request):
        p.requests.append(request)
        path = request.url.path
        if p.unavailable:
            return httpx.Response(503)
        if path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": config.issuer,
                    "authorization_endpoint": "https://idp.test/authorize",
                    "token_endpoint": "https://idp.test/token",
                    "userinfo_endpoint": "https://idp.test/userinfo",
                    "jwks_uri": "https://idp.test/keys",
                    "revocation_endpoint": "https://idp.test/revoke",
                },
            )
        if path == "/keys":
            return httpx.Response(200, json={"keys": [p.key.as_dict(private=False)]})
        if path == "/token":
            body = parse_qs(request.content.decode())
            assert pkce(body["code_verifier"][0]) == p.challenge
            if config.token_endpoint_auth_method == "client_secret_post":
                assert "authorization" not in request.headers
                assert body["client_id"] == [config.client_id]
                assert body["client_secret"] == [config.client_secret.get_secret_value()]
            else:
                assert request.headers["authorization"].startswith("Basic ")
            claims = {
                "iss": config.issuer,
                "sub": p.subject,
                "aud": config.client_id,
                "iat": int(utc_now().timestamp()),
                "exp": int(utc_now().timestamp()) + 3600,
                "nonce": p.nonce,
                "token_use": "id",
                "amr": ["pwd", "mfa"],
                "email": "ignored@example.test",
                "cognito:groups": ["administrators"],
                **p.claims,
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "provider-access-secret",
                    "refresh_token": "provider-refresh-secret",
                    "token_type": "Bearer",
                    "expires_in": p.expires_in,
                    "id_token": jwt.encode(
                        {"alg": "RS256", "kid": p.key.kid}, claims, getattr(p, "signing_key", p.key)
                    ),
                },
            )
        if path == "/userinfo":
            assert request.headers["authorization"] == "Bearer provider-access-secret"
            if p.revoked:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"sub": p.userinfo_subject or p.subject})
        if path == "/revoke":
            p.revoke_count += 1
            return httpx.Response(200)
        raise AssertionError(path)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(oidc, "http_client", lambda: httpx.AsyncClient(transport=transport))
    original = oidc.oauth_client

    def oauth(config, redirect_uri):
        client = original(config, redirect_uri)
        client._transport = transport
        return client

    monkeypatch.setattr(oidc, "oauth_client", oauth)
    return p


async def enable(a, provider, name="tech_a"):
    uid = a.users[name].id
    result = await a.call(
        "POST",
        f"/admin/users/{uid}/identities",
        json={"provider_id": "cognito", "subject": provider.subject},
    )
    assert result.status_code == 201, result.text
    result = await a.call("PUT", f"/admin/users/{uid}/authentication", json={"auth_mode": "sso"})
    assert result.status_code == 200, result.text


async def begin(a, provider):
    verifier = secrets.token_urlsafe(48)
    client_state = secrets.token_urlsafe(24)
    start = await a.client.post(
        "/api/v1/auth/sso/start",
        json={
            "provider_id": "cognito",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "code_challenge": pkce(verifier),
            "state": client_state,
        },
    )
    assert start.status_code == 200, start.text
    assert start.headers["cache-control"] == "no-store"
    assert start.headers["pragma"] == "no-cache"
    authorization = await a.client.get(start.json()["authorization_url"])
    assert authorization.status_code == 302, authorization.text
    values = parse_qs(urlsplit(authorization.headers["location"]).query)
    assert values["code_challenge_method"] == ["S256"]
    provider.nonce = values["nonce"][0]
    provider.challenge = values["code_challenge"][0]
    return SimpleNamespace(
        verifier=verifier,
        state=client_state,
        values=values,
        callback="https://test/api/v1/auth/sso/cognito/callback",
    )


async def finish(a, flow):
    return await a.client.get(
        flow.callback, params={"state": flow.values["state"][0], "code": "provider-code"}
    )


async def sign_in(a, provider):
    flow = await begin(a, provider)
    response = await finish(a, flow)
    assert response.status_code == 302, response.text
    values = parse_qs(urlsplit(response.headers["location"]).query)
    assert values["state"] == [flow.state]
    assert "access_token" not in values
    response = await a.client.post(
        "/api/v1/auth/sso/exchange",
        json={"code": values["code"][0], "code_verifier": flow.verifier},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    a.headers["tech_a"] = {"Authorization": "Bearer " + response.json()["access_token"]}
    return response


async def test_oidc_login_preserves_identity_groups_and_local_emergency_access(access, provider):
    a = access
    await enable(a, provider)
    response = await sign_in(a, provider)
    assert response.json()["user_id"] == str(a.users["tech_a"].id)
    me = await a.call("GET", "/auth/me", "tech_a")
    assert me.status_code == 200
    assert me.json()["auth_mode"] == "sso"
    assert not me.json()["is_admin"]
    assert me.json()["permissions"] == ["connect"]
    assert me.json()["group_ids"] == [str(a.groups["A"].id)]
    assert (await a.call("GET", "/auth/me", "root")).status_code == 200
    assert (await a.call("GET", f'/gateways/{a.gateways["B"].id}', "tech_a")).status_code == 404
    async with a.db() as db:
        row = await db.scalar(select(SSOSession))
        assert "provider-access-secret" not in row.tokens_encrypted
        assert decrypt(row.tokens_encrypted)["access_token"] == "provider-access-secret"
        user = await db.get(User, a.users["tech_a"].id)
        assert user.password_hash is None


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil.test"},
        {"aud": "another-client"},
        {"nonce": "wrong"},
        {"exp": 1},
        {"token_use": "access"},
        {"sub": "unknown"},
        {"azp": "another-client"},
    ],
)
async def test_wrong_identity_claims_rejected(access, provider, claims):
    await enable(access, provider)
    provider.claims = claims
    result = await finish(access, await begin(access, provider))
    assert result.status_code in (401, 403), result.text
    async with access.db() as db:
        assert await db.scalar(select(SSOSession.id)) is None


async def test_userinfo_subject_must_match_id_token(access, provider):
    await enable(access, provider)
    provider.userinfo_subject = "another-user"
    assert (await finish(access, await begin(access, provider))).status_code == 401


async def test_unknown_user_not_linked_by_email_or_created(access, provider):
    provider.claims = {"email": "existing@example.test", "email_verified": True}
    async with access.db() as db:
        user = await db.get(User, access.users["tech_a"].id)
        user.email = "existing@example.test"
        await db.commit()
    assert (await finish(access, await begin(access, provider))).status_code == 403
    async with access.db() as db:
        assert await db.scalar(select(ExternalIdentity.id)) is None


async def test_callback_and_exchange_are_single_use_and_client_bound(access, provider):
    await enable(access, provider)
    flow = await begin(access, provider)
    response = await finish(access, flow)
    assert response.status_code == 302
    assert (await finish(access, flow)).status_code == 400
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    wrong = await access.client.post(
        "/api/v1/auth/sso/exchange", json={"code": code, "code_verifier": "a" * 43}
    )
    assert wrong.status_code == 401
    values = {"code": code, "code_verifier": flow.verifier}
    results = await asyncio.gather(
        *(access.client.post("/api/v1/auth/sso/exchange", json=values) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [200, 401]


async def test_callback_requires_browser_cookie(access, provider):
    await enable(access, provider)
    flow = await begin(access, provider)
    access.client.cookies.clear()
    assert (await finish(access, flow)).status_code == 400


async def test_unapproved_redirect_and_provider_rejected(access, provider):
    payload = {
        "provider_id": "cognito",
        "redirect_uri": "https://evil.test/callback",
        "code_challenge": pkce("a" * 43),
        "state": "a" * 32,
    }
    assert (await access.client.post("/api/v1/auth/sso/start", json=payload)).status_code == 400
    payload.update(provider_id="unknown", redirect_uri="http://127.0.0.1:8765/callback")
    assert (await access.client.post("/api/v1/auth/sso/start", json=payload)).status_code == 404


async def test_sso_cannot_be_bypassed_with_password_or_old_local_token(access, provider):
    await enable(access, provider)
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    result = await access.client.post(
        "/api/v1/auth/login", json={"username": "tech_a", "password": PASSWORD}
    )
    assert result.status_code == 401
    assert (
        await access.call(
            "PUT", f'/admin/users/{access.users["tech_a"].id}', json={"password": "new-password"}
        )
    ).status_code == 409
    await sign_in(access, provider)
    result = await access.call(
        "POST",
        "/auth/password",
        "tech_a",
        json={"current_password": PASSWORD, "new_password": "new-password"},
    )
    assert result.status_code == 409


async def test_delegated_admin_cannot_link_identities_or_change_authentication(access, provider):
    uid = access.users["tech_a"].id
    for path, method, body in [
        ("identities", "POST", {"provider_id": "cognito", "subject": provider.subject}),
        ("authentication", "PUT", {"auth_mode": "sso"}),
    ]:
        result = await access.call(method, f"/admin/users/{uid}/{path}", "admin_a", json=body)
        assert result.status_code == 403


async def test_last_local_admin_cannot_be_converted_to_sso(access, provider):
    uid = access.users["root"].id
    assert (
        await access.call(
            "POST",
            f"/admin/users/{uid}/identities",
            json={"provider_id": "cognito", "subject": provider.subject},
        )
    ).status_code == 201
    assert (
        await access.call("PUT", f"/admin/users/{uid}/authentication", json={"auth_mode": "sso"})
    ).status_code == 409


async def vpn(a):
    result = await a.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(a.gateways["A"].id)}
    )
    assert result.status_code == 200, result.text
    return UUID(result.json()["session_id"])


async def age(a, seconds):
    async with a.db() as db:
        row = await db.scalar(select(SSOSession))
        row.verified_at = utc_now() - timedelta(seconds=seconds)
        await db.commit()


async def test_cognito_revocation_closes_vpn_and_invalidates_api_token(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    identifier = await vpn(access)
    await age(access, 65)
    provider.revoked = True
    await maintenance_pass(access.db)
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    async with access.db() as db:
        row = await db.get(Session, identifier)
        assert row.status == "FAILED" and row.cleaned_at is None
        assert row.cleaned_at is None
        session = await db.scalar(select(SSOSession))
        assert session.revoked_at and session.tokens_encrypted is None
    assert provider.revoke_count == 1


async def test_outage_expires_authorization_without_waiting_for_maintenance(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    identifier = await vpn(access)
    provider.unavailable = True
    await age(access, 65)
    await maintenance_pass(access.db)
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 200
    await age(access, 125)
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    response = await access.client.get(
        "/api/v1/internal/sessions/work", headers={"X-Worker-Key": WORKER}
    )
    assert response.status_code == 200, response.text
    async with access.db() as db:
        assert (await db.get(Session, identifier)).status == "FAILED"


async def test_logout_revokes_locally_and_returns_cognito_browser_logout(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    identifier = await vpn(access)
    result = await access.call("POST", "/auth/logout", "tech_a")
    assert result.status_code == 200, result.text
    assert result.json()["provider_logout_urls"][0].startswith("https://idp.test/logout?client_id=")
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    async with access.db() as db:
        assert (await db.get(Session, identifier)).status == "FAILED"
    await maintenance_pass(access.db)
    assert provider.revoke_count == 1


async def test_key_rotation_refreshes_jwks(access, provider):
    await enable(access, provider)
    await oidc.OIDCAdapter("cognito").jwks()
    provider.key = RSAKey.generate_key(2048, parameters={"kid": "rotated"})
    await sign_in(access, provider)


async def test_signature_from_an_untrusted_key_is_rejected(access, provider):
    await enable(access, provider)
    provider.signing_key = RSAKey.generate_key(2048, parameters={"kid": "first"})
    assert (await finish(access, await begin(access, provider))).status_code == 401


async def test_last_local_admin_protected_even_with_an_sso_admin(access, provider):
    await enable(access, provider)
    uid = access.users["tech_a"].id
    assert (
        await access.call("PUT", f"/admin/users/{uid}", json={"is_admin": True})
    ).status_code == 200
    root_id = access.users["root"].id
    assert (
        await access.call("PUT", f"/admin/users/{root_id}", json={"is_active": False})
    ).status_code == 409


async def test_sso_only_creation_requires_global_admin_and_forbids_password(access, provider):
    body = {
        "username": "new-sso-user",
        "auth_mode": "sso",
        "group_ids": [str(access.groups["A"].id)],
    }
    assert (await access.call("POST", "/admin/users", "admin_a", json=body)).status_code == 403
    assert (
        await access.call("POST", "/admin/users", json={**body, "password": PASSWORD})
    ).status_code == 422
    result = await access.call("POST", "/admin/users", json=body)
    assert result.status_code == 201 and result.json()["auth_mode"] == "sso"


async def test_provider_config_change_invalidates_existing_tokens(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    settings.SSO_PROVIDERS["cognito"].enabled = False
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401


@pytest.mark.parametrize("change", ["disable", "unlink", "groups"])
async def test_administrative_changes_revoke_sso_and_vpn(access, provider, change):
    await enable(access, provider)
    await sign_in(access, provider)
    identifier = await vpn(access)
    uid = access.users["tech_a"].id
    if change == "unlink":
        response = await access.call("GET", f"/admin/users/{uid}/identities")
        identity_id = response.json()[0]["id"]
        result = await access.call("DELETE", f"/admin/users/{uid}/identities/{identity_id}")
    else:
        result = await access.call(
            "PUT",
            f"/admin/users/{uid}",
            json={"is_active": False} if change == "disable" else {"group_ids": []},
        )
    assert result.status_code in (200, 204), result.text
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401
    async with access.db() as db:
        assert (await db.get(Session, identifier)).status == "FAILED"


async def test_late_provider_success_cannot_undo_logout(access, provider, monkeypatch):
    await enable(access, provider)
    await sign_in(access, provider)
    identifier = await vpn(access)
    await age(access, 65)
    started, release = asyncio.Event(), asyncio.Event()
    original = oidc.OIDCAdapter.userinfo

    async def delayed(adapter, *args):
        await original(adapter, *args)
        started.set()
        await release.wait()

    monkeypatch.setattr(oidc.OIDCAdapter, "userinfo", delayed)
    maintenance = asyncio.create_task(maintenance_pass(access.db))
    try:
        await asyncio.wait_for(started.wait(), 5)
        result = await access.call("POST", "/auth/logout", "tech_a")
        assert result.status_code == 200
    finally:
        release.set()
        await asyncio.wait_for(maintenance, 5)
    async with access.db() as db:
        assert (await db.scalar(select(SSOSession))).revoked_at is not None
        assert (await db.get(Session, identifier)).status == "FAILED"
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 401


async def test_sso_session_cannot_be_used_for_another_account(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    async with access.db() as db:
        row = await db.scalar(select(SSOSession))
        other = await db.get(User, access.users["tech_b"].id)
        token = issue_access_token(other, sso_session=row)
    result = await access.client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer " + token}
    )
    assert result.status_code == 401


async def test_identity_cannot_be_linked_to_two_users(access, provider):
    await enable(access, provider)
    result = await access.call(
        "POST",
        f'/admin/users/{access.users["tech_b"].id}/identities',
        json={"provider_id": "cognito", "subject": provider.subject},
    )
    assert result.status_code == 409


async def test_expired_session_cannot_create_vpn_and_token_does_not_extend_it(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    async with access.db() as db:
        row = await db.scalar(select(SSOSession))
        row.expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    result = await access.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert result.status_code == 401


async def test_generic_oidc_reuses_core_without_cognito_claims(access, provider):
    config = settings.SSO_PROVIDERS["cognito"]
    config.kind = "oidc"
    config.revocation_check = "expiry"
    provider.claims = {"token_use": None}
    await enable(access, provider)
    await sign_in(access, provider)
    await age(access, 125)
    assert (await access.call("GET", "/auth/me", "tech_a")).status_code == 200
    listing = await access.client.get("/api/v1/auth/sso/providers")
    assert listing.json()[0]["online_revocation"] is False
    assert listing.json()[0]["status"] == "wip"


async def test_vpn_expiry_is_bounded_by_authentication(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    async with access.db() as db:
        row = await db.scalar(select(SSOSession))
        row.expires_at = utc_now() + timedelta(minutes=3)
        expires = row.expires_at
        await db.commit()
    identifier = await vpn(access)
    async with access.db() as db:
        assert (await db.get(Session, identifier)).expires_at == expires


async def test_failed_remote_logout_does_not_undo_local_logout(access, provider):
    await enable(access, provider)
    await sign_in(access, provider)
    provider.unavailable = True
    assert (await access.call("POST", "/auth/logout", "tech_a")).status_code == 200
    await maintenance_pass(access.db)
    async with access.db() as db:
        row = await db.scalar(select(SSOSession))
        assert row.revoked_at is not None and row.tokens_encrypted is not None
    provider.unavailable = False
    await maintenance_pass(access.db)
    async with access.db() as db:
        assert (await db.scalar(select(SSOSession))).tokens_encrypted is None


@pytest.mark.parametrize("delay", [30, 121])
async def test_userinfo_latency_cannot_extend_access_token_lifetime(
    access, provider, monkeypatch, delay
):
    await enable(access, provider)
    provider.expires_in = 120
    started_at = utc_now()
    clock = [started_at]
    monkeypatch.setattr(sso_router, "utc_now", lambda: clock[0])
    monkeypatch.setattr(oidc, "utc_now", lambda: clock[0])
    original = oidc.OIDCAdapter.userinfo

    async def delayed(adapter, *args):
        await original(adapter, *args)
        clock[0] += timedelta(seconds=delay)

    monkeypatch.setattr(oidc.OIDCAdapter, "userinfo", delayed)
    response = await finish(access, await begin(access, provider))
    assert response.status_code == (302 if delay < 120 else 401), response.text
    async with access.db() as db:
        session = await db.scalar(select(SSOSession))
        if delay < 120:
            assert session.expires_at == started_at + timedelta(seconds=120)
        else:
            assert session is None


async def test_remote_revocation_uses_configured_post_client_authentication(access, provider):
    settings.SSO_PROVIDERS["cognito"].token_endpoint_auth_method = "client_secret_post"
    await enable(access, provider)
    await sign_in(access, provider)
    assert (await access.call("POST", "/auth/logout", "tech_a")).status_code == 200
    await maintenance_pass(access.db)
    request = next(r for r in provider.requests if r.url.path == "/revoke")
    body = parse_qs(request.content.decode())
    assert "authorization" not in request.headers
    assert body["client_id"] == ["factoryvpn-test"]
    assert body["client_secret"] == ["test-secret"]
    assert body["token"] == ["provider-refresh-secret"]
    assert body["token_type_hint"] == ["refresh_token"]


@pytest.mark.parametrize(
    "claims,accepted",
    [
        ({}, True),
        ({"amr": ["pwd"]}, False),
        ({"amr": None}, False),
        ({"amr": "mfa"}, False),
        ({"amr": ["mfa", 2]}, False),
    ],
)
async def test_privileged_sso_requires_explicit_signed_mfa(access, provider, claims, accepted):
    await enable(access, provider)
    async with access.db() as db:
        user = await db.get(User, access.users["tech_a"].id)
        user.permissions = ["users.manage"]
        await db.commit()
    provider.claims = claims
    flow = await begin(access, provider)
    response = await finish(access, flow)
    expected = 302 if accepted else 401 if isinstance(claims.get("amr"), str) else 403
    assert response.status_code == expected
    async with access.db() as db:
        sessions = (await db.scalars(select(SSOSession))).all()
        assert bool(sessions) is accepted
        if accepted:
            assert sessions[0].mfa_verified
