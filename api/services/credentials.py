"""Dynamic machine credentials and certificate validity, shared by every access path."""

import hashlib
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select, exists
from api.config import settings
from api.models.provisioning import GatewayCredential, VPNCertificate
from api.time_utils import utc_now


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def cipher():
    try:
        return Fernet(settings.PROVISIONING_SECRET_KEY.get_secret_value().encode())
    except (ValueError, TypeError):
        raise HTTPException(503, "Provisioning encryption is not configured") from None


def encrypt(value: bytes) -> str:
    return cipher().encrypt(value).decode()


def decrypt(value: str) -> bytes:
    try:
        return cipher().decrypt(value.encode())
    except InvalidToken:
        raise HTTPException(503, "Provisioning encrypted data is unavailable") from None


def certificate_filter(kind, peer_id):
    return (
        VPNCertificate.kind == kind,
        VPNCertificate.peer_id == peer_id,
        VPNCertificate.state == "active",
        VPNCertificate.not_before <= utc_now(),
        VPNCertificate.not_after > utc_now(),
    )


async def active_certificate(db, kind, peer_id):
    return await db.scalar(select(VPNCertificate).where(*certificate_filter(kind, peer_id)))


def configured_gateway(gateway_id):
    return exists(
        select(GatewayCredential.id).where(
            GatewayCredential.gateway_id == gateway_id,
            GatewayCredential.active.is_(True),
            GatewayCredential.revoked_at.is_(None),
            GatewayCredential.registered_at.is_not(None),
        )
    ) & exists(select(VPNCertificate.id).where(*certificate_filter("gateway", gateway_id)))


async def gateway_configured(db, gateway_id):
    return bool(await db.scalar(select(configured_gateway(gateway_id))))


async def current_credential(db, gateway_id):
    return await db.scalar(
        select(GatewayCredential).where(
            GatewayCredential.gateway_id == gateway_id,
            GatewayCredential.active.is_(True),
            GatewayCredential.revoked_at.is_(None),
        )
    )


async def sms_secret(db, gateway_id):
    credential = await current_credential(db, gateway_id)
    if credential is None:
        raise HTTPException(409, "Gateway credentials are not provisioned")
    return decrypt(credential.sms_secret_encrypted).decode()
