"""Browser OIDC login with a PKCE-bound, single-use handoff to an approved client."""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.db.session import get_db
from api.models.sso import ExternalIdentity, SSOSession, SSOLoginAttempt
from api.models.user import User
from api.schemas.sso import (
    SSOStartRequest,
    SSOStartResponse,
    SSOExchangeRequest,
    SSOProviderResponse,
)
from api.schemas.auth import LoginResponse
from api.services.auth_service import issue_access_token
from api.services.access_service import record_user_event, user_group_ids
from api.services.lifecycle import allocation_lock
from api.services.oidc import OIDCAdapter, IdentityRejected, ProviderUnavailable
from api.services.sso_service import digest, encrypt, decrypt, valid_session
from api.time_utils import utc_now

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def adapter_for(identifier):
    try:
        return OIDCAdapter(identifier)
    except IdentityRejected:
        raise HTTPException(404, "SSO provider not available") from None


def cookie_name(attempt):
    return "factoryvpn_sso_" + attempt.id.hex


def pkce(value):
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b"=").decode()


@router.get("/providers", response_model=list[SSOProviderResponse])
async def providers():
    return [
        {"id": key, "label": value.label, "online_revocation": value.checks_revocation}
        for key, value in settings.SSO_PROVIDERS.items()
        if value.enabled
    ]


@router.post("/start", response_model=SSOStartResponse)
async def start(body: SSOStartRequest, response: Response, db: AsyncSession = Depends(get_db)):
    if body.redirect_uri not in settings.SSO_CLIENT_REDIRECT_URIS:
        raise HTTPException(400, "Client redirect URI not allowed")
    attempt = build_attempt(body.provider_id, body.redirect_uri, body.code_challenge, body.state)
    db.add(attempt)
    await db.commit()
    response.headers.update(NO_STORE)
    return start_response(attempt)


def start_response(attempt):
    return {
        "authorization_url": f"{settings.SSO_PUBLIC_URL}/api/v1/auth/sso/authorize/{attempt.id}",
        "expires_in": 300,
    }


def build_attempt(
    provider_id,
    redirect_uri,
    challenge,
    state,
    *,
    portal=False,
    reauth_user_id=None,
    reauth_web_session_id=None,
):
    adapter = adapter_for(provider_id)
    values = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(48),
    }
    if reauth_user_id:
        values.update(reauth_user_id=reauth_user_id, reauth_started_at=utc_now().timestamp())
        values["reauth_web_session_id"] = reauth_web_session_id
    return SSOLoginAttempt(
        id=uuid4(),
        provider_id=provider_id,
        portal=portal,
        provider_fingerprint=adapter.fingerprint,
        state_hash=digest(values["state"]),
        secrets_encrypted=encrypt(values),
        client_challenge=challenge,
        client_state=state,
        redirect_uri=redirect_uri,
        expires_at=utc_now() + timedelta(minutes=5),
    )


@router.get("/authorize/{identifier}", include_in_schema=False)
async def authorize(identifier: UUID, db: AsyncSession = Depends(get_db)):
    attempt = await db.get(SSOLoginAttempt, identifier)
    if (
        not attempt
        or attempt.expires_at <= utc_now()
        or attempt.consumed_at
        or attempt.browser_hash
    ):
        raise HTTPException(400, "Login attempt unavailable")
    adapter = adapter_for(attempt.provider_id)
    if attempt.provider_fingerprint != adapter.fingerprint:
        raise HTTPException(400, "SSO configuration changed; restart login")
    values = decrypt(attempt.secrets_encrypted)
    # Release the read transaction before contacting the provider.
    await db.rollback()
    try:
        url = await adapter.authorization_url(values)
    except (IdentityRejected, ProviderUnavailable):
        raise HTTPException(503, "SSO provider unavailable") from None
    attempt = await db.get(
        SSOLoginAttempt, identifier, with_for_update=True, populate_existing=True
    )
    if (
        not attempt
        or attempt.expires_at <= utc_now()
        or attempt.consumed_at
        or attempt.browser_hash
    ):
        raise HTTPException(400, "Login attempt unavailable")
    browser = secrets.token_urlsafe(32)
    attempt.browser_hash = digest(browser)
    response = RedirectResponse(url, status_code=302, headers=NO_STORE)
    response.set_cookie(
        cookie_name(attempt),
        browser,
        max_age=300,
        httponly=True,
        secure=settings.SSO_PUBLIC_URL.startswith("https://"),
        samesite="lax",
        path=f"/api/v1/auth/sso/{adapter.identifier}/callback",
    )
    await db.commit()
    return response


