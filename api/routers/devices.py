"""Device inventory inherits gateway scope; mutations require delegated permission."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, Header
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.device import Device
from api.models.user import User
from api.permissions import Permission
from api.routers.auth import get_current_user, require_permission
from api.schemas.device import DeviceCreateRequest, DeviceResponse, DeviceType
from api.schemas.client import PAGINATION_RESPONSES
from api.services.device_inventory import inventory, device_payload
from api.services.edit_versions import check_revision, gateway_revision
from api.services.access_service import (
    check_permission,
    gateway_for_user,
    lock_actor,
    record_event,
)

router = APIRouter()


@router.post("/", response_model=DeviceResponse)
async def create_device(
    request: DeviceCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(Permission.GATEWAYS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, user)
    check_permission(user, Permission.GATEWAYS_MANAGE)
    gateway = await gateway_for_user(db, request.gateway_id, user)
    check_revision(user, if_match, gateway_revision(gateway))
    device = Device(**request.model_dump())
    db.add(device)
    await db.flush()
    record_event(
        db,
        "device_added",
        user.id,
        group_id=gateway.group_id,
        gateway_id=gateway.id,
        details={"device_id": str(device.id)},
    )
    await db.commit()
    return device_payload(device, gateway)


@router.get(
    "/gateway/{gateway_id}", response_model=list[DeviceResponse], responses=PAGINATION_RESPONSES
)
async def list_devices(
    gateway_id: UUID,
    response: Response,
    q: str | None = Query(None, max_length=100),
    device_type: DeviceType | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items, total = await inventory(
        db, user, gateway_id, q=q, device_type=device_type, limit=limit, offset=offset
    )
    response.headers["X-Total-Count"] = str(total)
    return items
