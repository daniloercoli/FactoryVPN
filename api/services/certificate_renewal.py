"""CSR proof of possession and atomic certificate-only device activation."""

import base64
import hashlib
import secrets
from datetime import timedelta
from uuid import uuid4
from fastapi import HTTPException
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, func
from api.models.certificate_renewal import CertificateRenewal
from api.models.provisioning import VPNCertificate, GatewayCredential, ProvisioningJob
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.config import Config
from api.time_utils import utc_now
from api.services.access_service import record_event
from api.services.credentials import current_credential
from api.services.provisioning import current_certificate, mark_revoked
from api.services.lifecycle import occupied
from factoryvpn_common.certificates import PEM, fingerprint, parse_csr

PENDING = ("challenge", "queued", "ready", "active")


def event(db, job, action):
    record_event(
        db,
        "certificate_renewal_" + action,
        None,
        group_id=job.group_id,
        gateway_id=job.gateway_id,
        details={"renewal_id": str(job.id)},
    )


def response(job):
    return dict(
        id=job.id,
        gateway_id=job.gateway_id,
        previous_certificate_id=job.previous_certificate_id,
        certificate_id=job.certificate_id,
        state=job.state,
        challenge=job.challenge,
        challenge_expires_at=job.challenge_expires_at,
        expires_at=job.expires_at,
        maintenance_until=job.maintenance_until,
        bundle=job.public_bundle if job.state in ("ready", "active", "confirmed") else None,
        error_code=job.error_code,
    )


async def cancel(db, job, reason="cancelled"):
    if job.state not in PENDING:
        return
    if job.state != "active" and job.certificate_id:
        mark_revoked(await db.get(VPNCertificate, job.certificate_id))
    job.state, job.error_code, job.maintenance_until = "cancelled", reason, None
    event(db, job, "cancelled")


async def reconcile(db):
    for job in (
        await db.scalars(select(CertificateRenewal).where(CertificateRenewal.state.in_(PENDING)))
    ).all():
        gateway = await db.get(Gateway, job.gateway_id)
        credential = await db.get(GatewayCredential, job.credential_id)
        current = await current_certificate(db, "gateway", job.gateway_id)
        expected = job.certificate_id if job.state == "active" else job.previous_certificate_id
        if (
            not gateway
            or gateway.group_id != job.group_id
            or not credential
            or not credential.active
            or credential.revoked_at
            or not current
            or current.id != expected
        ):
            await cancel(db, job, "identity_changed")
        elif job.state != "active" and (
            job.expires_at <= utc_now()
            or (job.state == "challenge" and job.challenge_expires_at <= utc_now())
        ):
            await cancel(db, job, "expired")
    await db.flush()


async def pending(db, gateway_id):
    return await db.scalar(
        select(CertificateRenewal).where(
            CertificateRenewal.gateway_id == gateway_id, CertificateRenewal.state.in_(PENDING)
        )
    )


async def blocks_sessions(db, gateway_id):
    return bool(
        await db.scalar(
            select(CertificateRenewal.id).where(
                CertificateRenewal.gateway_id == gateway_id,
                CertificateRenewal.state == "active",
                CertificateRenewal.maintenance_until > utc_now(),
            )
        )
    )


async def status(db, gateway_id):
    await reconcile(db)
    cert = await current_certificate(db, "gateway", gateway_id)
    gateway = await db.get(Gateway, gateway_id)
    if not cert or not gateway or gateway.group_id is None:
        raise HTTPException(409, "Gateway certificate is not eligible for renewal")
    job = await pending(db, gateway_id)
    flag = await db.get(Config, "certificate-renewal:" + str(gateway_id))
    return dict(
        gateway_id=gateway_id,
        certificate_id=cert.id,
        serial=cert.serial,
        expires_at=cert.not_after,
        renewal_due=bool(flag) or cert.not_after <= utc_now() + timedelta(days=365),
        renewal=response(job) if job else None,
    )


