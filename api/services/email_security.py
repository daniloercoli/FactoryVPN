"""Email verification policy and atomic, bounded challenges shared by every login."""

from datetime import timedelta
import hashlib
import hmac
import secrets
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import delete, select
from api.config import settings
from api.models import EmailChallenge, User
from api.services import mail_service
from api.services.lifecycle import allocation_lock
from api.services.access_service import record_user_event, user_group_ids, terminate_user_sessions
from api.time_utils import utc_now

MANAGEMENT = {
    "users.manage",
    "groups.manage",
    "gateways.manage",
    "gateways.provision",
    "sessions.manage",
}


def privileged(user):
    return bool(user.is_admin or MANAGEMENT.intersection(user.permissions or []))


def required(user):
    return privileged(user) or bool(user.email_two_step)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def code_hash(identifier, code):
    key = settings.EMAIL_CODE_KEY.get_secret_value()
    if len(key) < 32:
        raise HTTPException(503, "Email verification is not configured")
    return hmac.new(
        key.encode(), (str(identifier) + ":" + code).encode(), hashlib.sha256
    ).hexdigest()


def binding_cookie():
    return (
        "__Host-factoryvpn_verification"
        if settings.PORTAL_PUBLIC_URL.startswith("https://")
        else "factoryvpn_verification"
    )


def bind_browser(request, response):
    secret = request.cookies.get(binding_cookie()) or secrets.token_urlsafe(48)
    response.set_cookie(
        binding_cookie(),
        secret,
        max_age=600,
        httponly=True,
        secure=settings.PORTAL_PUBLIC_URL.startswith("https://"),
        samesite="strict",
        path="/",
    )
    return digest(secret)


def payload(row, secret):
    local, domain = row.email.split("@", 1)
    return {
        "challenge_required": True,
        "challenge_token": secret,
        "destination": local[:1] + "***@" + domain,
        "expires_at": row.expires_at,
        "resend_after": 60,
    }


def sending_allowed(user, *, transition=False):
    now = utc_now()
    if user.email_locked_until and user.email_locked_until > now:
        raise HTTPException(
            429, "Email verification temporarily locked", headers={"Retry-After": "900"}
        )
    if (
        not transition
        and user.email_last_sent_at
        and user.email_last_sent_at > now - timedelta(seconds=60)
    ):
        raise HTTPException(
            429, "Wait before requesting another email", headers={"Retry-After": "60"}
        )
    if not user.email_window_at or user.email_window_at <= now - timedelta(hours=1):
        user.email_window_at, user.email_send_count = now, 0
    if user.email_send_count >= 10:
        raise HTTPException(429, "Email hourly limit reached", headers={"Retry-After": "3600"})
    user.email_send_count += 1
    user.email_last_sent_at = now


async def deliver(db, row, code):
    await db.commit()  # SMTP never holds the installation lifecycle lock.
    try:
        await mail_service.send_code(row.email, code, row.purpose)
    except Exception:
        await allocation_lock(db)
        current = await db.get(EmailChallenge, row.id, populate_existing=True)
        if current:
            current.consumed_at = utc_now()
            record_user_event(
                db,
                "email_delivery_failed",
                current.user_id,
                current.user_id,
                await user_group_ids(db, current.user_id),
            )
        await db.commit()
        raise HTTPException(
            503, "Email delivery unavailable; access has not been granted"
        ) from None


