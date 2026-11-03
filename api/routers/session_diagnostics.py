"""Session history follows the same ownership and historical scope as VPN detail."""

import json
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import AwareDatetime
from sqlalchemy import func, select
from api.db.session import get_db
from api.models.session import Session
from api.models.session_event import SessionEvent
from api.routers.auth import get_current_user
from api.routers.sessions import get_authorized_session
from api.permissions import Permission
from api.schemas.client import PAGINATION_RESPONSES
from api.schemas.diagnostics import DiagnosticsResponse, SessionEventResponse
from api.services.access_service import scope, has_permission
from api.services.csv_export import CSV_RESPONSES, EXPORT_LIMIT, csv_response
from api.services.diagnostics import project

router = APIRouter()


def event_filters(
    gateway_id: UUID | None = None,
    session_id: UUID | None = None,
    group_id: UUID | None = None,
    event_type: str | None = Query(None, max_length=50),
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
):
    if since and until and since > until:
        raise HTTPException(422, "Invalid date interval")
    return dict(
        gateway_id=gateway_id,
        session_id=session_id,
        group_id=group_id,
        event_type=event_type,
        since=since,
        until=until,
    )


def event_query(actor, filters):
    query = select(SessionEvent).join(Session).where(scope(Session.group_id, actor))
    if not has_permission(actor, Permission.SESSIONS_MANAGE):
        query = query.where(Session.technician_id == actor.id)
    for key, column in (
        ("gateway_id", Session.gateway_id),
        ("session_id", Session.id),
        ("group_id", Session.group_id),
        ("event_type", SessionEvent.event_type),
    ):
        if filters.get(key) is not None:
            query = query.where(column == filters[key])
    if filters.get("since"):
        query = query.where(SessionEvent.timestamp >= filters["since"])
    if filters.get("until"):
        query = query.where(SessionEvent.timestamp <= filters["until"])
    return query


def payload(row):
    return {key: getattr(row, key) for key in SessionEventResponse.model_fields}


@router.get(
    "/session-events", response_model=list[SessionEventResponse], responses=PAGINATION_RESPONSES
)
async def list_events(
    response: Response,
    filters=Depends(event_filters),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    query = event_query(actor, filters)
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(query.subquery()))
    )
    rows = (
        await db.scalars(
            query.order_by(SessionEvent.timestamp.desc(), SessionEvent.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [payload(row) for row in rows]


@router.get("/session-events.csv", response_class=Response, responses=CSV_RESPONSES)
async def export_events(
    filters=Depends(event_filters), db=Depends(get_db), actor=Depends(get_current_user)
):
    rows = (
        await db.scalars(
            event_query(actor, filters)
            .order_by(SessionEvent.timestamp, SessionEvent.id)
            .limit(EXPORT_LIMIT + 1)
        )
    ).all()
    return csv_response(
        ["id", "timestamp_utc", "session_id", "source", "event_type", "actor_id", "details"],
        [
            [
                r.id,
                r.timestamp.isoformat(),
                r.session_id,
                r.source,
                r.event_type,
                r.actor_id,
                json.dumps(r.details, ensure_ascii=False),
            ]
            for r in rows
        ],
        "factoryvpn-session-events.csv",
    )


@router.get("/sessions/{session_id}/diagnostics", response_model=DiagnosticsResponse)
async def diagnostics(session_id: UUID, db=Depends(get_db), actor=Depends(get_current_user)):
    session = await get_authorized_session(db, session_id, actor)
    result = project(session)
    await db.commit()
    return result
