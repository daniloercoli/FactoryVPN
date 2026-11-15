"""Arruolamento iniziale: identità prenotate e consegna indipendente dal browser.

Le mutazioni usano allocation_lock, condiviso con provisioning, gruppi e revoche.
Il registro non sostituisce la riga Gateway e non concede accesso alla VPN.
"""

from datetime import timedelta
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from api.config import settings
from api.models.enrollment import GatewayEnrollment
from api.models.gateway import Gateway
from api.models.group import Group
from api.models.provisioning import ProvisioningJob
from api.models.session import Session
from api.schemas.gateway import GatewayRegisterRequest
from api.services.access_service import record_event
from api.services.credentials import cipher, digest
from api.services.edit_versions import revision
from api.services.lifecycle import occupied
from api.time_utils import utc_now

DATA_FIELDS = ("display_name", "activation_mode", "phone_number", "mac_address", "imei", "iccid")
OPERATIONAL_FIELDS = ("activation_mode", "phone_number", "mac_address")
_UNSET = object()


async def get_enrollment(db, identifier):
    row = await db.get(GatewayEnrollment, identifier, populate_existing=True)
    if row is None:
        raise HTTPException(404, "Enrollment not found")
    return row


async def available_identity(db, identifier, serial_number, *, existing=False):
    """Controllo trasversale: UNIQUE per tabella non basta a prenotare l'identità."""
    for model in (Gateway, GatewayEnrollment):
        if not existing and await db.get(model, identifier):
            raise HTTPException(409, "Gateway identity is already reserved")
        if serial_number and await db.scalar(
            select(model.id).where(model.serial_number == serial_number, model.id != identifier)
        ):
            raise HTTPException(409, "Serial number is already reserved")


def incomplete(data):
    if not data.activation_mode:
        return ["activation_mode"]
    if data.activation_mode == "sms" and not data.phone_number:
        return ["phone_number"]
    if data.activation_mode == "wifi" and not data.mac_address:
        return ["mac_address"]
    return []


async def data_for(db, row):
    gateway = await db.get(Gateway, row.id, populate_existing=True)
    return gateway, gateway or row


async def latest_job(db, row):
    return await db.scalar(
        select(ProvisioningJob)
        .where(ProvisioningJob.kind == "gateway", ProvisioningJob.peer_id == row.id)
        .order_by(ProvisioningJob.created_at.desc(), ProvisioningJob.id.desc())
        .limit(1)
    )


async def response(db, row, *, gateway=_UNSET, job=_UNSET):
    if gateway is _UNSET:
        gateway, _ = await data_for(db, row)
    data = gateway or row
    if job is _UNSET:
        job = await latest_job(db, row)
    missing = incomplete(data)
    if row.blocked_at:
        state = "blocked"
    elif row.completed_at:
        state = "completed"
    elif row.delivery_mode == "manual":
        state = "manual"
    elif not row.approved_at:
        state = "awaiting_approval" if row.presented_at else "prepared"
    elif missing:
        state = "awaiting_data"
    elif job and job.state in ("queued", "ready") and job.expires_at > utc_now():
        state = "preparing" if job.state == "queued" else "ready"
    else:
        state = "approved"
    payload = {
        "id": row.id,
        "gateway_id": gateway.id if gateway else None,
        "vpn_ip": gateway.vpn_ip if gateway else None,
        "serial_number": data.serial_number or row.original_serial_number,
        **{field: getattr(data, field) for field in DATA_FIELDS},
        "external_reference": row.external_reference,
        "group_id": gateway.group_id if gateway else row.group_id,
        "state": state,
        "delivery_mode": row.delivery_mode,
        "configuration_version": row.configuration_version,
        "created_at": row.created_at,
        "approved_at": row.approved_at,
        "completed_at": row.completed_at,
        "blocked_reason": row.blocked_reason,
        "can_resume": bool(
            row.delivery_mode == "manual"
            and not row.completed_at
            and not row.blocked_at
            and row.secret_hash
        ),
        "missing_fields": missing,
        "job": (
            {
                "id": job.id,
                "state": job.state,
                "expires_at": job.expires_at,
                "configuration_version": job.configuration_version,
                "can_download": state == "ready" and job.delivery_mode == "enrollment",
                "error_code": job.error_code,
            }
            if job
            else None
        ),
    }
    # Lo stato del job e l'inventario operativo entrano nella revisione della scheda.
    payload["revision"] = revision({"type": "enrollment", **payload})
    return payload