@router.get("/{provider_id}/callback", include_in_schema=False)
async def callback(provider_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        return await complete_callback(provider_id, request, db)
    except HTTPException:
        if getattr(request.state, "portal_login", False):
            await db.rollback()
            return RedirectResponse("/app/login?error=sso", status_code=303, headers=NO_STORE)
        raise


async def complete_callback(provider_id, request, db):
    adapter = adapter_for(provider_id)
    state = request.query_params.get("state", "")
    if not state or len(state) > 128:
        raise HTTPException(400, "Invalid login response")
    attempt = await db.scalar(
        select(SSOLoginAttempt).where(SSOLoginAttempt.state_hash == digest(state)).with_for_update()
    )
    if (
        not attempt
        or attempt.provider_id != provider_id
        or attempt.consumed_at
        or attempt.expires_at <= utc_now()
        or not attempt.browser_hash
        or not secrets.compare_digest(
            attempt.browser_hash, digest(request.cookies.get(cookie_name(attempt), ""))
        )
        or attempt.provider_fingerprint != adapter.fingerprint
    ):
        raise HTTPException(400, "Invalid login response")
    request.state.portal_login = attempt.portal
    if attempt.portal:
        from api.services.web_session_service import require_portal

        require_portal()
        if settings.PORTAL_PUBLIC_URL != settings.SSO_PUBLIC_URL:
            raise HTTPException(400, "SSO and portal origins differ")
    identifier = attempt.id
    values = decrypt(attempt.secrets_encrypted)
    attempt.consumed_at = utc_now()
    attempt.secrets_encrypted = None
    await db.commit()  # One callback only, even if the provider times out.
    code = request.query_params.get("code", "")
    if request.query_params.get("error") or not code or len(code) > 4096:
        raise HTTPException(400, "SSO login was not completed")
    try:
        tokens, claims, access_expires_at = await adapter.exchange(code, values)
    except IdentityRejected:
        raise HTTPException(401, "SSO authentication rejected") from None
    except ProviderUnavailable:
        raise HTTPException(503, "SSO provider unavailable; restart login") from None
    verified_at = utc_now()
    await allocation_lock(db)
    attempt = await db.get(
        SSOLoginAttempt, identifier, with_for_update=True, populate_existing=True
    )
    if (
        not attempt
        or attempt.expires_at <= utc_now()
        or adapter_for(provider_id).fingerprint != adapter.fingerprint
    ):
        raise HTTPException(400, "Login attempt expired or configuration changed")
    identity = await db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider_id == provider_id,
            ExternalIdentity.issuer == claims["iss"],
            ExternalIdentity.subject == claims["sub"],
        )
    )
    user = await db.get(User, identity.user_id) if identity else None
    if not user or not user.is_active or user.auth_mode != "sso":
        raise HTTPException(403, "Identity is not enabled for FactoryVPN")
    from api.services.email_security import privileged

    amr = claims.get("amr", [])
    mfa_verified = bool(
        (
            isinstance(amr, list)
            and all(isinstance(v, str) for v in amr)
            and set(amr).intersection(adapter.provider.mfa_amr_values)
        )
        or (isinstance(claims.get("acr"), str) and claims["acr"] in adapter.provider.mfa_acr_values)
    )
    if privileged(user) and not mfa_verified:
        raise HTTPException(403, "SSO provider did not confirm required MFA")
    reauth_web = None
    if values.get("reauth_user_id"):
        auth_time = claims.get("auth_time")
        if (
            str(user.id) != values["reauth_user_id"]
            or not user.is_admin
            or not isinstance(auth_time, (int, float))
            or isinstance(auth_time, bool)
            or not values["reauth_started_at"] - 60 <= auth_time <= utc_now().timestamp() + 30
        ):
            raise HTTPException(401, "Provider did not confirm recent authentication")
        from api.services import web_session_service as web

        reauth_web = await web.lookup(db, request.cookies.get(web.cookie_name()), lock=True)
        if (
            not reauth_web
            or str(reauth_web.id) != values.get("reauth_web_session_id")
            or not await web.valid_web_session(db, reauth_web.id, user)
        ):
            raise HTTPException(401, "Original portal session no longer authorized")
    now = utc_now()
    expires_at = min(
        now + timedelta(minutes=settings.SSO_SESSION_MINUTES),
        access_expires_at,
        datetime.fromtimestamp(claims["exp"], timezone.utc),
    )
    if expires_at <= now or (
        adapter.provider.checks_revocation
        and verified_at + timedelta(seconds=settings.SSO_MAX_STALENESS_SECONDS) <= now
    ):
        raise HTTPException(401, "SSO authentication expired")
    session = SSOSession(
        id=uuid4(),
        user_id=user.id,
        provider_id=provider_id,
        provider_fingerprint=adapter.fingerprint,
        issuer=claims["iss"],
        subject=claims["sub"],
        token_version=user.token_version,
        tokens_encrypted=encrypt(tokens),
        expires_at=expires_at,
        verified_at=verified_at,
        mfa_verified=mfa_verified,
    )
    db.add(session)
    if reauth_web:
        # Confirmation does not log out the browser or terminate its live VPNs.
        # The newly acquired provider tokens are queued for revocation, never exposed.
        reauth_web.reauthenticated_at = now
        session.revoked_at, session.revocation_reason = now, "Identity confirmation completed"
        record_user_event(db, "backup_reauthenticated", user.id, user.id, [])
        await db.commit()
        response = RedirectResponse("/app/backups", status_code=303, headers=NO_STORE)
        response.delete_cookie(
            cookie_name(attempt), path=f"/api/v1/auth/sso/{provider_id}/callback"
        )
        return response
    if attempt.portal:
        from api.services import web_session_service as web

        # Web attempts never create an exchange code or send a token to JavaScript.
        response = RedirectResponse(
            "/app/backups" if values.get("reauth_user_id") else "/app/",
            status_code=303,
            headers=NO_STORE,
        )
        await web.issue(
            db,
            response,
            user,
            previous_secret=request.cookies.get(web.cookie_name()),
            sso_session=session,
            reauthenticated=bool(values.get("reauth_user_id")),
        )
        user.last_login = now
        record_user_event(db, "sso_login", user.id, user.id, await user_group_ids(db, user.id))
        response.delete_cookie(
            cookie_name(attempt), path=f"/api/v1/auth/sso/{provider_id}/callback"
        )
        await db.commit()
        return response
    handoff = secrets.token_urlsafe(32)
    attempt.exchange_hash = digest(handoff)
    attempt.sso_session_id = session.id
    attempt.expires_at = now + timedelta(seconds=60)
    user.last_login = now
    record_user_event(db, "sso_login", user.id, user.id, await user_group_ids(db, user.id))
    response = RedirectResponse(
        attempt.redirect_uri + "?" + urlencode({"code": handoff, "state": attempt.client_state}),
        status_code=302,
        headers=NO_STORE,
    )
    response.delete_cookie(cookie_name(attempt), path=f"/api/v1/auth/sso/{provider_id}/callback")
    await db.commit()
    return response


