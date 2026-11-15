"""Session maintenance endpoints for the authenticated worker."""

from api.services.session_events import event

from uuid import UUID
from fastapi import HTTPException
from api.services.lifecycle import (
    allocation_lock,
    LIVE,
    TERMINAL,
    occupied,
    validate_live,
    work_item,
    gateway_ready,
    network_for,
)
from api.time_utils import utc_now
from factoryvpn_common.protocol import parse_peer_name
from ipaddress import IPv4Network


from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from api.services.credentials import active_certificate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.session import Session
from api.services.machine_auth import require_worker
from api.services.session_service import expire_stale_sessions
from api.schemas.diagnostics import WorkerObservation, ObservationResponse
from api.services.diagnostics import observe

router = APIRouter(dependencies=[Depends(require_worker)])
TERMINAL_STATUSES = {"CLOSED", "EXPIRED", "FAILED"}


@router.post("/sessions/expire-stale")
async def expire_sessions(db: AsyncSession = Depends(get_db)):
    return {"expired": await expire_stale_sessions(db)}


@router.get("/sessions/work")
async def session_work(db: AsyncSession = Depends(get_db)):
    await expire_stale_sessions(db)
    sessions = (
        (await db.execute(select(Session).where(occupied()).order_by(Session.requested_at)))
        .scalars()
        .all()
    )
    work = []
    for session in sessions:
        await validate_live(db, session)
        work.append(work_item(session))
        # Commit each validated snapshot before locking the next session.
        await db.commit()
    return work


async def locked_session(db, identifier):
    session = (
        await db.execute(select(Session).where(Session.id == identifier).with_for_update())
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    return session


@router.post("/sessions/{identifier}/ready")
async def network_ready(
    identifier: UUID,
    observation: WorkerObservation | None = None,
    db: AsyncSession = Depends(get_db),
):
    session = await locked_session(db, identifier)
    if not await validate_live(db, session) or not gateway_ready(session):
        await db.commit()
        raise HTTPException(409, "Session is no longer ready for activation")
    if session.status != "ACTIVE":
        event(db, session, "session_active", source="worker")
    session.status = "ACTIVE"
    session.network_ready_at = utc_now()
    session.worker_seen_at = utc_now()
    session.support_started_at = session.support_started_at or utc_now()
    if observation:
        await observe(db, session, "worker", observation)
    await db.commit()
    return {"status": session.status}


@router.post("/sessions/{identifier}/waiting")
async def network_waiting(
    identifier: UUID,
    observation: WorkerObservation | None = None,
    db: AsyncSession = Depends(get_db),
):
    session = await locked_session(db, identifier)
    if session.status in LIVE:
        if session.status != "REQUESTED":
            event(db, session, "session_waiting", source="worker")
        session.status = "REQUESTED"
        session.network_ready_at = None
        session.worker_seen_at = utc_now()
        if observation:
            await observe(db, session, "worker", observation)
        await db.commit()
    return {"status": session.status}


@router.post("/sessions/{identifier}/diagnostics", response_model=ObservationResponse)
async def diagnostic_report(
    identifier: UUID,
    report: WorkerObservation,
    db: AsyncSession = Depends(get_db),
):
    session = await locked_session(db, identifier)
    recorded = await observe(db, session, "worker", report)
    await db.commit()
    return {"recorded": recorded}


@router.post("/sessions/{identifier}/cleaned")
async def network_cleaned(identifier: UUID, db: AsyncSession = Depends(get_db)):
    session = await locked_session(db, identifier)
    if session.status not in TERMINAL:
        raise HTTPException(409, "Cannot release resources of a live session")
    if not session.cleaned_at:
        event(db, session, "resources_released", source="worker")
    session.cleaned_at = session.cleaned_at or utc_now()
    await db.commit()
    return {"status": session.status, "cleaned": True}


class VPNPeerRequest(BaseModel):
    common_name: str
    address: str | None = None


class VPNAuthorizeRequest(VPNPeerRequest):
    certificate_serial: str = Field(pattern=r"^[1-9][0-9]{0,49}$")


async def authorized_peer(db, common_name, certificate_serial=None):
    try:
        kind, identifier = parse_peer_name(common_name)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(403, "Invalid VPN identity") from exc
    # Bind the serial check and session lookup to the same credential generation.
    # Rotation/revocation takes this lock before changing either record.
    await allocation_lock(db)
    certificate = await active_certificate(db, kind, identifier)
    if not certificate or (
        certificate_serial is not None and certificate.serial != certificate_serial
    ):
        raise HTTPException(403, "VPN certificate is not active for this identity")
    column = Session.gateway_id if kind == "gateway" else Session.technician_id
    session = (
        await db.execute(
            select(Session).where(column == identifier, Session.status.in_(LIVE)).with_for_update()
        )
    ).scalar_one_or_none()
    if not session:
        raise HTTPException(403, "No authorized VPN session")
    if not await validate_live(db, session):
        await db.commit()
        raise HTTPException(403, "No authorized VPN session")
    spec = network_for(session)
    address = spec.gateway_ip if kind == "gateway" else spec.technician_ip
    return kind, session, spec, address


@router.post("/vpn/authorize")
async def authorize_vpn(request: VPNAuthorizeRequest, db: AsyncSession = Depends(get_db)):
    kind, session, spec, address = await authorized_peer(
        db, request.common_name, request.certificate_serial
    )
    # Una sola destinazione VPN per peer; nessuna rotta cliente o default route.
    route = IPv4Network((spec.technician_ip if kind == "gateway" else spec.gateway_ip) + "/32")
    await db.commit()
    return {
        "config": f'ifconfig-push {address} 255.255.0.0\npush "route {route.network_address} {route.netmask}"\n'
    }


@router.post("/vpn/learn")
async def learn_vpn(request: VPNPeerRequest, db: AsyncSession = Depends(get_db)):
    _, _, _, address = await authorized_peer(db, request.common_name)
    await db.commit()
    if request.address != address:
        raise HTTPException(403, "VPN source address is not assigned to this certificate")
    return {"allowed": True}
