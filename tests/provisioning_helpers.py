"""Explicit credentials for isolated tests; never imported by application code."""

from datetime import timedelta
from api.models.provisioning import GatewayCredential, VPNCertificate, PKIIssuer
from api.services.credentials import digest, encrypt
from api.time_utils import utc_now


async def seed_certificate(db, kind, peer_id, serial=None):
    if not await db.get(PKIIssuer, "0" * 64):
        db.add(
            PKIIssuer(
                id="0" * 64,
                root_id="1" * 64,
                certificate_pem="test-only-issuer",
                not_after=utc_now() + timedelta(days=6000),
                state="retiring",
            )
        )
        await db.flush()
    cert = VPNCertificate(
        issuer_id="0" * 64,
        kind=kind,
        peer_id=peer_id,
        serial=serial or str(peer_id.int),
        certificate_pem="test-public-certificate",
        state="active",
        not_before=utc_now() - timedelta(minutes=1),
        not_after=utc_now() + timedelta(days=365),
    )
    db.add(cert)
    await db.flush()
    return cert


async def seed_gateway(db, gateway, key, *, registered=True, serial=None):
    await db.flush()
    gateway.extra_metadata = gateway.extra_metadata or {}
    db.add(
        GatewayCredential(
            gateway_id=gateway.id,
            secret_hash=digest(key),
            sms_secret_encrypted=encrypt(key.encode()),
            active=True,
            registered_at=utc_now() if registered else None,
        )
    )
    return await seed_certificate(db, "gateway", gateway.id, serial=serial)