@router.post("/exchange", response_model=LoginResponse)
async def exchange(
    body: SSOExchangeRequest, response: Response, db: AsyncSession = Depends(get_db)
):
    await allocation_lock(db)
    attempt = await db.scalar(
        select(SSOLoginAttempt)
        .where(SSOLoginAttempt.exchange_hash == digest(body.code))
        .with_for_update()
    )
    if (
        not attempt
        or attempt.expires_at <= utc_now()
        or not attempt.sso_session_id
        or not secrets.compare_digest(pkce(body.code_verifier), attempt.client_challenge)
    ):
        raise HTTPException(401, "Invalid login exchange")
    session = await valid_session(db, attempt.sso_session_id)
    if not session:
        raise HTTPException(401, "SSO session no longer authorized")
    user = await db.get(User, session.user_id)
    token = issue_access_token(user, sso_session=session)
    attempt.exchange_hash = None
    await db.commit()
    response.headers.update(NO_STORE)
    return LoginResponse(access_token=token, user_id=str(user.id), username=user.username)


@router.get("/signed-out", include_in_schema=False)
async def signed_out():
    if settings.PORTAL_PUBLIC_URL:
        return RedirectResponse("/app/login", status_code=303, headers=NO_STORE)
    return HTMLResponse(
        "<p>FactoryVPN: signed out. You can close this window.</p>", headers=NO_STORE
    )
