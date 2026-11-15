"""
Sessions router
"""

from api.services.session_events import event, terminal_event

from fastapi import APIRouter, Depends, HTTPException, status, Query, Response, Header
from datetime import timedelta
from math import ceil
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from api.time_utils import utc_now
from uuid import UUID
from api.db.session import get_db
from api.models.user import User
from api.models.session import Session
from api.routers.auth import get_current_user, require_admin
from api.schemas.session import SessionCreateRequest, SessionCreateResponse
from api.schemas.client import (
    SessionDetailResponse,
    SessionListResponse,
    SessionStatus,
    PAGINATION_RESPONSES,
    MessageResponse,
    ExpirationResponse,
)
from api.services.session_service import (
    create_session,
    get_session_status,
    close_session,
    expire_stale_sessions,
)
from api.services.sms_service import send_sms_detailed as send_sms, SMSResult
from api.services.credentials import sms_secret
from factoryvpn_common.protocol import activation_message
from api.models.gateway import Gateway
from api.services.lifecycle import validate_live, allocation_lock
from api.services.access_service import has_permission, scope, lock_actor
from api.permissions import Permission
from api.schemas.gateway import ActivationMode
from api.services.vpn_catalog import session_list

router = APIRouter()


async def get_authorized_session(db: AsyncSession, session_id: UUID, user: User):
    session = (
        await db.execute(
            select(Session).where(Session.id == session_id, scope(Session.group_id, user))
        )
    ).scalar_one_or_none()
    if not session or (
        not has_permission(user, Permission.SESSIONS_MANAGE) and session.technician_id != user.id
    ):
        # Do not disclose whether another technician's session exists.
        raise HTTPException(status_code=404, detail="Session not found")
    return await get_session_status(db, session_id)