async def create(db, station, key, request):
    values = request.model_dump(mode="json")
    fingerprint = digest(json.dumps(values, sort_keys=True))
    old = await db.scalar(
        select(GatewayEnrollment).where(
            GatewayEnrollment.station_id == station, GatewayEnrollment.request_key == key
        )
    )
    if old:
        if old.request_hash != fingerprint:
            raise HTTPException(409, "Idempotency key belongs to a different enrollment")
        return old
    await available_identity(db, request.gateway_id, request.serial_number)
    row = GatewayEnrollment(
        id=request.gateway_id,
        station_id=station,
        request_key=key,
        request_hash=fingerprint,
        original_serial_number=request.serial_number,
        serial_number=request.serial_number,
        secret_hash=request.secret_hash,
        external_reference=request.external_reference,
        **{field: getattr(request, field) for field in DATA_FIELDS},
    )
    db.add(row)
    await db.flush()
    record_event(
        db, "enrollment_prepared", None, gateway_id=row.id, details={"station_id": station}
    )
    return row


async def cancel_pending(db, row, *, actor_id=None):
    from api.services.provisioning import cancel_job

    for job in (
        await db.scalars(
            select(ProvisioningJob).where(
                ProvisioningJob.kind == "gateway",
                ProvisioningJob.peer_id == row.id,
                ProvisioningJob.state.in_(("queued", "ready", "failed")),
            )
        )
    ).all():
        await cancel_job(db, job, actor_id=actor_id)
    await db.flush()


async def ensure_idle(db, identifier):
    if await db.scalar(
        select(Session.id).where(Session.gateway_id == identifier, occupied()).limit(1)
    ):
        raise HTTPException(409, "Close sessions and wait for network cleanup first")


def require_automatic(row):
    if row.blocked_at or row.completed_at or not row.secret_hash:
        raise HTTPException(409, "Enrollment is blocked or completed")
    if row.delivery_mode != "automatic":
        raise HTTPException(409, "Enrollment is managed manually")


async def update(db, row, values, *, actor=None):
    if actor is None:
        require_automatic(row)
    gateway, target = await data_for(db, row)
    if "serial_number" in values:
        await available_identity(db, row.id, values["serial_number"], existing=True)
    operational = any(
        field in values and values[field] != getattr(target, field) for field in OPERATIONAL_FIELDS
    )
    if gateway:
        # Una riga già materializzata mantiene i vincoli del modello operativo.
        if actor is None or not row.completed_at:
            from api.services.gateway_updates import validate_configuration

            candidate = {
                field: values.get(field, getattr(gateway, field)) for field in OPERATIONAL_FIELDS
            }
            validate_configuration(row.id, candidate)
            if operational:
                await ensure_idle(db, row.id)
                await cancel_pending(db, row)
        else:
            from api.services.gateway_updates import update_fields

            await update_fields(
                db, actor, gateway, {k: v for k, v in values.items() if k != "external_reference"}
            )
    elif operational:
        await cancel_pending(db, row, actor_id=actor.id if actor else None)
    for field, value in values.items():
        setattr(row if field == "external_reference" else target, field, value)
    if operational:
        row.configuration_version += 1
    if actor is None:
        row.presented_at = row.presented_at or utc_now()
    record_event(
        db,
        "enrollment_updated",
        actor.id if actor else None,
        gateway_id=row.id,
        details={"fields": sorted(values), "configuration_version": row.configuration_version},
    )
    await db.flush()


async def approve(db, row, actor, group_id):
    if row.blocked_at or row.completed_at:
        raise HTTPException(409, "Enrollment is blocked or completed")
    if await db.get(Group, group_id) is None:
        raise HTTPException(404, "Group not found")
    gateway, _ = await data_for(db, row)
    if row.group_id != group_id:
        await ensure_idle(db, row.id)
        await cancel_pending(db, row, actor_id=actor.id)
        row.configuration_version += 1
        if gateway:
            gateway.group_id = group_id
    row.group_id, row.approved_by, row.approved_at = group_id, actor.id, utc_now()
    record_event(db, "enrollment_approved", actor.id, gateway_id=row.id, group_id=group_id)
    await db.flush()


async def materialize(db, row):
    if not row.approved_at or row.blocked_at or not row.group_id:
        raise HTTPException(409, "Enrollment approval is required")
    if await db.get(Group, row.group_id) is None:
        raise HTTPException(409, "Enrollment group is no longer available")
    gateway, data = await data_for(db, row)
    if incomplete(data):
        raise HTTPException(409, "Complete the activation configuration first")
    GatewayRegisterRequest(
        gateway_id=row.id, **{field: getattr(data, field) for field in OPERATIONAL_FIELDS}
    )
    if gateway is None:
        await available_identity(db, row.id, row.serial_number, existing=True)
        gateway = Gateway(
            id=row.id,
            serial_number=row.serial_number,
            group_id=row.group_id,
            status="PENDING",
            **{field: getattr(row, field) for field in DATA_FIELDS},
        )
        db.add(gateway)
        # Trasferimento atomico della prenotazione al vincolo della tabella Gateway.
        row.serial_number = None
        await db.flush()
    return gateway


