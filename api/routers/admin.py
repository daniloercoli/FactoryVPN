"""Administrative operations filtered by capability and historical resource scope."""

from api.services.session_events import terminal_event
from api.routers.session_diagnostics import event_filters
from api.services.csv_export import csv_response, CSV_RESPONSES, EXPORT_LIMIT
import json

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Header
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.user import User
from api.models.session import Session
from api.models.gateway import Gateway
from api.models.device import Device
from api.models.audit_log import AuditLog
from api.permissions import Permission
from api.routers.auth import require_permission
from api.routers.admin_users import router as users_router
from api.schemas.admin import AuditLogResponse, StatsResponse
from api.schemas.client import (
    SessionSummaryResponse,
    AdminSessionListResponse,
    SessionStatus,
    PAGINATION_RESPONSES,
    AdminSessionResponse,
    AdminSessionCloseResponse,
    AdminGatewaySummaryResponse,
    AdminGatewayCatalogResponse,
    AdminGatewayDetailResponse,
    AdminDeviceResponse,
    AdminDeviceCreatedResponse,
)
from api.schemas.device import DeviceInput, DeviceType
from api.services.device_inventory import inventory, device_payload
from api.schemas.group import GatewayGroupRequest
from api.schemas.gateway import (
    GatewayUpdateRequest,
    ActivationMode,
    GatewayStatus,
    GatewayAvailability,
)
from api.services.vpn_catalog import gateway_list, gateway_detail, session_list
from api.services.access_service import (
    check_permission,
    gateway_for_user,
    group_for_user,
    lock_actor,
    record_event,
    scope,
    users_scope,
)
from api.services.lifecycle import occupied
from api.time_utils import utc_now
from api.services.edit_versions import gateway_revision, device_revision, check_revision

router = APIRouter()
router.include_router(users_router)


def session_summary(session):
    return {field: getattr(session, field) for field in SessionSummaryResponse.model_fields}


def gateway_summary(gateway):
    return {
        field: gateway_revision(gateway) if field == "revision" else getattr(gateway, field)
        for field in AdminGatewaySummaryResponse.model_fields
    }


async def scoped_session(db, identifier, actor, *, lock=False):
    query = select(Session).where(Session.id == identifier, scope(Session.group_id, actor))
    if lock:
        query = query.with_for_update()
    session = await db.scalar(query)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


@router.get(
    "/sessions", response_model=list[AdminSessionListResponse], responses=PAGINATION_RESPONSES
)
async def list_sessions(
    response: Response,
    q: str | None = Query(None, max_length=100),
    status_filter: SessionStatus | None = None,
    group_id: UUID | None = None,
    gateway_id: UUID | None = None,
    technician_id: UUID | None = None,
    activation_mode: ActivationMode | None = None,
    reserved: bool | None = None,
    cleanup_pending: bool | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.SESSIONS_MANAGE)),
):
    """Search all sessions in delegated groups, preserving historical visibility."""
    items, total = await session_list(
        db,
        actor,
        own_only=False,
        limit=limit,
        offset=offset,
        q=q,
        group_id=group_id,
        gateway_id=gateway_id,
        technician_id=technician_id,
        status_filter=status_filter,
        activation_mode=activation_mode,
        reserved=reserved,
        cleanup=cleanup_pending,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/sessions/{session_id}", response_model=AdminSessionResponse)
async def get_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.SESSIONS_MANAGE)),
):
    session = await scoped_session(db, session_id, actor)
    return {
        **session_summary(session),
        "result": session.result,
        "error_reason": session.error_reason,
    }


@router.delete("/sessions/{session_id}", response_model=AdminSessionCloseResponse)
async def close_session(
    session_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.SESSIONS_MANAGE)),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.SESSIONS_MANAGE)
    session = await scoped_session(db, session_id, actor, lock=True)
    if session.status in ("REQUESTED", "ACTIVE"):
        session.status = "CLOSED"
        session.ended_at = utc_now()
        terminal_event(db, session, actor_id=actor.id, reason="administrative_close")
        record_event(
            db,
            "session_closed",
            actor.id,
            group_id=session.group_id,
            session_id=session.id,
            gateway_id=session.gateway_id,
            details={"reason": "administrative_close"},
        )
        await db.commit()
    return {"id": session.id, "status": session.status, "ended_at": session.ended_at}


