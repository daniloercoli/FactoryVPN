"""Authenticated observations never change VPN state, leases or last_seen."""

from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, func, delete, text, or_
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.gateway_observability import GatewaySystemSnapshot, GatewayTechnicalLog as Log
from api.services.credentials import current_credential
from api.services.access_service import scope, has_permission
from api.permissions import Permission
from api.time_utils import utc_now
from factoryvpn_common.observability import LOG_CODES, SystemView, TechnicalLogView, BatchResult

LOG_LOCK = 76141903
GATEWAY_LIMIT = 10000
INSTANCE_LIMIT = 500000
RATE_LIMIT = 200
RETENTION_DAYS = 30


async def record_system(db, gateway, report):
    now = utc_now()
    if report.observed_at > now + timedelta(seconds=30):
        raise HTTPException(422, "Observation timestamp is in the future")
    if report.observed_at < now - timedelta(minutes=5):
        return False
    credential = await current_credential(db, gateway.id)
    if credential is None:
        raise HTTPException(401, "Invalid gateway credentials")
    row = await db.get(GatewaySystemSnapshot, gateway.id)
    if row and row.credential_id == credential.id:
        if report.report_id == row.report_id or report.observed_at <= row.observed_at:
            return False
        components = dict(row.payload["components"])
    else:
        components = {}
    for name, value in report.components.items():
        # L'ultima lettura valida alimenta anche l'anagrafica ricercabile.
        # Dati assenti o letture fallite non cancellano gli identificativi noti.
        if name == "modem" and value.state == "reported":
            for field in ("imei", "iccid"):
                detected = getattr(value, field)
                if detected:
                    setattr(gateway, field, detected)
        previous = components.get(name, {})
        components[name] = {
            "latest": value.model_dump(mode="json", exclude_none=True),
            "observed_at": report.observed_at.isoformat(),
            "last_success": (
                value.model_dump(mode="json", exclude_none=True)
                if value.state == "reported"
                else previous.get("last_success")
            ),
            "last_success_at": (
                report.observed_at.isoformat()
                if value.state == "reported"
                else previous.get("last_success_at")
            ),
        }
    if row is None:
        row = GatewaySystemSnapshot(gateway_id=gateway.id)
        db.add(row)
    row.credential_id, row.report_id = credential.id, report.report_id
    row.observed_at, row.received_at = report.observed_at, now
    row.payload = {"components": components, "logs_supported": report.logs_supported}
    return True


async def system_view(db, identifier, actor):
    # Scope remains part of the data query, including concurrent transfers.
    from api.models.provisioning import GatewayCredential

    query = (
        select(GatewaySystemSnapshot)
        .join(Gateway)
        .join(GatewayCredential, GatewayCredential.id == GatewaySystemSnapshot.credential_id)
        .where(
            Gateway.id == identifier,
            scope(Gateway.group_id, actor),
            GatewayCredential.active.is_(True),
            GatewayCredential.revoked_at.is_(None),
        )
    )
    row = await db.scalar(query)
    if row is None:
        return SystemView(gateway_id=identifier)
    return SystemView(gateway_id=identifier, received_at=row.received_at, **row.payload)


async def log_lock(db):
    if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOG_LOCK}):
        raise HTTPException(429, "Technical log writer busy", headers={"Retry-After": "15"})


