"""Provider-independent session policy and bounded online verification.

Network calls run outside database transactions. Security transitions share the
existing allocation lock and never revive a revoked or concurrently changed session.
"""

import asyncio
import hashlib
import json
import logging
import time
from api.services.session_events import terminal_event

from datetime import timedelta

from cryptography.fernet import Fernet
from sqlalchemy import delete, select

from api.config import settings
from api.models.sso import ExternalIdentity, SSOSession, SSOLoginAttempt
from api.models.session import Session
from api.models.user import User
from api.time_utils import utc_now
from api.services.access_service import record_user_event, user_group_ids, record_event
from api.services.oidc import OIDCAdapter, IdentityRejected, ProviderUnavailable, get_provider

logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encrypt(value):
    return (
        Fernet(settings.SSO_TOKEN_KEY.get_secret_value().encode())
        .encrypt(json.dumps(value).encode())
        .decode()
    )


def decrypt(value):
    return json.loads(
        Fernet(settings.SSO_TOKEN_KEY.get_secret_value().encode()).decrypt(value.encode())
    )


def session_deadline(session):
    try:
        provider = get_provider(session.provider_id)
        if provider.fingerprint() != session.provider_fingerprint:
            return None
    except IdentityRejected:
        return None
    deadline = session.expires_at
    if provider.checks_revocation:
        deadline = min(
            deadline, session.verified_at + timedelta(seconds=settings.SSO_MAX_STALENESS_SECONDS)
        )
    return deadline


async def valid_session(db, identifier, user=None):
    session = await db.get(SSOSession, identifier, populate_existing=True)
    if not session or session.revoked_at or not session.user_id:
        return None
    if user is None:
        user = await db.get(User, session.user_id, populate_existing=True)
    deadline = session_deadline(session)
    from api.services.email_security import privileged

    if (
        not user
        or user.id != session.user_id
        or not user.is_active
        or user.auth_mode != "sso"
        or user.token_version != session.token_version
        or not deadline
        or deadline <= utc_now()
        or (privileged(user) and not session.mfa_verified)
    ):
        return None
    linked = await db.scalar(
        select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.provider_id == session.provider_id,
            ExternalIdentity.issuer == session.issuer,
            ExternalIdentity.subject == session.subject,
        )
    )
    return session if linked else None


async def revoke_session(db, session, reason):
    """Caller holds allocation_lock; lock order is SSO session then VPN sessions."""
    await db.refresh(session, with_for_update=True)
    if session.revoked_at:
        return
    session.revoked_at = utc_now()
    session.revocation_reason = reason
    rows = (
        await db.scalars(
            select(Session)
            .where(
                Session.sso_session_id == session.id, Session.status.in_(("REQUESTED", "ACTIVE"))
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    for row in rows:
        row.status = "FAILED"
        row.ended_at = utc_now()
        row.error_reason = reason
        terminal_event(db, row, actor_id=session.user_id)
        record_event(
            db,
            "session_revoked",
            session.user_id,
            group_id=row.group_id,
            session_id=row.id,
            gateway_id=row.gateway_id,
            details={"reason": reason},
        )
    record_user_event(
        db,
        "sso_session_revoked",
        session.user_id,
        session.user_id,
        await user_group_ids(db, session.user_id),
    )


async def revoke_user_sso(db, user, reason):
    rows = (
        await db.scalars(
            select(SSOSession)
            .where(SSOSession.user_id == user.id, SSOSession.revoked_at.is_(None))
            .order_by(SSOSession.id)
        )
    ).all()
    for session in rows:
        await revoke_session(db, session, reason)


async def maintenance_pass(factory):
    """Check all active sessions with bounded concurrency, including during provider outages."""
    from api.services.lifecycle import allocation_lock
    from api.services.oidc import best_effort_revoke

    async with factory() as db:
        rows = (
            await db.scalars(
                select(SSOSession)
                .where(SSOSession.revoked_at.is_(None))
                .order_by(SSOSession.verified_at)
            )
        ).all()
        snapshots = [
            (r.id, r.provider_id, r.tokens_encrypted, r.subject, r.verified_at) for r in rows
        ]

    async def check_one(snapshot):
        identifier, provider_id, encrypted, subject, verified_at = snapshot
        async with factory() as db:
            current = await valid_session(db, identifier)
        outcome = "invalid" if current is None else None
        if current and verified_at <= utc_now() - timedelta(
            seconds=settings.SSO_CHECK_INTERVAL_SECONDS
        ):
            try:
                async with asyncio.timeout(10):
                    adapter = OIDCAdapter(provider_id)
                    await adapter.userinfo(decrypt(encrypted)["access_token"], subject)
                checked_at = utc_now()
                outcome = "verified"
            except IdentityRejected:
                outcome = "invalid"
            except (ProviderUnavailable, TimeoutError):
                outcome = "unavailable"
            except Exception:
                outcome = "invalid"
        if outcome not in ("invalid", "verified"):
            return
        async with factory() as db:
            await allocation_lock(db)
            current = await db.get(SSOSession, identifier, with_for_update=True)
            if not current or current.revoked_at or current.verified_at != verified_at:
                return
            # An HTTP response cannot extend a session whose local authorization changed.
            if outcome == "verified" and await valid_session(db, identifier):
                current.verified_at = checked_at
            else:
                await revoke_session(db, current, "SSO session expired or authorization revoked")
            await db.commit()

    await bounded_checks(check_one, snapshots)
    async with factory() as db:
        pending = (
            await db.scalars(
                select(SSOSession)
                .where(SSOSession.revoked_at.is_not(None), SSOSession.tokens_encrypted.is_not(None))
                .order_by(SSOSession.revoked_at)
            )
        ).all()
        snapshots = [(r.id, r.provider_id, r.tokens_encrypted) for r in pending]

    async def retry_one(snapshot):
        identifier, provider_id, encrypted = snapshot
        if await best_effort_revoke(provider_id, encrypted):
            async with factory() as db:
                row = await db.get(SSOSession, identifier, with_for_update=True)
                if row and row.revoked_at and row.tokens_encrypted == encrypted:
                    row.tokens_encrypted = None
                await db.commit()

    await bounded_checks(retry_one, snapshots)
    async with factory() as db:
        await db.execute(delete(SSOLoginAttempt).where(SSOLoginAttempt.expires_at < utc_now()))
        from api.models.web_session import WebSession

        await db.execute(
            delete(WebSession).where(WebSession.expires_at < utc_now() - timedelta(days=1))
        )
        await db.commit()


async def bounded_checks(operation, snapshots):
    semaphore = asyncio.Semaphore(10)

    async def run(snapshot):
        async with semaphore:
            await operation(snapshot)

    outcomes = await asyncio.gather(
        *(run(snapshot) for snapshot in snapshots), return_exceptions=True
    )
    if any(isinstance(result, Exception) for result in outcomes):
        logger.error("SSO check failed; verification deadlines remain enforced")


async def maintenance_loop(factory):
    while True:
        started = time.monotonic()
        try:
            await maintenance_pass(factory)
        except Exception:
            # Do not log tokens, URLs, SQL parameter values or provider response bodies.
            logger.error("SSO maintenance failed; verification deadlines remain enforced")
        # Poll more often than the verification interval. Sleeping a full interval
        # after a response can skip a due check and exhaust the stale window even
        # when the provider is healthy (completion timestamps include HTTP latency).
        tick = min(5, settings.SSO_CHECK_INTERVAL_SECONDS / 2)
        await asyncio.sleep(max(1, tick - (time.monotonic() - started)))