async def challenge(db, gateway_id, request_key, request):
    await reconcile(db)
    csr = parse_csr(request.csr_pem.encode())
    normalized = csr.public_bytes(PEM).decode()
    csr_hash = hashlib.sha256(normalized.encode()).hexdigest()
    previous = await db.scalar(
        select(CertificateRenewal).where(
            CertificateRenewal.gateway_id == gateway_id,
            CertificateRenewal.request_key == request_key,
        )
    )
    if previous:
        if (
            previous.csr_sha256 != csr_hash
            or previous.previous_certificate_id != request.previous_certificate_id
        ):
            raise HTTPException(409, "Renewal idempotency conflict")
        return await authorized_job(db, gateway_id, previous.id)
    gateway = await db.get(Gateway, gateway_id)
    current = await current_certificate(db, "gateway", gateway_id)
    credential = await current_credential(db, gateway_id)
    if (
        not gateway
        or not gateway.group_id
        or not current
        or current.id != request.previous_certificate_id
        or not credential
        or credential.registered_at is None
    ):
        raise HTTPException(409, "Gateway identity changed or is not registered")
    if await pending(db, gateway_id) or await db.scalar(
        select(ProvisioningJob.id).where(
            ProvisioningJob.kind == "gateway",
            ProvisioningJob.peer_id == gateway_id,
            ProvisioningJob.state.in_(("queued", "ready")),
        )
    ):
        raise HTTPException(409, "A certificate replacement is already pending")
    count = await db.scalar(
        select(func.count())
        .select_from(CertificateRenewal)
        .where(
            CertificateRenewal.gateway_id == gateway_id,
            CertificateRenewal.created_at > utc_now() - timedelta(days=1),
        )
    )
    if count >= 10:
        raise HTTPException(429, "Certificate renewal limit reached")
    job_id = uuid4()
    message = (
        f"factoryvpn-certificate-renewal-v1\n{job_id}\n{gateway_id}\n{credential.id}\n"
        f"{current.id}\n{csr_hash}\n{secrets.token_hex(32)}\n"
    )
    job = CertificateRenewal(
        id=job_id,
        gateway_id=gateway_id,
        credential_id=credential.id,
        group_id=gateway.group_id,
        previous_certificate_id=current.id,
        request_key=request_key,
        csr_pem=normalized,
        csr_sha256=csr_hash,
        challenge=base64.b64encode(message.encode()).decode(),
        state="challenge",
        challenge_expires_at=utc_now() + timedelta(minutes=15),
        expires_at=utc_now() + timedelta(days=1),
    )
    db.add(job)
    event(db, job, "requested")
    await db.flush()
    return job


async def authorized_job(db, gateway_id, job_id):
    await reconcile(db)
    job = await db.get(CertificateRenewal, job_id)
    credential = await current_credential(db, gateway_id)
    gateway = await db.get(Gateway, gateway_id)
    if (
        not job
        or job.gateway_id != gateway_id
        or not credential
        or credential.id != job.credential_id
        or not gateway
        or gateway.group_id != job.group_id
    ):
        raise HTTPException(404, "Certificate renewal not found")
    if job.state == "confirmed":
        current = await current_certificate(db, "gateway", gateway_id)
        if not current or current.id != job.certificate_id:
            raise HTTPException(409, "Confirmed certificate has been superseded or revoked")
    return job


def verify_proof(cert, signature, message):
    try:
        public_key = x509.load_pem_x509_certificate(cert.certificate_pem.encode()).public_key()
        if (
            not isinstance(public_key, ec.EllipticCurvePublicKey)
            or public_key.curve.name != "secp256r1"
        ):
            raise ValueError("Unsupported proof key")
        public_key.verify(
            base64.b64decode(signature, validate=True), message, ec.ECDSA(hashes.SHA256())
        )
    except (ValueError, InvalidSignature, TypeError):
        raise HTTPException(403, "Invalid certificate possession proof") from None