async def record_logs(db, gateway, events, source, *, expected_group=None):
    now = utc_now()
    await log_lock(db)
    credential = await current_credential(db, gateway.id) if source == "agent" else None
    if source == "agent" and credential is None:
        raise HTTPException(401, "Invalid gateway credentials")
    if source == "api" and gateway.group_id != expected_group:
        return BatchResult(accepted=0, duplicates=0, discarded=len(events))

    def event_key(event):
        return (credential.id if credential else event.session_id, event.event_id)

    known = set(
        (
            await db.execute(
                select(Log.context_id, Log.event_id).where(
                    Log.gateway_id == gateway.id,
                    Log.source == source,
                    Log.event_id.in_([e.event_id for e in events]),
                )
            )
        ).all()
    )
    recent = await db.scalar(
        select(func.count())
        .select_from(Log)
        .where(Log.gateway_id == gateway.id, Log.received_at >= now - timedelta(minutes=1))
    )
    new_count = len({event_key(e) for e in events} - known)
    if recent + new_count > RATE_LIMIT:
        raise HTTPException(429, "Technical log rate exceeded", headers={"Retry-After": "60"})
    accepted = duplicates = discarded = 0
    for event in events:
        if event_key(event) in known:
            duplicates += 1
            continue
        component, severity, _, sources, message = LOG_CODES[event.code]
        if source not in sources:
            raise HTTPException(422, "Code not valid for this producer")
        if event.observed_at > now + timedelta(
            seconds=30
        ) or event.first_observed_at < now - timedelta(hours=24):
            discarded += 1
            continue
        session = await db.get(Session, event.session_id) if event.session_id else None
        if event.session_id and (session is None or session.gateway_id != gateway.id):
            # A deleted session can no longer be attributed; never downgrade to generic.
            discarded += 1
            continue
        if source == "agent" and session and session.group_id != gateway.group_id:
            discarded += 1
            continue
        if source == "worker" and session is None:
            raise HTTPException(422, "Worker log requires a session")
        if session and event.first_observed_at < session.requested_at - timedelta(seconds=30):
            discarded += 1
            continue
        db.add(
            Log(
                gateway_id=gateway.id,
                group_id=session.group_id if session else gateway.group_id,
                session_id=event.session_id,
                technician_id=session.technician_id if session else None,
                context_id=credential.id if credential else session.id,
                source=source,
                event_id=event.event_id,
                code=event.code,
                component=component,
                severity=severity,
                message=message,
                observed_at=event.observed_at,
                first_observed_at=event.first_observed_at,
                received_at=now,
                occurrences=event.occurrences,
                parameters=event.parameters.model_dump(exclude_none=True),
            )
        )
        known.add(event_key(event))
        accepted += 1
    await db.flush()
    # Hard ceilings also hold when the background maintenance job is unavailable.
    for condition, maximum in (
        (Log.gateway_id == gateway.id, GATEWAY_LIMIT),
        (True, INSTANCE_LIMIT),
    ):
        count = await db.scalar(select(func.count()).select_from(Log).where(condition))
        overflow = max(0, count - maximum)
        if overflow:
            victims = (
                select(Log.id).where(condition).order_by(Log.received_at, Log.id).limit(overflow)
            )
            await db.execute(delete(Log).where(Log.id.in_(victims)))
    return BatchResult(accepted=accepted, duplicates=duplicates, discarded=discarded)


async def prune_logs(db):
    await log_lock(db)
    victims = (
        select(Log.id)
        .where(Log.received_at < utc_now() - timedelta(days=RETENTION_DAYS))
        .order_by(Log.received_at, Log.id)
        .limit(1000)
    )
    await db.execute(delete(Log).where(Log.id.in_(victims)))


def logs_query(actor, gateway_id, filters):
    query = (
        select(Log)
        .join(Gateway)
        .where(
            Log.gateway_id == gateway_id,
            scope(Gateway.group_id, actor),
            scope(Log.group_id, actor),
            Log.received_at >= utc_now() - timedelta(days=RETENTION_DAYS),
        )
    )
    if not has_permission(actor, Permission.SESSIONS_MANAGE):
        query = query.where(or_(Log.session_id.is_(None), Log.technician_id == actor.id))
    for name in ("source", "severity", "component", "code", "session_id"):
        if filters.get(name) is not None:
            query = query.where(getattr(Log, name) == filters[name])
    if filters.get("since"):
        query = query.where(Log.received_at >= filters["since"])
    if filters.get("until"):
        query = query.where(Log.received_at <= filters["until"])
    if filters.get("q"):
        query = query.where(
            or_(
                Log.message.icontains(filters["q"], autoescape=True),
                Log.code.icontains(filters["q"], autoescape=True),
            )
        )
    return query


def log_view(row):
    return TechnicalLogView(**{key: getattr(row, key) for key in TechnicalLogView.model_fields})