@router.get("/", response_model=list[SessionListResponse], responses=PAGINATION_RESPONSES)
async def list_own_sessions(
    response: Response,
    q: str | None = Query(None, max_length=100),
    group_id: UUID | None = None,
    gateway_id: UUID | None = None,
    status_filter: SessionStatus | None = None,
    activation_mode: ActivationMode | None = None,
    reserved: bool | None = None,
    cleanup_pending: bool | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Search only owned sessions in visible historical groups.

    q matches the exact session/gateway UUID or a currently visible gateway's
    label, MAC or phone. reserved includes live sessions and unreleased resources.
    X-Total-Count applies all scope and search filters before pagination.
    """
    items, total = await session_list(
        db,
        user,
        limit=limit,
        offset=offset,
        q=q,
        group_id=group_id,
        gateway_id=gateway_id,
        status_filter=status_filter,
        activation_mode=activation_mode,
        reserved=reserved,
        cleanup=cleanup_pending,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.post("/", response_model=SessionCreateResponse)
async def create_assistance_session(
    request: SessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    idempotency_key: UUID | None = Header(None),
):
    """Request assistance using the gateway's configured activation mode.

    Provide exactly one of gateway_id or phone_number. HTTP 200 does not imply an
    active VPN: inspect status, which may be REQUESTED or FAILED. Poll the detail
    until gateway_ready_at is set, connect the technician VPN, then wait for ACTIVE.
    Sessions use the configured maximum (3 hours by default), bounded by the opening browser/SSO lifetime;
    each peer has one reserved session. Browser logout closes its associated VPNs.
    Optional Idempotency-Key (UUID) recovers the same request for this account,
    including terminal outcomes, without resending SMS. Changed input returns 409.
    """
    await lock_actor(db, current_user)
    session = await create_session(
        db=db,
        technician_id=current_user.id,
        phone_number=request.phone_number,
        gateway_id=request.gateway_id,
        **(
            {"sso_session_id": current_user._sso_session_id}
            if getattr(current_user, "_sso_session_id", None)
            else {}
        ),
        **({"idempotency_key": idempotency_key} if idempotency_key is not None else {}),
        **(
            {"web_session_id": current_user._web_session_id}
            if getattr(current_user, "_web_session_id", None)
            else {}
        ),
    )
    if not session:
        raise HTTPException(status_code=404, detail="Gateway not found")
    if getattr(session, "_idempotency_replay", False):
        return SessionCreateResponse(
            session_id=str(session.id),
            status=session.status,
            message="Existing session request recovered",
            replayed=True,
        )
    if session.activation_mode == "wifi":
        message = "Waiting for gateway Wi-Fi check"
    else:
        message = await dispatch_activation(db, session)
    return SessionCreateResponse(session_id=str(session.id), status=session.status, message=message)


async def dispatch_activation(db, session, *, fail_on_rejection=True):
    # Claim the attempt durably before external I/O. Another API replica/tab must
    # observe the cooldown even when the provider outcome is lost or rejected.
    await allocation_lock(db)
    if not await validate_live(db, session, require_activation=False):
        await db.commit()
        raise HTTPException(409, "Session credentials are no longer authorized")
    now = utc_now()
    if session.sms_last_attempt_at:
        remaining = (session.sms_last_attempt_at + timedelta(seconds=60) - now).total_seconds()
        if remaining > 0:
            await db.commit()
            raise HTTPException(
                429,
                "Wait before sending another activation SMS",
                headers={"Retry-After": str(ceil(remaining))},
            )
    session.sms_last_attempt_at = now
    event(db, session, "sms_attempt")
    session.diagnostic_state = {
        **(session.diagnostic_state or {}),
        "sms": {"outcome": "pending", "received_at": now.isoformat()},
    }
    gateway = await db.get(Gateway, session.gateway_id)
    phone = gateway.phone_number
    token = activation_message(
        session.id, session.expires_at.timestamp(), await sms_secret(db, gateway.id)
    )
    await db.commit()
    accepted = await send_sms(
        phone_number=phone,
        message=token,
    )
    # The gateway may acknowledge real receipt while the provider request is in flight.
    # Lock and refresh before recording the provider outcome; never overwrite a close/expiry.
    await db.refresh(session, with_for_update=True)
    if session.sms_last_attempt_at != now:
        await db.commit()
        return "A newer activation attempt superseded this response"
    outcome = (
        accepted.outcome
        if isinstance(accepted, SMSResult)
        else ("accepted" if accepted else "unconfirmed")
    )
    session.diagnostic_state = {
        **(session.diagnostic_state or {}),
        "sms": {"outcome": outcome, "received_at": utc_now().isoformat()},
    }
    event(db, session, "sms_" + outcome)
    if not accepted:
        from api.services.technical_log_writer import emit

        emit(session, "sms_failed")
    if accepted:
        session.sms_sent_at = utc_now()
        message = "SMS accepted by provider, waiting for gateway activation"
    elif session.activation_received_at:
        message = "Gateway confirmed SMS receipt"
    elif not fail_on_rejection:
        message = "SMS resend failed; session remains valid until expiry"
    else:
        if session.status in ("REQUESTED", "ACTIVE"):
            session.status = "FAILED"
            session.ended_at = utc_now()
            session.error_reason = "SMS provider did not accept activation"
            terminal_event(db, session, reason="sms_" + outcome)
        message = "Session failed: SMS provider did not accept activation"
    await db.commit()
    return message


@router.post(
    "/{session_id}/wake",
    response_model=SessionCreateResponse,
    responses={
        429: {
            "description": "Another SMS attempt was reserved within the last 60 seconds.",
            "headers": {"Retry-After": {"schema": {"type": "integer", "minimum": 1}}},
        }
    },
)
async def resend_activation(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retry a live SMS session, at most once per 60 seconds across all callers.

    Attempts count even when the provider rejects the SMS or the response is lost.
    Retry-After reports the remaining cooldown; retries never extend the session.
    """
    await lock_actor(db, current_user)
    session = await get_authorized_session(db, session_id, current_user)
    if session.status not in ("REQUESTED", "ACTIVE") or session.activation_mode != "sms":
        await db.commit()
        raise HTTPException(409, "Only a live SMS session can be woken")
    if not await validate_live(db, session, require_activation=False):
        await db.commit()
        raise HTTPException(409, "Session credentials are no longer authorized")
    message = await dispatch_activation(db, session, fail_on_rejection=False)
    return SessionCreateResponse(session_id=str(session.id), status=session.status, message=message)


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read an owned session in a current group, or use sessions.manage in that group.

    Global administrators can read all sessions. Invisible sessions return 404.
    gateway_ready_at indicates gateway
    readiness; ACTIVE also requires the technician VPN. cleaned_at confirms that
    network resources have been released after a terminal state.
    """
    session = await get_authorized_session(db, session_id, current_user)
    await db.commit()
    return session


@router.delete("/{session_id}", responses={200: {"model": MessageResponse}})
async def terminate_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Close an authorized session, preserving an existing terminal state.

    The response confirms the database state change. Network cleanup is asynchronous;
    poll the session detail until cleaned_at is set before reusing the peers.
    """
    await lock_actor(db, current_user)
    await get_authorized_session(db, session_id, current_user)
    closed = await close_session(db, session_id, actor_id=current_user.id)
    if not closed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return {"message": "Session terminated"}


@router.post("/expire-stale", responses={200: {"model": ExpirationResponse}})
async def expire_stale(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)
):
    """Expire stale sessions past their expiry time"""
    count = await expire_stale_sessions(db)
    return {"expired": count}
