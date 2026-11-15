"""Correzioni HQ: inventario libero dalla VPN, dati operativi con revoca esplicita."""

from fastapi import HTTPException
from pydantic import ValidationError
from api.models.enrollment import GatewayEnrollment
from api.schemas.gateway import GatewayRegisterRequest


def validate_configuration(identifier, values):
    try:
        return GatewayRegisterRequest(gateway_id=identifier, **values)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid gateway activation configuration") from exc


async def update_fields(db, actor, gateway, values):
    from api.services.enrollment import available_identity, ensure_idle, OPERATIONAL_FIELDS
    from api.services.provisioning import invalidate_peer

    row = await db.get(GatewayEnrollment, gateway.id)
    if "serial_number" in values:
        if row and not values["serial_number"]:
            raise HTTPException(422, "Serial number is required for an enrolled gateway")
        await available_identity(db, gateway.id, values["serial_number"], existing=True)
    requested = set(values) & set(OPERATIONAL_FIELDS)
    if requested and not actor.is_admin:
        raise HTTPException(403, "Global administrator required for operational changes")
    changed = any(values[field] != getattr(gateway, field) for field in requested)
    if changed:
        validate_configuration(
            gateway.id,
            {field: values.get(field, getattr(gateway, field)) for field in OPERATIONAL_FIELDS},
        )
        await ensure_idle(db, gateway.id)
        await invalidate_peer(db, actor, "gateway", gateway)
    for field, value in values.items():
        setattr(gateway, field, value)
    if row and row.serial_number is not None and "serial_number" in values:
        row.serial_number = values["serial_number"]
