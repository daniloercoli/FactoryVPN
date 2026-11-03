from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, Query, Response, HTTPException
from sqlalchemy import select, func, or_
from api.db.session import get_db
from api.models.gateway import Gateway
from api.models.telemetry import TelemetrySnapshot
from api.permissions import Permission
from api.routers.auth import require_permission, require_admin
from api.services.machine_auth import get_gateway_identity, require_worker
from api.services.access_service import scope
from api.services.telemetry import record, project
from api.services.vpn_catalog import identifier_filter
from api.schemas.telemetry import GatewayTelemetry, ServerTelemetry, TelemetryView
from api.schemas.diagnostics import ObservationResponse
from api.schemas.client import PAGINATION_RESPONSES
from api.schemas.telemetry import TelemetryHistory
from api.services.telemetry_history import history
from api.services.access_service import gateway_for_user

router = APIRouter()


@router.post("/gateways/telemetry", response_model=ObservationResponse)
async def gateway_report(
    report: GatewayTelemetry, identity=Depends(get_gateway_identity), db=Depends(get_db)
):
    # Lock against concurrent gateway deletion. Identity is always derived from the key.
    gateway = await db.scalar(select(Gateway).where(Gateway.id == UUID(identity)).with_for_update())
    if gateway is None:
        raise HTTPException(404, "Gateway not found")
    recorded = await record(db, "gateway:" + identity, gateway.id, report)
    await db.commit()
    return {"recorded": recorded}


@router.post(
    "/internal/telemetry/server",
    response_model=ObservationResponse,
    dependencies=[Depends(require_worker)],
)
async def server_report(report: ServerTelemetry, db=Depends(get_db)):
    recorded = await record(db, "server:worker", None, report)
    await db.commit()
    return {"recorded": recorded}


def gateways_query(actor):
    return (
        select(Gateway, TelemetrySnapshot)
        .outerjoin(TelemetrySnapshot, TelemetrySnapshot.gateway_id == Gateway.id)
        .where(scope(Gateway.group_id, actor))
    )


@router.get(
    "/telemetry/gateways", response_model=list[TelemetryView], responses=PAGINATION_RESPONSES
)
async def list_gateways(
    response: Response,
    group_id: UUID | None = None,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    actor=Depends(require_permission(Permission.TELEMETRY_READ)),
    db=Depends(get_db),
):
    query = gateways_query(actor)
    if group_id:
        query = query.where(Gateway.group_id == group_id)
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(
            or_(
                Gateway.display_name.ilike(pattern),
                Gateway.mac_address.ilike(pattern),
                identifier_filter(Gateway.id, q),
            )
        )
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(query.subquery()))
    )
    rows = (
        await db.execute(query.order_by(Gateway.created_at, Gateway.id).limit(limit).offset(offset))
    ).all()
    return [project(row, gateway) for gateway, row in rows]


@router.get("/telemetry/gateways/{gateway_id}", response_model=TelemetryView)
async def gateway_status(
    gateway_id: UUID,
    actor=Depends(require_permission(Permission.TELEMETRY_READ)),
    db=Depends(get_db),
):
    result = (await db.execute(gateways_query(actor).where(Gateway.id == gateway_id))).one_or_none()
    if result is None:
        raise HTTPException(404, "Gateway not found")
    return project(result[1], result[0])


@router.get("/telemetry/server", response_model=TelemetryView)
async def server_status(actor=Depends(require_admin), db=Depends(get_db)):
    row = await db.get(TelemetrySnapshot, "server:worker")
    return project(row, server=True)


@router.get("/telemetry/gateways/{gateway_id}/history", response_model=TelemetryHistory)
async def gateway_history(
    gateway_id: UUID,
    period: Literal["1h", "24h", "7d", "30d"] = "24h",
    actor=Depends(require_permission(Permission.TELEMETRY_READ)),
    db=Depends(get_db),
):
    await gateway_for_user(db, gateway_id, actor)
    return await history(db, actor, gateway_id, period)


@router.get("/telemetry/server/history", response_model=TelemetryHistory)
async def server_history(
    period: Literal["1h", "24h", "7d", "30d"] = "24h",
    actor=Depends(require_admin),
    db=Depends(get_db),
):
    return await history(db, actor, None, period)
