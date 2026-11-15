"""
Gateways router
"""

from api.services.session_events import event
from api.services.diagnostics import observe
from api.schemas.diagnostics import GatewayObservation, ObservationResponse

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from factoryvpn_common.protocol import verify_activation
from api.time_utils import utc_now
from api.services.credentials import current_credential, sms_secret
from api.services.provisioning import gateway_parameters
from api.db.session import get_db
from api.models.gateway import Gateway
from api.models.user import User
from api.models.session import Session
from api.services.lifecycle import (
    allocation_lock,
    assign_ip,
    validate_live,
    network_for,
    lease_seconds,
    LIVE,
)
from api.schemas.gateway import HeartbeatRequest
from api.routers.auth import get_current_user
from api.schemas.gateway import (
    GatewayRegisterRequest,
    GatewayResponse,
    GatewayCatalogResponse,
    GatewayDetailResponse,
    GatewayAvailability,
    GatewayStatus,
    ActivationMode,
)
from api.services.machine_auth import get_gateway_identity
from api.services.vpn_catalog import gateway_list, gateway_detail
from api.schemas.client import PAGINATION_RESPONSES

router = APIRouter()


@router.post("/sessions/{session_id}/diagnostics", response_model=ObservationResponse)
async def diagnostic_report(
    session_id: UUID,
    report: GatewayObservation,
    identity: str = Depends(get_gateway_identity),
    db: AsyncSession = Depends(get_db),
):
    session = await db.scalar(
        select(Session)
        .where(Session.id == session_id, Session.gateway_id == UUID(identity))
        .with_for_update()
    )
    if session is None:
        raise HTTPException(404, "Session not found")
    recorded = await observe(db, session, "gateway", report)
    await db.commit()
    return {"recorded": recorded}


@router.post("/register", response_model=GatewayResponse)
async def register_gateway(
    request: GatewayRegisterRequest,
    identity: str = Depends(get_gateway_identity),
    db: AsyncSession = Depends(get_db),
):
    """Register a new gateway or update existing"""
    if str(request.gateway_id) != identity:
        raise HTTPException(status_code=403, detail="Gateway identity does not match credential")
    await allocation_lock(db)
    result = await db.execute(select(Gateway).where(Gateway.id == request.gateway_id))
    gateway = result.scalar_one_or_none()

    if gateway is None:
        raise HTTPException(404, "Provision the gateway before registering it")
    if gateway_parameters(gateway) != request.model_dump(mode="json"):
        raise HTTPException(409, "Gateway registration differs from its provisioned configuration")
    gateway.status, gateway.last_seen = "ACTIVE", utc_now()
    credential = await current_credential(db, gateway.id)
    if credential.registered_at is None:
        from api.services.access_service import record_event

        record_event(
            db, "gateway_registered", None, group_id=gateway.group_id, gateway_id=gateway.id
        )
    credential.registered_at = credential.registered_at or utc_now()
    await assign_ip(db, gateway)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "Gateway identity or phone number is already registered") from exc
    await db.refresh(gateway)

    return GatewayResponse(
        id=str(gateway.id),
        display_name=gateway.display_name,
        group_id=gateway.group_id,
        phone_number=gateway.phone_number,
        activation_mode=gateway.activation_mode,
        mac_address=gateway.mac_address,
        serial_number=gateway.serial_number,
        imei=gateway.imei,
        iccid=gateway.iccid,
        status=gateway.status,
        vpn_ip=gateway.vpn_ip,
        last_seen=str(gateway.last_seen) if gateway.last_seen else None,
    )


@router.get("/", response_model=list[GatewayCatalogResponse], responses=PAGINATION_RESPONSES)
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
    current_user: User = Depends(get_current_user),
):
    """Search scoped gateways by label, inventory identifiers, phone or exact UUID.

    X-Total-Count covers the same filtered scope before pagination. Availability
    and can_start are advisory; they neither reserve a peer nor prove it is online.
    No session identifiers or identities of other technicians are returned.
    """
    items, total = await gateway_list(
        db,
        current_user,
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


@router.get("/{gateway_id}", response_model=GatewayDetailResponse)
async def get_gateway(
    gateway_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read scoped network data and advisory availability; invisible IDs return 404.

    The current reservation is returned only to its owner or sessions.manage in
    its historical group. Other callers see availability without session details.
    """
    return await gateway_detail(db, current_user, gateway_id)


@router.post("/heartbeat")
async def heartbeat(
    request: HeartbeatRequest,
    identity: str = Depends(get_gateway_identity),
    db: AsyncSession = Depends(get_db),
):
    gateway = (
        await db.execute(select(Gateway).where(Gateway.id == UUID(identity)))
    ).scalar_one_or_none()
    if not gateway:
        raise HTTPException(404, "Register the gateway first")
    if request.vpn_ip and request.vpn_ip != gateway.vpn_ip:
        raise HTTPException(409, "Tunnel address differs from assigned gateway address")
    gateway.last_seen = utc_now()
    gateway.status = "ACTIVE" if request.vpn_ip else "INACTIVE"
    session = (
        await db.execute(
            select(Session)
            .where(Session.gateway_id == gateway.id, Session.status.in_(LIVE))
            .with_for_update()
        )
    ).scalar_one_or_none()
    received_before = session.activation_received_at if session else None
    desired = bool(session and await validate_live(db, session, require_activation=False))
    if desired and session.activation_mode == "sms":
        if request.activation_token:
            identifier = verify_activation(
                request.activation_token, await sms_secret(db, UUID(identity))
            )
            if identifier != str(session.id):
                raise HTTPException(403, "SMS activation is invalid or belongs to another session")
            session.activation_received_at = session.activation_received_at or utc_now()
        desired = session.activation_received_at is not None
    elif desired:
        session.activation_received_at = session.activation_received_at or utc_now()
    if desired and session.activation_received_at and not received_before:
        event(db, session, "gateway_acknowledged", source="gateway")
    if desired:
        if request.ready_session_id == session.id and request.vpn_ip == session.gateway_vpn_ip:
            session.gateway_ready_at = utc_now()
            session.gateway_connected_at = session.gateway_connected_at or utc_now()
        else:
            session.gateway_ready_at = None
        await db.commit()
        return {
            "connect": True,
            "network": network_for(session).to_dict(),
            "lease_seconds": lease_seconds(session),
            "expires_at": session.expires_at.isoformat(),
        }
    await db.commit()
    return {"connect": False, "network": None, "lease_seconds": 0}
