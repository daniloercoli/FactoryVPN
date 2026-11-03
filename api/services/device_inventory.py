"""Shared scoped inventory reads and edit revisions, without machine metadata."""

from sqlalchemy import func, or_, select
from api.models.device import Device
from api.models.gateway import Gateway
from api.services.access_service import gateway_for_user, scope
from api.services.edit_versions import device_revision


def device_payload(device, gateway):
    return {
        "revision": device_revision(device, gateway),
        **{
            field: getattr(device, field)
            for field in (
                "id",
                "gateway_id",
                "name",
                "ip_address",
                "mac_address",
                "device_type",
                "status",
                "last_seen",
            )
        },
    }


async def inventory(db, actor, gateway_id, *, q=None, device_type=None, limit=100, offset=0):
    await gateway_for_user(db, gateway_id, actor)
    # Authorization must be part of each data query: the gateway can move after
    # the initial 404 check, including between the count and the page read.
    query = (
        select(Device, Gateway)
        .join(Gateway, Gateway.id == Device.gateway_id)
        .where(Gateway.id == gateway_id, scope(Gateway.group_id, actor))
    )
    if q:
        query = query.where(
            or_(
                *[
                    column.icontains(q, autoescape=True)
                    for column in (Device.name, Device.ip_address, Device.mac_address)
                ]
            )
        )
    if device_type:
        query = query.where(Device.device_type == device_type)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.execute(
            query.order_by(Device.name, Device.id)
            .offset(offset)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
    ).all()
    # Read revision context in the same snapshot as the device, even if this
    # session previously loaded an older gateway assignment.
    return [device_payload(device, gateway) for device, gateway in rows], total