async def valid_delivery(db, job):
    from api.services.provisioning import gateway_parameters

    row = await db.get(GatewayEnrollment, job.enrollment_id, populate_existing=True)
    gateway = await db.get(Gateway, job.peer_id, populate_existing=True)
    return bool(
        row
        and gateway
        and row.id == job.peer_id
        and row.secret_hash
        and not row.blocked_at
        and not row.completed_at
        and row.approved_at
        and row.delivery_mode == "automatic"
        and row.group_id == gateway.group_id == job.group_id
        and await db.get(Group, row.group_id) is not None
        and row.configuration_version == job.configuration_version
        and gateway_parameters(gateway) == job.parameters
    )


async def request_package(db, row, version):
    from api.services import provisioning
    from api.services.certificate_renewal import pending

    require_automatic(row)
    if row.configuration_version != version:
        raise HTTPException(409, "Enrollment configuration changed")
    cipher()
    await provisioning.expire_jobs(db)
    gateway = await materialize(db, row)
    if await pending(db, row.id):
        raise HTTPException(409, "A device certificate renewal is pending")
    job = await db.scalar(
        select(ProvisioningJob).where(
            ProvisioningJob.kind == "gateway",
            ProvisioningJob.peer_id == row.id,
            ProvisioningJob.state.in_(("queued", "ready")),
        )
    )
    if job:
        if job.delivery_mode != "enrollment" or not await valid_delivery(db, job):
            raise HTTPException(409, "Another delivery is pending")
        return job
    recent = (
        await db.scalars(
            select(ProvisioningJob).where(
                ProvisioningJob.peer_id == row.id,
                ProvisioningJob.kind == "gateway",
                ProvisioningJob.created_at > utc_now() - timedelta(hours=1),
            )
        )
    ).all()
    if len(recent) >= settings.ENROLLMENT_PACKAGES_PER_HOUR or any(
        j.created_at > utc_now() - timedelta(seconds=60) for j in recent
    ):
        raise HTTPException(
            429, "Wait before requesting another package", headers={"Retry-After": "60"}
        )
    job = ProvisioningJob(
        id=uuid4(),
        delivery_mode="enrollment",
        enrollment_id=row.id,
        configuration_version=row.configuration_version,
        request_key=uuid4(),
        request_hash=digest(f"{row.id}:{row.configuration_version}"),
        kind="gateway",
        peer_id=row.id,
        group_id=row.group_id,
        parameters=provisioning.gateway_parameters(gateway),
        state="queued",
        expires_at=utc_now() + timedelta(minutes=settings.PROVISIONING_DOWNLOAD_MINUTES),
    )
    db.add(job)
    await db.flush()
    provisioning.audit(db, job, "enrollment_package_requested")
    return job


async def delivery_job(db, row, identifier, version=None):
    from api.services.provisioning import expire_jobs

    require_automatic(row)
    await expire_jobs(db)
    job = await db.get(ProvisioningJob, identifier, populate_existing=True)
    if not job or job.enrollment_id != row.id or job.delivery_mode != "enrollment":
        raise HTTPException(404, "Enrollment package not found")
    if (version is not None and version != row.configuration_version) or not await valid_delivery(
        db, job
    ):
        raise HTTPException(409, "Enrollment configuration or approval changed")
    return job


async def complete(db, identifier):
    row = await db.get(GatewayEnrollment, identifier, populate_existing=True)
    if row:
        row.secret_hash = None
        row.completed_at = row.completed_at or utc_now()
        row.blocked_at, row.blocked_reason = None, None
        gateway = await db.get(Gateway, identifier)
        if gateway:
            row.group_id = gateway.group_id
            row.serial_number = None
        record_event(db, "enrollment_completed", None, gateway_id=row.id, group_id=row.group_id)


async def invalidate(db, identifier, reason="enrollment_revoked", *, actor_id=None):
    row = await db.get(GatewayEnrollment, identifier, populate_existing=True)
    if row is None:
        return
    await cancel_pending(db, row, actor_id=actor_id)
    row.secret_hash, row.blocked_at, row.blocked_reason = None, utc_now(), reason
    row.approved_at = None
    # Conserva anche la matricola se viene eliminato il gateway operativo.
    gateway = await db.get(Gateway, identifier)
    if gateway:
        row.serial_number = gateway.serial_number
    record_event(db, "enrollment_blocked", actor_id, gateway_id=row.id, details={"reason": reason})


async def block(db, row, actor, reason):
    from api.services.provisioning import invalidate_peer

    gateway = await db.get(Gateway, row.id)
    if gateway:
        await invalidate_peer(db, actor, "gateway", gateway)
    await invalidate(db, row.id, reason, actor_id=actor.id)


