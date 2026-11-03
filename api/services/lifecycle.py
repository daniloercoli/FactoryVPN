"""Allocation and validated lifecycle snapshots; PostgreSQL serializes reservations."""

from api.services.session_events import event, terminal_event

from datetime import timedelta
from ipaddress import IPv4Address

from fastapi import HTTPException
from sqlalchemy import or_, select, text

from api.services.credentials import gateway_configured, active_certificate
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.user import User
from api.time_utils import utc_now
from factoryvpn_common.network import SessionNetwork
from factoryvpn_common.protocol import peer_name
from api.services.access_service import can_connect

LIVE = ("REQUESTED", "ACTIVE")
TERMINAL = ("CLOSED", "EXPIRED", "FAILED")


def occupied():
    return or_(
        Session.status.in_(LIVE), (Session.network_slot.is_not(None) & Session.cleaned_at.is_(None))
    )


async def allocation_lock(db, *, maintenance=False):
    # Shared by gateway registration and session creation, including concurrent API replicas.
    await db.execute(text("SELECT pg_advisory_xact_lock(71702026)"))
    if not maintenance:
        from api.services.backup_access import maintenance_active, cleanup_request

        if not cleanup_request.get() and maintenance_active():
            from fastapi import HTTPException

            raise HTTPException(503, "Installation maintenance in progress")


async def assign_ip(db, entity):
    if entity.vpn_ip:
        return entity.vpn_ip
    model = Gateway if isinstance(entity, Gateway) else User
    used = set(
        (await db.execute(select(model.vpn_ip).where(model.vpn_ip.is_not(None)))).scalars().all()
    )
    base = int(IPv4Address("10.100.0.0" if model is Gateway else "10.100.128.0"))
    for offset in range(2, 32767):
        address = str(IPv4Address(base + offset))
        if address not in used:
            entity.vpn_ip = address
            return address
    raise HTTPException(503, "VPN address pool exhausted")


def network_for(session):
    return SessionNetwork(
        str(session.id),
        session.network_slot,
        session.technician_vpn_ip,
        session.gateway_vpn_ip,
        session.lan_cidr,
    )


def lease_seconds(session):
    if not session.expires_at:
        return 0
    deadline = min(
        session.expires_at,
        getattr(session, "_sso_lease_deadline", session.expires_at),
        getattr(session, "_web_lease_deadline", session.expires_at),
    )
    return max(0, min(30, int((deadline - utc_now()).total_seconds())))


def gateway_ready(session):
    return bool(
        session.gateway_ready_at and session.gateway_ready_at > utc_now() - timedelta(seconds=30)
    )


def activation_confirmed(session):
    return session.activation_mode == "wifi" or session.activation_received_at is not None


async def validate_live(db, session, *, require_activation=True):
    """Refresh and validate under a row lock held until the caller commits.

    Callers may have loaded the session before a concurrent revocation. Never
    trust that cached state or release the lock before their subsequent writes.
    Rejection paths must also commit any terminal transition made here.
    """
    await db.refresh(session, with_for_update=True)
    if session.status not in LIVE:
        return False
    reason = None
    if lease_seconds(session) == 0:
        session.status = "EXPIRED"
        reason = "Session expired"
    else:
        user = await db.get(User, session.technician_id) if session.technician_id else None
        gateway = await db.get(Gateway, session.gateway_id) if session.gateway_id else None
        if (
            not user
            or not user.is_active
            or not gateway
            or not await gateway_configured(db, gateway.id)
            or not await active_certificate(db, "technician", user.id)
        ):
            session.status = "FAILED"
            reason = "Peer disabled or credentials revoked"
        elif session.group_id != gateway.group_id or not await can_connect(db, user, gateway):
            session.status = "FAILED"
            reason = "Gateway access permission revoked"
        if not reason and getattr(session, "sso_session_id", None):
            from api.services.sso_service import valid_session, session_deadline

            auth_session = await valid_session(db, session.sso_session_id, user)
            if auth_session is None:
                session.status = "FAILED"
                reason = "SSO session expired or authorization revoked"
            else:
                session._sso_lease_deadline = session_deadline(auth_session)
        elif not reason and user.auth_mode == "sso":
            session.status = "FAILED"
            reason = "SSO authentication required"
        if not reason and getattr(session, "web_session_id", None):
            from api.services.web_session_service import valid_web_session

            browser = await valid_web_session(db, session.web_session_id, user)
            if browser is None or browser.sso_session_id != session.sso_session_id:
                session.status = "FAILED"
                reason = "Portal session expired or authorization revoked"
            else:
                session._web_lease_deadline = browser.expires_at
    if reason:
        session.ended_at = utc_now()
        session.error_reason = reason
        terminal_event(db, session)
        await db.flush()
        return False
    if (
        session.status == "ACTIVE"
        and session.worker_seen_at
        and session.worker_seen_at < utc_now() - timedelta(seconds=30)
    ):
        session.status = "REQUESTED"
        session.network_ready_at = None
        event(db, session, "worker_stale")
        await db.flush()
    return not require_activation or activation_confirmed(session)


def work_item(session):
    return {
        "session_id": str(session.id),
        "status": session.status,
        "network": network_for(session).to_dict(),
        "technician_cn": peer_name("technician", session.technician_id),
        "gateway_cn": peer_name("gateway", session.gateway_id),
        "gateway_ready": gateway_ready(session),
        "lease_seconds": lease_seconds(session),
        "activation_confirmed": activation_confirmed(session),
    }