@router.get(
    "/gateways", response_model=list[AdminGatewayCatalogResponse], responses=PAGINATION_RESPONSES
)
async def list_gateways(
    response: Response,
    q: str | None = Query(None, max_length=100),
    group_id: UUID | None = None,
    status_filter: GatewayStatus | None = None,
    activation_mode: ActivationMode | None = None,
    availability: GatewayAvailability | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
):
    items, total = await gateway_list(
        db,
        actor,
        administrative=True,
        limit=limit,
        offset=offset,
        q=q,
        group_id=group_id,
        status_filter=status_filter,
        activation_mode=activation_mode,
        availability=availability,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/gateways/{gateway_id}", response_model=AdminGatewayDetailResponse)
async def get_gateway(
    gateway_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
):
    return await gateway_detail(db, actor, gateway_id, administrative=True)


@router.put("/gateways/{gateway_id}/group", response_model=AdminGatewaySummaryResponse)
async def assign_gateway_group(
    gateway_id: UUID,
    request: GatewayGroupRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, gateway_id, actor)
    check_revision(actor, if_match, gateway_revision(gateway))
    if request.group_id is not None:
        await group_for_user(db, request.group_id, actor)
    elif not actor.is_admin:
        raise HTTPException(403, "Only global administrators can leave gateways unassigned")
    if gateway.group_id != request.group_id:
        if await db.scalar(
            select(Session.id).where(Session.gateway_id == gateway.id, occupied()).limit(1)
        ):
            raise HTTPException(409, "Close sessions and wait for network cleanup first")
        record_event(
            db, "gateway_unassigned", actor.id, group_id=gateway.group_id, gateway_id=gateway.id
        )
        from api.services.provisioning import invalidate_peer

        await invalidate_peer(db, actor, "gateway", gateway)
        gateway.group_id = request.group_id
        record_event(
            db, "gateway_assigned", actor.id, group_id=gateway.group_id, gateway_id=gateway.id
        )
        await db.commit()
    return gateway_summary(gateway)


@router.patch("/gateways/{gateway_id}", response_model=AdminGatewaySummaryResponse)
async def update_gateway(
    gateway_id: UUID,
    request: GatewayUpdateRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    """Set or clear a display name; machine registration never overwrites it."""
    await lock_actor(db, actor)
    check_permission(actor, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, gateway_id, actor)
    check_revision(actor, if_match, gateway_revision(gateway))
    gateway.display_name = request.display_name
    record_event(db, "gateway_updated", actor.id, group_id=gateway.group_id, gateway_id=gateway.id)
    await db.commit()
    return gateway_summary(gateway)


@router.delete("/gateways/{gateway_id}", status_code=204)
async def delete_gateway(
    gateway_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, gateway_id, actor)
    check_revision(actor, if_match, gateway_revision(gateway))
    if await db.scalar(
        select(Session.id).where(Session.gateway_id == gateway.id, occupied()).limit(1)
    ):
        raise HTTPException(409, "Close sessions and wait for network cleanup first")
    from api.services.provisioning import invalidate_peer

    await invalidate_peer(db, actor, "gateway", gateway)
    await db.execute(delete(Device).where(Device.gateway_id == gateway.id))
    await db.execute(delete(Gateway).where(Gateway.id == gateway.id))
    record_event(db, "gateway_deleted", actor.id, group_id=gateway.group_id, gateway_id=gateway.id)
    await db.commit()


@router.get(
    "/gateways/{gateway_id}/devices",
    response_model=list[AdminDeviceResponse],
    responses=PAGINATION_RESPONSES,
)
async def list_devices(
    gateway_id: UUID,
    response: Response,
    q: str | None = Query(None, max_length=100),
    device_type: DeviceType | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
):
    items, total = await inventory(
        db, actor, gateway_id, q=q, device_type=device_type, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.post(
    "/gateways/{gateway_id}/devices", status_code=201, response_model=AdminDeviceCreatedResponse
)
async def add_device(
    gateway_id: UUID,
    request: DeviceInput,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, gateway_id, actor)
    check_revision(actor, if_match, gateway_revision(gateway))
    device = Device(gateway_id=gateway_id, **request.model_dump())
    db.add(device)
    await db.flush()
    record_event(
        db,
        "device_added",
        actor.id,
        group_id=gateway.group_id,
        gateway_id=gateway_id,
        details={"device_id": str(device.id)},
    )
    await db.commit()
    return {"id": device.id, "status": device.status}


@router.put("/gateways/{gateway_id}/devices/{device_id}", response_model=AdminDeviceResponse)
async def update_device(
    gateway_id: UUID,
    device_id: UUID,
    request: DeviceInput,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, gateway_id, actor)
    device = await db.scalar(
        select(Device).where(Device.id == device_id, Device.gateway_id == gateway_id)
    )
    if device is None:
        raise HTTPException(404, "Device not found")
    check_revision(actor, if_match, device_revision(device, gateway))
    changed = [
        field for field, value in request.model_dump().items() if getattr(device, field) != value
    ]
    for field, value in request.model_dump().items():
        setattr(device, field, value)
    record_event(
        db,
        "device_updated",
        actor.id,
        group_id=gateway.group_id,
        gateway_id=gateway_id,
        details={"device_id": str(device.id), "fields": changed},
    )
    await db.commit()
    return device_payload(device, gateway)


@router.delete("/gateways/{gateway_id}/devices/{device_id}", status_code=204)
async def delete_device(
    gateway_id: UUID,
    device_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, gateway_id, actor)
    device = await db.scalar(
        select(Device).where(Device.id == device_id, Device.gateway_id == gateway_id)
    )
    if device is None:
        raise HTTPException(404, "Device not found")
    check_revision(actor, if_match, device_revision(device, gateway))
    await db.delete(device)
    record_event(
        db,
        "device_deleted",
        actor.id,
        group_id=gateway.group_id,
        gateway_id=gateway_id,
        details={"device_id": str(device_id)},
    )
    await db.commit()


def audit_query(actor, filters):
    query = select(AuditLog).where(scope(AuditLog.group_id, actor))
    for field in ("group_id", "gateway_id", "session_id", "event_type"):
        if filters[field] is not None:
            query = query.where(getattr(AuditLog, field) == filters[field])
    if filters["since"]:
        query = query.where(AuditLog.timestamp >= filters["since"])
    if filters["until"]:
        query = query.where(AuditLog.timestamp <= filters["until"])
    return query


def audit_payload(row):
    return {
        field: (
            str(getattr(row, field))
            if getattr(row, field) is not None
            and field in ("user_id", "gateway_id", "session_id", "ip_address")
            else getattr(row, field)
        )
        for field in AuditLogResponse.model_fields
    }


@router.get("/audit", response_model=list[AuditLogResponse])
async def list_audit_logs(
    response: Response,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    filters=Depends(event_filters),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.AUDIT_READ)),
):
    query = audit_query(actor, filters)
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(query.subquery()))
    )
    rows = (
        await db.scalars(
            query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [audit_payload(row) for row in rows]


@router.get("/audit.csv", response_class=Response, responses=CSV_RESPONSES)
async def export_audit(
    filters=Depends(event_filters),
    db=Depends(get_db),
    actor=Depends(require_permission(Permission.AUDIT_READ)),
):
    rows = (
        await db.scalars(
            audit_query(actor, filters)
            .order_by(AuditLog.timestamp, AuditLog.id)
            .limit(EXPORT_LIMIT + 1)
        )
    ).all()
    fields = list(AuditLogResponse.model_fields)
    return csv_response(
        fields,
        [
            [
                (
                    json.dumps(row.details, ensure_ascii=False)
                    if key == "details"
                    else (
                        (row.timestamp.isoformat() if row.timestamp else None)
                        if key == "timestamp"
                        else getattr(row, key)
                    )
                )
                for key in fields
            ]
            for row in rows
        ],
        "factoryvpn-audit.csv",
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.AUDIT_READ)),
):
    async def count(model, *conditions):
        boundary = users_scope(actor) if model is User else scope(model.group_id, actor)
        return (
            await db.scalar(select(func.count()).select_from(model).where(boundary, *conditions))
            or 0
        )

    return {
        "users_total": await count(User),
        "users_active": await count(User, User.is_active.is_(True)),
        "users_admin": await count(User, User.is_admin.is_(True)),
        "gateways_total": await count(Gateway),
        "gateways_active": await count(Gateway, Gateway.status == "ACTIVE"),
        "sessions_total": await count(Session),
        **{
            f"sessions_{state.lower()}": await count(Session, Session.status == state)
            for state in ("ACTIVE", "REQUESTED", "CLOSED", "EXPIRED", "FAILED")
        },
    }
