"""Scoped VPN discovery without reserving peers or exposing other sessions."""

from uuid import UUID
import re
from datetime import datetime, timedelta
from api.services.credentials import configured_gateway, active_certificate

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select

from api.models.gateway import Gateway
from api.models.session import Session
from api.models.user import User
from api.permissions import Permission
from api.schemas.client import AdminGatewaySummaryResponse, SessionSummaryResponse
from api.schemas.gateway import GatewayResponse
from api.services.access_service import check_permission, has_permission, scope, users_scope
from api.services.diagnostics import project
from api.services.edit_versions import gateway_revision
from api.services.lifecycle import LIVE, TERMINAL, occupied


def identifier_filter(column, value):
    try:
        return column == UUID(value)
    except ValueError:
        return False


def gateway_search(value):
    value = value.strip()
    # Accetta MAC con due punti, trattini, punti o senza separatori, anche parziali.
    compact_mac = re.sub(r"[:.\s-]", "", value)
    mac = func.replace(Gateway.mac_address, ":", "")
    return or_(
        Gateway.display_name.icontains(value, autoescape=True),
        Gateway.mac_address.icontains(value, autoescape=True),
        (
            mac.icontains(compact_mac, autoescape=True)
            if re.fullmatch(r"[0-9a-fA-F]{2,12}", compact_mac)
            else False
        ),
        Gateway.serial_number.icontains(value, autoescape=True),
        Gateway.imei.icontains(value, autoescape=True),
        Gateway.iccid.icontains(value, autoescape=True),
        Gateway.phone_number.icontains(value, autoescape=True),
        identifier_filter(Gateway.id, value),
    )


def gateway_query(
    actor, *, q=None, group_id=None, status_filter=None, activation_mode=None, availability=None
):
    reservation = select(Session.gateway_id, Session.status).where(occupied()).subquery()
    configured = and_(
        Gateway.group_id.is_not(None),
        configured_gateway(Gateway.id),
        or_(
            Gateway.activation_mode == "wifi",
            func.nullif(func.trim(Gateway.phone_number), "").is_not(None),
        ),
    )
    state = case(
        (reservation.c.status.in_(LIVE), "busy"),
        (reservation.c.gateway_id.is_not(None), "cleanup_pending"),
        (~configured, "unconfigured"),
        else_="available",
    )
    query = (
        select(Gateway, state.label("availability"))
        .outerjoin(reservation, reservation.c.gateway_id == Gateway.id)
        .where(scope(Gateway.group_id, actor))
    )
    if q:
        query = query.where(gateway_search(q))
    for column, value in (
        (Gateway.group_id, group_id),
        (Gateway.status, status_filter),
        (Gateway.activation_mode, activation_mode),
        (state, availability),
    ):
        if value is not None:
            query = query.where(column == value)
    return query


async def technician_reservation(db, actor):
    # Only disclose a blocking condition, never a session ID from a lost group.
    return await db.scalar(
        select(Session.status).where(Session.technician_id == actor.id, occupied()).limit(1)
    )


def gateway_payload(
    gateway, availability, actor, technician_status, *, administrative=False, profile_ready=True
):
    model = AdminGatewaySummaryResponse if administrative else GatewayResponse
    payload = {
        field: getattr(gateway, field) for field in model.model_fields if field != "revision"
    }
    if administrative:
        payload["revision"] = gateway_revision(gateway)
    else:
        payload["id"] = str(gateway.id)
        payload["last_seen"] = gateway.last_seen.isoformat() if gateway.last_seen else None
    if not has_permission(actor, Permission.CONNECT):
        reason = "permission_required"
    elif availability != "available":
        reason = "gateway_" + availability
    elif not profile_ready:
        reason = "personal_profile_required"
    elif technician_status:
        reason = "technician_busy" if technician_status in LIVE else "technician_cleanup_pending"
    else:
        reason = None
    return {
        **payload,
        "availability": availability,
        "can_start": reason is None,
        "start_block_reason": reason,
    }


async def gateway_list(db, actor, *, limit, offset, administrative=False, **filters):
    query = gateway_query(actor, **filters)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    ordering = (Gateway.created_at.desc(), Gateway.id) if administrative else (Gateway.id,)
    rows = (await db.execute(query.order_by(*ordering).offset(offset).limit(limit))).all()
    technician_status = await technician_reservation(db, actor) if rows else None
    profile_ready = bool(await active_certificate(db, "technician", actor.id)) if rows else False
    return [
        gateway_payload(
            gateway,
            state,
            actor,
            technician_status,
            administrative=administrative,
            profile_ready=profile_ready,
        )
        for gateway, state in rows
    ], total