async def submit(db, job, signature):
    if job.state in ("queued", "ready", "active", "confirmed"):
        return job
    if job.state != "challenge" or job.challenge_expires_at <= utc_now():
        raise HTTPException(409, "Renewal challenge expired or cancelled")
    previous = await db.get(VPNCertificate, job.previous_certificate_id)
    verify_proof(previous, signature, base64.b64decode(job.challenge))
    job.state = "queued"
    event(db, job, "submitted")
    return job


def activation_message(job):
    return f"factoryvpn-certificate-activate-v1\n{job.id}\n{job.public_bundle['certificate_sha256']}\n".encode()


async def activate(db, job, request):
    if job.state not in ("ready", "active", "confirmed"):
        raise HTTPException(409, "Certificate is not ready")
    cert = await db.get(VPNCertificate, job.certificate_id)
    if request.certificate_sha256 != job.public_bundle["certificate_sha256"]:
        raise HTTPException(409, "Prepared certificate differs")
    verify_proof(cert, request.signature, activation_message(job))
    if job.state in ("active", "confirmed"):
        return job
    if await db.scalar(select(Session.id).where(Session.gateway_id == job.gateway_id, occupied())):
        raise HTTPException(409, "Wait for VPN sessions and cleanup before activation")
    if cert.state != "candidate" or cert.not_after <= utc_now():
        raise HTTPException(409, "Candidate certificate is no longer valid")
    previous = await current_certificate(db, "gateway", job.gateway_id)
    if not previous or previous.id != job.previous_certificate_id:
        raise HTTPException(409, "Certificate generation changed")
    mark_revoked(previous)
    await db.flush()
    cert.state, job.state = "active", "active"
    job.maintenance_until = utc_now() + timedelta(minutes=5)
    flag = await db.get(Config, "certificate-renewal:" + str(job.gateway_id))
    if flag:
        await db.delete(flag)
    event(db, job, "activated")
    return job


async def confirm(db, job, signature):
    if job.state not in ("active", "confirmed"):
        raise HTTPException(409, "Certificate has not been activated")
    cert = await db.get(VPNCertificate, job.certificate_id)
    verify_proof(cert, signature, activation_message(job))
    job.state, job.maintenance_until = "confirmed", None
    if job.confirmed_at is None:
        job.confirmed_at = utc_now()
        event(db, job, "confirmed")
    return job


async def process_one(db, root):
    from factoryvpn_common.pki import issue_csr
    from scripts.provisioning_worker import sync_issuers
    from api.config import settings

    await reconcile(db)
    job = await db.scalar(
        select(CertificateRenewal)
        .where(CertificateRenewal.state == "queued")
        .order_by(CertificateRenewal.created_at)
        .limit(1)
    )
    if not job:
        return
    try:
        cert, issuer_id, chain, trust, generation = issue_csr(
            root,
            "gateway",
            job.gateway_id,
            job.csr_pem.encode(),
            days=settings.VPN_GATEWAY_CERTIFICATE_DAYS,
        )
    except (ValueError, OSError):
        job.state, job.error_code = "failed", "issuer_unavailable"
        event(db, job, "failed")
        return
    await sync_issuers(db, root)
    row = VPNCertificate(
        issuer_id=issuer_id,
        kind="gateway",
        peer_id=job.gateway_id,
        serial=str(cert.serial_number),
        certificate_pem=cert.public_bytes(PEM).decode(),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        state="candidate",
    )
    db.add(row)
    await db.flush()
    job.certificate_id = row.id
    job.public_bundle = dict(
        trust_generation=generation,
        certificate_pem=row.certificate_pem,
        issuer_pem=chain,
        trust_pem=trust,
        issuer_id=issuer_id,
        certificate_sha256=fingerprint(cert),
    )
    job.state = "ready"
    event(db, job, "ready")