async def start(
    db,
    user,
    purpose,
    *,
    email=None,
    browser_hash=None,
    session_id=None,
    next_email=None,
    transition=False,
):
    if not mail_service.configured():
        raise HTTPException(503, "Email verification is not configured")
    try:
        destination = mail_service.email_address(email or user.email)
    except ValueError:
        raise HTTPException(
            409, "A valid account email is required; contact an administrator"
        ) from None
    sending_allowed(user, transition=transition)
    secret, code, identifier = (
        secrets.token_urlsafe(48),
        f"{secrets.randbelow(1000000):06d}",
        uuid4(),
    )
    row = EmailChallenge(
        id=identifier,
        secret_hash=digest(secret),
        user_id=user.id,
        token_version=user.token_version,
        purpose=purpose,
        email=destination,
        next_email=next_email,
        code_hash=code_hash(identifier, code),
        browser_hash=browser_hash,
        web_session_id=session_id,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    await db.execute(
        delete(EmailChallenge).where(EmailChallenge.expires_at < utc_now() - timedelta(days=1))
    )
    db.add(row)
    await deliver(db, row, code)
    return payload(row, secret)


async def lookup(db, secret, purposes, *, browser_secret=None, actor=None):
    await allocation_lock(db)
    row = await db.scalar(
        select(EmailChallenge)
        .where(EmailChallenge.secret_hash == digest(secret))
        .execution_options(populate_existing=True)
    )
    if not row or row.purpose not in purposes or row.consumed_at or row.expires_at <= utc_now():
        raise HTTPException(400, "Verification expired or invalid")
    user = await db.get(User, row.user_id, populate_existing=True)
    if (
        not user
        or not user.is_active
        or user.auth_mode != "local"
        or user.token_version != row.token_version
        or (
            row.browser_hash
            and not secrets.compare_digest(row.browser_hash, digest(browser_secret or ""))
        )
    ):
        raise HTTPException(400, "Verification expired or invalid")
    if row.web_session_id:
        from api.services.web_session_service import valid_web_session

        if (
            not actor
            or actor.id != user.id
            or getattr(actor, "_web_session_id", None) != row.web_session_id
            or not await valid_web_session(db, row.web_session_id, user)
        ):
            raise HTTPException(401, "Original portal session no longer authorized")
    if user.email_locked_until and user.email_locked_until > utc_now():
        raise HTTPException(429, "Email verification temporarily locked")
    return row, user


async def verify(db, secret, code, purposes, **kwargs):
    row, user = await lookup(db, secret, purposes, **kwargs)
    if row.attempts >= 5:
        raise HTTPException(400, "Verification expired or invalid")
    row.attempts += 1
    if not secrets.compare_digest(row.code_hash, code_hash(row.id, code)):
        user.email_failures += 1
        if row.attempts >= 5:
            row.consumed_at = utc_now()
        if user.email_failures >= 10:
            user.email_locked_until = utc_now() + timedelta(minutes=15)
            user.email_failures = 0
        record_user_event(
            db, "email_verification_failed", user.id, user.id, await user_group_ids(db, user.id)
        )
        await db.commit()  # A rejected request must still consume its attempt.
        raise HTTPException(400, "Incorrect or expired verification code")
    row.consumed_at = utc_now()
    user.email_failures, user.email_locked_until, user.email_last_sent_at = 0, None, None
    user._email_verified = True
    user._second_factor_at = utc_now()
    record_user_event(db, "email_verified", user.id, user.id, await user_group_ids(db, user.id))
    return row, user


async def resend(db, secret, purposes, **kwargs):
    row, user = await lookup(db, secret, purposes, **kwargs)
    if not mail_service.configured():
        raise HTTPException(503, "Email verification is not configured")
    sending_allowed(user)
    code = f"{secrets.randbelow(1000000):06d}"
    row.code_hash = code_hash(row.id, code)
    # Neither expiry nor attempt budget is reset by resend.
    await deliver(db, row, code)
    return payload(row, secret)


async def invalidate_access(db, user, actor_id=None):
    from api.services.sso_service import revoke_user_sso

    await revoke_user_sso(db, user, "Email security changed")
    user.token_version += 1
    await db.execute(delete(EmailChallenge).where(EmailChallenge.user_id == user.id))
    await terminate_user_sessions(db, user, reason="Email security changed", actor_id=actor_id)


def require_recent(user):
    if required(user) and user.auth_mode == "local":
        verified = getattr(user, "_second_factor_at", None)
        if not verified or verified < utc_now() - timedelta(minutes=5):
            raise HTTPException(428, "Recent email verification required")


async def recover_email(db, user, email, actor_id=None):
    if user.auth_mode != "local":
        raise HTTPException(409, "SSO recovery is managed by the identity provider")
    user.email = mail_service.email_address(email)
    user.email_verified_at = None
    user.email_two_step = True
    user.email_last_sent_at = user.email_window_at = user.email_locked_until = None
    user.email_send_count = user.email_failures = 0
    await invalidate_access(db, user, actor_id)
    record_user_event(
        db, "email_recovery_requested", actor_id, user.id, await user_group_ids(db, user.id)
    )
