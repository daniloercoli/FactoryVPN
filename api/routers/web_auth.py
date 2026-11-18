"""Browser-only login endpoints. They never return a Bearer or provider token."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.user import User
from api.models.web_session import WebSession
from api.routers.auth import get_current_user, current_user_response, logout as logout_all
from api.schemas.auth import LoginRequest, PasswordChangeRequest
from api.schemas.sso import LogoutResponse, SSOStartResponse
from api.schemas.web import WebSessionResponse, WebSSORequest
from api.services.auth_service import authenticate_local, change_local_password
from api.services.lifecycle import allocation_lock
from api.services.access_service import record_user_event, user_group_ids
from api.services import web_session_service as web
from api.time_utils import utc_now
from api.schemas.email_security import EmailChallengeResponse, EmailProof, EmailResend
from api.services import email_security as email_auth

router = APIRouter()


@router.post("/sso/start", response_model=SSOStartResponse)
async def sso_start(body: WebSSORequest, request: Request, db: AsyncSession = Depends(get_db)):
    import secrets
    from api.config import settings
    from api.routers.sso import build_attempt, start_response, pkce

    web.check_login_request(request)
    reauth_user = None
    if body.reauthenticate:
        from api.services.access_service import lock_actor

        reauth_user = await web.authenticate(request, db)
        await lock_actor(db, reauth_user)
        if not reauth_user.is_admin or reauth_user.auth_mode != "sso":
            raise HTTPException(403, "Global SSO administrator required")
    if settings.SSO_PUBLIC_URL != settings.PORTAL_PUBLIC_URL:
        raise HTTPException(503, "SSO and portal must share the same public origin")
    attempt = build_attempt(
        body.provider_id,
        settings.PORTAL_PUBLIC_URL + "/app/",
        pkce(secrets.token_urlsafe(48)),
        secrets.token_urlsafe(32),
        portal=True,
        reauth_user_id=str(reauth_user.id) if reauth_user else None,
        reauth_web_session_id=str(reauth_user._web_session_id) if reauth_user else None,
    )
    db.add(attempt)
    await db.commit()
    return start_response(attempt)


async def browser_user(user: User = Depends(get_current_user)):
    if not getattr(user, "_web_session_id", None):
        raise HTTPException(401, "A portal session is required")
    return user


async def payload(db, user, row, csrf):
    return {
        "user": await current_user_response(db, user),
        "csrf_token": csrf,
        "expires_at": row.expires_at,
    }


@router.post("/login", response_model=WebSessionResponse | EmailChallengeResponse)
async def login(
    body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    web.check_login_request(request)
    user = await authenticate_local(db, body.username, body.password)
    if body.emergency_code is not None:
        from api.services import console_access

        await console_access.verify(db, user, body.emergency_code)
    elif email_auth.required(user):
        return await email_auth.start(
            db, user, "login_web", browser_hash=email_auth.bind_browser(request, response)
        )
    user.last_login = utc_now()
    row, csrf = await web.issue(
        db, response, user, previous_secret=request.cookies.get(web.cookie_name())
    )
    record_user_event(db, "portal_login", user.id, user.id, await user_group_ids(db, user.id))
    await db.commit()
    return await payload(db, user, row, csrf)


@router.post("/login/verify", response_model=WebSessionResponse)
async def verify_login(body: EmailProof, request: Request, response: Response, db=Depends(get_db)):
    web.check_login_request(request)
    _, user = await email_auth.verify(
        db,
        body.challenge_token,
        body.code,
        {"login_web"},
        browser_secret=request.cookies.get(email_auth.binding_cookie()),
    )
    user.email_verified_at = user.email_verified_at or utc_now()
    user.last_login = utc_now()
    row, csrf = await web.issue(
        db, response, user, previous_secret=request.cookies.get(web.cookie_name())
    )
    record_user_event(db, "portal_login", user.id, user.id, await user_group_ids(db, user.id))
    await db.commit()
    return await payload(db, user, row, csrf)


@router.post("/login/resend", response_model=EmailChallengeResponse)
async def resend_login(body: EmailResend, request: Request, db=Depends(get_db)):
    web.check_login_request(request)
    return await email_auth.resend(
        db,
        body.challenge_token,
        {"login_web"},
        browser_secret=request.cookies.get(email_auth.binding_cookie()),
    )


@router.get("/session", response_model=WebSessionResponse)
async def session(request: Request, db: AsyncSession = Depends(get_db)):
    if request.headers.get("authorization"):
        raise HTTPException(401, "A portal session is required")
    user = await web.authenticate(request, db)
    row = await db.get(WebSession, user._web_session_id)
    return await payload(db, user, row, web.csrf_token(request.cookies[web.cookie_name()]))


@router.post("/password", response_model=WebSessionResponse)
async def password(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(browser_user),
):
    await change_local_password(db, user, body.current_password, body.new_password)
    row, csrf = await web.issue(
        db, response, user, previous_secret=request.cookies.get(web.cookie_name())
    )
    await db.commit()
    return await payload(db, user, row, csrf)


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    # Also works after expiry or account revocation, so an obsolete cookie can be cleared.
    web.require_origin(request)
    secret = request.cookies.get(web.cookie_name())
    if secret:
        web.check_csrf(request, secret)
    else:
        web.check_login_request(request)
    await allocation_lock(db)
    row = await web.lookup(db, secret, lock=True)
    provider = await web.revoke_browser(db, row)
    if row:
        record_user_event(
            db, "portal_logout", row.user_id, row.user_id, await user_group_ids(db, row.user_id)
        )
    await db.commit()
    web.clear_cookie(response)
    urls = []
    if provider:
        from api.services.oidc import OIDCAdapter

        try:
            url = await OIDCAdapter(provider).logout_url()
            if url:
                urls.append(url)
        except Exception:
            pass  # Local revocation is committed; SSO maintenance retries token revocation.
    return {"message": "Portal session signed out", "provider_logout_urls": urls}


@router.post("/logout-all", response_model=LogoutResponse)
async def global_logout(
    response: Response, db: AsyncSession = Depends(get_db), user: User = Depends(browser_user)
):
    result = await logout_all(db, user)
    web.clear_cookie(response)
    return result