async def gateway_detail(db, actor, identifier, *, administrative=False):
    row = (await db.execute(gateway_query(actor).where(Gateway.id == identifier))).first()
    if row is None:
        raise HTTPException(404, "Gateway not found")
    gateway, state = row
    payload = gateway_payload(
        gateway,
        state,
        actor,
        await technician_reservation(db, actor),
        administrative=administrative,
        profile_ready=bool(await active_certificate(db, "technician", actor.id)),
    )
    if administrative:
        payload["metadata"] = gateway.extra_metadata
    else:
        # Explicit public projection: never return arbitrary machine metadata.
        query = select(Session).where(
            Session.gateway_id == gateway.id, occupied(), scope(Session.group_id, actor)
        )
        if not has_permission(actor, Permission.SESSIONS_MANAGE):
            query = query.where(Session.technician_id == actor.id)
        current = await db.scalar(query)
        payload.update(
            revision=gateway_revision(gateway),
            serial_number=gateway.serial_number,
            created_at=gateway.created_at,
            current_session=(
                {
                    "id": current.id,
                    "status": current.status,
                    "activation_mode": current.activation_mode,
                    "activation_received_at": current.activation_received_at,
                    "expires_at": current.expires_at,
                    "cleanup_pending": current.status in TERMINAL,
                }
                if current
                else None
            ),
        )
    return payload


def cleanup_pending():
    return and_(
        Session.status.in_(TERMINAL),
        Session.cleaned_at.is_(None),
    )


def supervision_payload(session, username, full_name, actor):
    worker = (session.diagnostic_state or {}).get("worker", {})
    valid_until = None
    if (
        worker.get("observed_at")
        and worker.get("received_at")
        and worker.get("code") != "vpn_unavailable"
    ):
        valid_until = min(
            datetime.fromisoformat(worker["observed_at"]),
            datetime.fromisoformat(worker["received_at"]),
        ) + timedelta(seconds=30)
    if session.expires_at and valid_until:
        valid_until = min(valid_until, session.expires_at)
    return {
        "technician_username": username,
        "technician_full_name": full_name,
        "technician_manageable": bool(username and has_permission(actor, Permission.USERS_MANAGE)),
        "technician_vpn_ip": session.technician_vpn_ip,
        "gateway_vpn_ip": session.gateway_vpn_ip,
        "observation_valid_until": valid_until,
        "diagnostics": project(session),
    }


async def session_list(
    db,
    actor,
    *,
    limit,
    offset,
    own_only=True,
    q=None,
    group_id=None,
    gateway_id=None,
    technician_id=None,
    status_filter=None,
    activation_mode=None,
    reserved=None,
    cleanup=None,
):
    if not own_only:
        check_permission(actor, Permission.SESSIONS_MANAGE)
    # Historical session scope must not reveal current metadata after a gateway
    # moves outside the caller's groups. Keep the session, mask the joined gateway.
    query = (
        select(Session, Gateway.display_name, Gateway.mac_address)
        .select_from(Session)
        .outerjoin(Gateway, and_(Gateway.id == Session.gateway_id, scope(Gateway.group_id, actor)))
        .where(scope(Session.group_id, actor))
    )
    if own_only:
        query = query.where(Session.technician_id == actor.id)
    else:
        # Expose only a minimal identity, within the same boundary as user management.
        # A moved, shared or global account must not leak through search/counts.
        query = query.outerjoin(
            User, and_(User.id == Session.technician_id, users_scope(actor))
        ).add_columns(User.username, User.full_name)
    if q:
        search = [identifier_filter(Session.id, q), gateway_search(q)]
        if not own_only:
            search.extend(
                (
                    User.username.icontains(q, autoescape=True),
                    User.full_name.icontains(q, autoescape=True),
                )
            )
        query = query.where(or_(*search))
    for column, value in (
        (Session.group_id, group_id),
        (Session.gateway_id, gateway_id),
        (Session.technician_id, technician_id),
        (Session.status, status_filter),
        (Session.activation_mode, activation_mode),
    ):
        if value is not None:
            query = query.where(column == value)
    if reserved is not None:
        query = query.where(occupied() if reserved else ~occupied())
    if cleanup is not None:
        query = query.where(cleanup_pending() if cleanup else ~cleanup_pending())
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(Session.requested_at.desc(), Session.id).offset(offset).limit(limit)
        )
    ).all()
    items = []
    for row in rows:
        session, name, mac = row[:3]
        payload = {
            **{field: getattr(session, field) for field in SessionSummaryResponse.model_fields},
            "cleanup_pending": (session.status in TERMINAL and session.cleaned_at is None),
            "gateway_display_name": name,
            "gateway_mac_address": mac,
        }
        if not own_only:
            payload.update(supervision_payload(session, row[3], row[4], actor))
        items.append(payload)
    return items, total
