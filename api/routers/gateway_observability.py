"""Group-scoped component inventory and technical logs."""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime
from sqlalchemy import func, select
from api.db.session import get_db
from api.models.gateway import Gateway
from api.models.gateway_observability import GatewayTechnicalLog as Log
from api.permissions import Permission
from api.routers.auth import get_current_user, require_permission
from api.schemas.client import PAGINATION_RESPONSES
from api.schemas.diagnostics import ObservationResponse
from api.services.machine_auth import get_gateway_identity, require_worker
from api.services.access_service import gateway_for_user
from api.services.lifecycle import allocation_lock
from api.services import gateway_observability as service
from api.services.csv_export import csv_response, CSV_RESPONSES, EXPORT_LIMIT
from factoryvpn_common.observability import (
    SystemReport,
    SystemView,
    EventBatch,
    BatchResult,
    TechnicalLogView,
    LogSource,
)


class ObservationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Validation must not echo rejected secrets or raw command output.
                return JSONResponse(
                    status_code=422, content={"detail": "Invalid observation or filters"}
                )

        return validated


router = APIRouter(route_class=ObservationRoute)


@router.post("/gateways/system-info", response_model=ObservationResponse)
async def report_system(
    report: SystemReport, identity=Depends(get_gateway_identity), db=Depends(get_db)
):
    gateway = await db.scalar(select(Gateway).where(Gateway.id == UUID(identity)).with_for_update())
    if gateway is None:
        raise HTTPException(404, "Gateway not found")
    recorded = await service.record_system(db, gateway, report)
    await db.commit()
    return {"recorded": recorded}


@router.get("/gateways/{gateway_id}/system-info", response_model=SystemView)
async def system_info(gateway_id: UUID, actor=Depends(get_current_user), db=Depends(get_db)):
    await gateway_for_user(db, gateway_id, actor)
    return await service.system_view(db, gateway_id, actor)


@router.post("/gateways/technical-logs", response_model=BatchResult)
async def report_logs(
    batch: EventBatch, identity=Depends(get_gateway_identity), db=Depends(get_db)
):
    gateway = await db.get(Gateway, UUID(identity))
    if gateway is None:
        raise HTTPException(404, "Gateway not found")
    result = await service.record_logs(db, gateway, batch.events, "agent")
    await db.commit()
    return result


@router.post(
    "/internal/gateways/{gateway_id}/technical-logs",
    response_model=BatchResult,
    dependencies=[Depends(require_worker)],
)
async def report_worker_logs(gateway_id: UUID, batch: EventBatch, db=Depends(get_db)):
    await allocation_lock(db)
    gateway = await db.get(Gateway, gateway_id)
    if gateway is None:
        raise HTTPException(404, "Gateway not found")
    result = await service.record_logs(db, gateway, batch.events, "worker")
    await db.commit()
    return result


def log_filters(
    source: LogSource | None = None,
    severity: Literal["info", "warning", "error"] | None = None,
    component: str | None = Query(None, max_length=16),
    code: str | None = Query(None, max_length=40),
    session_id: UUID | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    q: str | None = Query(None, max_length=100),
):
    if since and until and since > until:
        raise HTTPException(422, "Invalid date interval")
    return dict(
        source=source,
        severity=severity,
        component=component,
        code=code,
        session_id=session_id,
        since=since,
        until=until,
        q=q,
    )


@router.get(
    "/gateways/{gateway_id}/technical-logs",
    response_model=list[TechnicalLogView],
    responses=PAGINATION_RESPONSES,
)
async def list_logs(
    gateway_id: UUID,
    response: Response,
    filters=Depends(log_filters),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    actor=Depends(require_permission(Permission.LOGS_READ)),
    db=Depends(get_db),
):
    await gateway_for_user(db, gateway_id, actor)
    query = service.logs_query(actor, gateway_id, filters)
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(query.subquery()))
    )
    rows = (
        await db.scalars(
            query.order_by(Log.received_at.desc(), Log.id.desc()).offset(offset).limit(limit)
        )
    ).all()
    return [service.log_view(row) for row in rows]


@router.get(
    "/gateways/{gateway_id}/technical-logs.csv", response_class=Response, responses=CSV_RESPONSES
)
async def export_logs(
    gateway_id: UUID,
    filters=Depends(log_filters),
    actor=Depends(require_permission(Permission.LOGS_READ)),
    db=Depends(get_db),
):
    await gateway_for_user(db, gateway_id, actor)
    rows = (
        await db.scalars(
            service.logs_query(actor, gateway_id, filters)
            .order_by(Log.received_at, Log.id)
            .limit(EXPORT_LIMIT + 1)
        )
    ).all()
    return csv_response(
        [
            "received_at_utc",
            "observed_at_utc",
            "first_observed_at_utc",
            "source",
            "component",
            "severity",
            "code",
            "message",
            "session_id",
            "occurrences",
            "parameters",
        ],
        [
            [
                r.received_at.isoformat(),
                r.observed_at.isoformat(),
                r.first_observed_at.isoformat(),
                r.source,
                r.component,
                r.severity,
                r.code,
                r.message,
                r.session_id,
                r.occurrences,
                service.log_view(r).parameters.model_dump_json(exclude_none=True),
            ]
            for r in rows
        ],
        "factoryvpn-gateway-technical-logs.csv",
    )
