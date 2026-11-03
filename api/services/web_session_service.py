"""Same-origin browser authentication, independent of machine and Bearer credentials."""

import hashlib
import hmac
import secrets
from api.services.session_events import terminal_event

from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from api.config import settings
from api.models.user import User
from api.models.web_session import WebSession
from api.models.sso import SSOSession
from api.time_utils import utc_now
from api.services.sso_service import digest, valid_session, revoke_session


def cookie_name():
    return (
        "__Host-factoryvpn_portal"
        if settings.PORTAL_PUBLIC_URL.startswith("https://")
        else "factoryvpn_portal"
    )


def require_portal():
    if not settings.PORTAL_PUBLIC_URL:
        raise HTTPException(503, "Portal authentication is not configured")


def require_origin(request):
    require_portal()
    if request.headers.get("origin") != settings.PORTAL_PUBLIC_URL:
        raise HTTPException(403, "Untrusted portal origin")


def csrf_token(secret):
    return hmac.new(secret.encode(), b"factoryvpn-portal-csrf", hashlib.sha256).hexdigest()


def check_csrf(request, secret):
    require_origin(request)
    supplied = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(supplied.encode(), csrf_token(secret).encode()):
        raise HTTPException(403, "Invalid CSRF token")


def check_login_request(request):
    require_origin(request)
    # Cross-origin HTML forms cannot supply this header. No credentialed CORS bypass.
    if request.headers.get("x-factoryvpn-portal") != "1":
        raise HTTPException(403, "Portal request header required")


async def lookup(db, secret, *, lock=False):
    if not secret or len(secret) > 128:
        return None
    query = select(WebSession).where(WebSession.secret_hash == digest(secret))
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def valid_web_session(db, identifier, user=None):
    row = await db.get(WebSession, identifier, populate_existing=True)
    if not row or row.revoked_at or row.expires_at <= utc_now():
        return None
    if user is None:
        user = await db.get(User, row.user_id, populate_existing=True)
    if (
        not user
        or not user.is_active
        or user.id != row.user_id
        or user.token_version != row.token_version
    ):
        return None
    if row.sso_session_id:
        if not await valid_session(db, row.sso_session_id, user):
            return None
    elif user.auth_mode != "local":
        return None
    else:
        from api.services.email_security import required

        if required(user) and not row.second_factor_at:
            return None
    return row


async def authenticate(request, db):
    require_portal()
    if request.headers.get("origin"):
        require_origin(request)
    secret = request.cookies.get(cookie_name(), "")
    row = await lookup(db, secret)
    if not row or not await valid_web_session(db, row.id):
        raise HTTPException(401, "Portal session expired or revoked")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_csrf(request, secret)
    user = await db.get(User, row.user_id)
    user._web_session_id = row.id
    user._email_verified = row.second_factor_at is not None
    if row.second_factor_at:
        user._second_factor_at = row.second_factor_at
    if row.sso_session_id:
        user._sso_session_id = row.sso_session_id
    return user


async def revoke_browser(db, row):
    """Caller holds allocation_lock; revoke only this browser and its SSO session."""
    if not row or row.revoked_at:
        return None
    row.revoked_at = utc_now()
    from api.models.session import Session
    from api.services.access_service import record_event
    from api.services.lifecycle import LIVE

    sessions = (
        await db.scalars(
            select(Session)
            .where(Session.web_session_id == row.id, Session.status.in_(LIVE))
            .with_for_update()
        )
    ).all()
    for session in sessions:
        session.status = "CLOSED"
        session.ended_at = utc_now()
        session.error_reason = "Portal session signed out"
        terminal_event(db, session, actor_id=row.user_id)
        record_event(
            db,
            "session_closed",
            row.user_id,
            group_id=session.group_id,
            session_id=session.id,
            gateway_id=session.gateway_id,
            details={"reason": session.error_reason},
        )
    provider_id = None
    if row.sso_session_id:
        auth = await db.get(SSOSession, row.sso_session_id)
        if auth:
            provider_id = auth.provider_id
            await revoke_session(db, auth, "Portal session signed out")
    return provider_id


def clear_cookie(response):
    response.delete_cookie(
        cookie_name(),
        path="/",
        secure=settings.PORTAL_PUBLIC_URL.startswith("https://"),
        httponly=True,
        samesite="lax",
    )


async def issue(
    db, response, user, *, previous_secret=None, sso_session=None, reauthenticated=False
):
    """Caller holds allocation_lock. Rotate the cookie on every successful login."""
    if previous_secret:
        await revoke_browser(db, await lookup(db, previous_secret, lock=True))
    secret = secrets.token_urlsafe(48)
    now = utc_now()
    expires = now + timedelta(minutes=settings.PORTAL_SESSION_MINUTES)
    if sso_session:
        expires = min(expires, sso_session.expires_at)
    row = WebSession(
        id=uuid4(),
        secret_hash=digest(secret),
        user_id=user.id,
        token_version=user.token_version,
        sso_session_id=sso_session.id if sso_session else None,
        expires_at=expires,
        reauthenticated_at=now if sso_session is None or reauthenticated else None,
        second_factor_at=(
            getattr(user, "_second_factor_at", now)
            if getattr(user, "_email_verified", False)
            else None
        ),
    )
    db.add(row)
    response.set_cookie(
        cookie_name(),
        secret,
        max_age=max(1, int((expires - now).total_seconds())),
        path="/",
        httponly=True,
        secure=settings.PORTAL_PUBLIC_URL.startswith("https://"),
        samesite="lax",
    )
    return row, csrf_token(secret)
