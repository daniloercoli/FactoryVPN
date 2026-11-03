"""Session creation, terminal transitions and expiry."""

import logging
import hashlib
import json
from api.services.session_events import event, terminal_event

from datetime import timedelta
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select
from api.time_utils import utc_now
from api.models.session import Session
from api.models.session_request import SessionRequest
from api.models.gateway import Gateway
from api.models.user import User
from api.services.credentials import gateway_configured, active_certificate
from api.services.lifecycle import allocation_lock, assign_ip, occupied, network_for, LIVE
from api.services.access_service import can_connect, record_event, scope

logger = logging.getLogger(__name__)


async def create_session(
    db,
    technician_id,
    phone_number=None,
    gateway_id=None,
    sso_session_id=None,
    idempotency_key=None,
    web_session_id=None,
):
    await allocation_lock(db)
    user = await db.get(User, technician_id, populate_existing=True)
    fingerprint = hashlib.sha256(
        json.dumps([str(gateway_id) if gateway_id else None, phone_number]).encode()
    ).hexdigest()
    if idempotency_key is not None:
        receipt = await db.get(SessionRequest, (technician_id, idempotency_key))
        if receipt:
            if receipt.request_hash != fingerprint:
                raise HTTPException(409, "Idempotency key already used for a different request")
            existing = await db.scalar(
                select(Session).where(
                    Session.id == receipt.session_id, scope(Session.group_id, user)
                )
            )
            if existing is None:
                raise HTTPException(404, "Session not found")
            existing = await get_session_status(db, existing.id)
            existing._idempotency_replay = True
            await db.commit()
            return existing
    gateway = (
        await db.execute(
            select(Gateway).where(
                Gateway.id == gateway_id if gateway_id else Gateway.phone_number == phone_number
            )
        )
    ).scalar_one_or_none()
    if not gateway:
        return None
    auth_session = None
    if sso_session_id:
        from api.services.sso_service import valid_session

        auth_session = await valid_session(db, sso_session_id, user)
        if auth_session is None:
            raise HTTPException(401, "SSO session no longer authorized")
    elif user and user.auth_mode == "sso":
        raise HTTPException(401, "SSO authentication required")
    web_session = None
    if web_session_id:
        from api.services.web_session_service import valid_web_session

        web_session = await valid_web_session(db, web_session_id, user)
        if web_session is None or web_session.sso_session_id != sso_session_id:
            raise HTTPException(401, "Portal session no longer authorized")
    if not await can_connect(db, user, gateway):
        raise HTTPException(404, "Gateway not found or not authorized for connection")
    if not await gateway_configured(db, gateway.id):
        raise HTTPException(409, "Gateway credentials are not provisioned")
    if not await active_certificate(db, "technician", user.id):
        raise HTTPException(409, "Prepare and activate your personal VPN profile first")
    busy = (
        await db.execute(
            select(Session.id).where(
                occupied(),
                (Session.technician_id == technician_id) | (Session.gateway_id == gateway.id),
            )
        )
    ).first()
    if busy:
        raise HTTPException(409, "Peer already has a session or cleanup is pending")
    slots = set(
        (await db.execute(select(Session.network_slot).where(Session.cleaned_at.is_(None))))
        .scalars()
        .all()
    )
    slot = next((n for n in range(1, 16384) if n not in slots), None)
    if slot is None:
        raise HTTPException(503, "Session network pool exhausted")
    session = Session(
        id=uuid4(),
        technician_id=technician_id,
        gateway_id=gateway.id,
        group_id=gateway.group_id,
        sso_session_id=sso_session_id,
        web_session_id=web_session_id,
        status="REQUESTED",
        activation_mode=gateway.activation_mode,
        expires_at=min(
            [utc_now() + timedelta(minutes=30)]
            + ([auth_session.expires_at] if auth_session else [])
            + ([web_session.expires_at] if web_session else [])
        ),
        diagnostics_started_at=utc_now(),
        network_slot=slot,
        technician_vpn_ip=await assign_ip(db, user),
        gateway_vpn_ip=await assign_ip(db, gateway),
        lan_cidr=gateway.vpn_subnet,
    )
    try:
        network_for(session)
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, "Gateway has an invalid or overlapping LAN configuration") from exc
    db.add(session)
    await db.flush()
    event(db, session, "session_requested", actor_id=user.id)
    if idempotency_key is not None:
        await db.flush()
        db.add(
            SessionRequest(
                user_id=technician_id,
                request_key=idempotency_key,
                request_hash=fingerprint,
                session_id=session.id,
            )
        )
    record_event(
        db,
        "session_requested",
        user.id,
        group_id=session.group_id,
        gateway_id=gateway.id,
        session_id=session.id,
    )
    await db.commit()
    await db.refresh(session)
    return session


async def get_session_status(db, session_id):
    """Refresh and maintain status under a row lock; the caller commits the operation."""
    session = (
        await db.execute(
            select(Session)
            .where(Session.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        session
        and session.status in LIVE
        and session.expires_at
        and session.expires_at <= utc_now()
    ):
        session.status = "EXPIRED"
        session.ended_at = utc_now()
        terminal_event(db, session, reason="expired")
        await db.flush()
    elif (
        session
        and session.status == "ACTIVE"
        and session.worker_seen_at
        and session.worker_seen_at < utc_now() - timedelta(seconds=30)
    ):
        session.status = "REQUESTED"
        session.network_ready_at = None
        event(db, session, "worker_stale")
        await db.flush()
    return session


async def close_session(db, session_id, actor_id=None):
    session = (
        await db.execute(
            select(Session)
            .where(Session.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not session:
        return False
    if session.status in LIVE:
        session.status = "CLOSED"
        session.ended_at = utc_now()
        terminal_event(db, session, actor_id=actor_id, reason="manual_close")
        record_event(
            db,
            "session_closed",
            actor_id or session.technician_id,
            group_id=session.group_id,
            session_id=session.id,
            gateway_id=session.gateway_id,
        )
    # get_session_status may have expired the session in this same transaction.
    await db.commit()
    return True


async def expire_stale_sessions(db):
    sessions = (
        (
            await db.execute(
                select(Session)
                .where(Session.status.in_(LIVE), Session.expires_at <= utc_now())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for session in sessions:
        session.status = "EXPIRED"
        session.ended_at = utc_now()
        terminal_event(db, session, reason="expired")
    if sessions:
        await db.commit()
    return len(sessions)