async def manual(db, row, actor, request, key):
    from api.services import provisioning

    # Il replay dello stesso comando resta valido anche dopo l'attivazione.
    previous = await db.scalar(
        select(ProvisioningJob).where(
            ProvisioningJob.actor_id == actor.id,
            ProvisioningJob.request_key == key,
        )
    )
    request_data = {
        "expected_certificate_id": (
            str(request.expected_certificate_id) if request.expected_certificate_id else None
        ),
        "enrollment_id": str(row.id),
        "group_id": str(request.group_id),
    }
    if previous:
        return await provisioning.create_job(db, actor, "gateway", row.id, key, request_data)
    if row.completed_at:
        raise HTTPException(409, "Use the gateway provisioning page for completed enrollment")
    if row.blocked_at:
        # HQ può recuperare una pratica bloccata, senza riattivare il vecchio segreto.
        row.blocked_at, row.blocked_reason = None, None
    await ensure_idle(db, row.id)
    await approve(db, row, actor, request.group_id)
    await cancel_pending(db, row, actor_id=actor.id)
    await materialize(db, row)
    row.delivery_mode = "manual"
    job = await provisioning.create_job(db, actor, "gateway", row.id, key, request_data)
    record_event(db, "enrollment_manual_takeover", actor.id, gateway_id=row.id)
    return job


async def resume(db, row, actor):
    if row.completed_at or row.blocked_at or not row.secret_hash or row.delivery_mode != "manual":
        raise HTTPException(409, "Automatic enrollment cannot be resumed")
    await ensure_idle(db, row.id)
    await cancel_pending(db, row, actor_id=actor.id)
    row.delivery_mode = "automatic"
    record_event(db, "enrollment_resumed", actor.id, gateway_id=row.id)


async def list_enrollments(db, *, q=None, state=None, limit=100, offset=0):
    # Il join proietta l'anagrafica operativa; il registro non diventa una copia da sincronizzare.
    job = aliased(ProvisioningJob)
    latest = (
        select(ProvisioningJob.id)
        .where(
            ProvisioningJob.kind == "gateway",
            ProvisioningJob.peer_id == GatewayEnrollment.id,
        )
        .order_by(ProvisioningJob.created_at.desc(), ProvisioningJob.id.desc())
        .limit(1)
        .correlate(GatewayEnrollment)
        .scalar_subquery()
    )
    query = (
        select(GatewayEnrollment, Gateway, job)
        .outerjoin(Gateway, Gateway.id == GatewayEnrollment.id)
        .outerjoin(job, job.id == latest)
    )

    def field(name):
        return case(
            (Gateway.id.is_not(None), getattr(Gateway, name)),
            else_=getattr(GatewayEnrollment, name),
        )

    missing = or_(
        field("activation_mode").is_(None),
        and_(field("activation_mode") == "sms", field("phone_number").is_(None)),
        and_(field("activation_mode") == "wifi", field("mac_address").is_(None)),
    )
    state_value = case(
        (GatewayEnrollment.blocked_at.is_not(None), "blocked"),
        (GatewayEnrollment.completed_at.is_not(None), "completed"),
        (GatewayEnrollment.delivery_mode == "manual", "manual"),
        (
            and_(
                GatewayEnrollment.approved_at.is_(None), GatewayEnrollment.presented_at.is_not(None)
            ),
            "awaiting_approval",
        ),
        (GatewayEnrollment.approved_at.is_(None), "prepared"),
        (missing, "awaiting_data"),
        (and_(job.state == "queued", job.expires_at > utc_now()), "preparing"),
        (and_(job.state == "ready", job.expires_at > utc_now()), "ready"),
        else_="approved",
    )
    if q and q.strip():
        from api.services.vpn_catalog import identifier_filter

        value = q.strip()
        columns = [GatewayEnrollment.external_reference, GatewayEnrollment.original_serial_number]
        columns += [
            field(name) for name in ("serial_number", *DATA_FIELDS) if name != "activation_mode"
        ]
        compact_mac = re.sub(r"[:.\s-]", "", value)
        mac_match = (
            func.replace(field("mac_address"), ":", "").icontains(compact_mac, autoescape=True)
            if re.fullmatch(r"[0-9a-fA-F]{2,12}", compact_mac)
            else False
        )
        query = query.where(
            or_(
                identifier_filter(GatewayEnrollment.id, value),
                mac_match,
                *(column.icontains(value, autoescape=True) for column in columns),
            )
        )
    if state:
        query = query.where(state_value == state)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(GatewayEnrollment.created_at.desc(), GatewayEnrollment.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return [
        await response(db, row, gateway=gateway, job=latest_job)
        for row, gateway, latest_job in rows
    ], total
