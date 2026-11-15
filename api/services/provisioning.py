"""Authorization and atomic state transitions for staged profile delivery."""

import json
from datetime import timedelta
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select, func
from api.config import settings
from api.models.gateway import Gateway
from api.models.user import User
from api.models.config import Config
from api.models.session import Session
from api.models.provisioning import ProvisioningJob, VPNCertificate, GatewayCredential
from api.permissions import Permission
from api.schemas.gateway import GatewayRegisterRequest
from api.services.access_service import (
    check_permission,
    gateway_for_user,
    validate_group_ids,
    record_event,
)
from api.services.credentials import cipher, digest, decrypt, current_credential
from api.services.lifecycle import occupied, LIVE
from api.services.session_events import terminal_event
from api.services.web_session_service import valid_web_session
from api.services.sso_service import valid_session
from api.time_utils import utc_now


def peer_column(kind):
    return Session.gateway_id if kind == "gateway" else Session.technician_id


async def current_certificate(db, kind, peer_id):
    # Include expired certificates: replacement must still match the displayed identity.
    return await db.scalar(
        select(VPNCertificate).where(
            VPNCertificate.kind == kind,
            VPNCertificate.peer_id == peer_id,
            VPNCertificate.state == "active",
        )
    )


async def authorize_peer(db, actor, kind, peer_id, *, issue=False):
    if kind == "gateway":
        check_permission(actor, Permission.GATEWAYS_PROVISION)
        return await gateway_for_user(db, peer_id, actor)
    if peer_id != actor.id:
        if issue or not actor.is_admin:
            raise HTTPException(404, "Profile not found")
    elif issue:
        check_permission(actor, Permission.CONNECT)
    peer = await db.get(User, peer_id)
    if peer is None or (issue and not peer.is_active):
        raise HTTPException(404, "Profile not found")
    return peer


async def valid_job_actor(db, job):
    if job.delivery_mode == "enrollment":
        from api.services.enrollment import valid_delivery

        return await valid_delivery(db, job)
    actor = await db.get(User, job.actor_id, populate_existing=True)
    if (
        not actor
        or not actor.is_active
        or actor.token_version != job.actor_version
        or job.access_expires_at <= utc_now()
    ):
        return None
    if job.web_session_id:
        if not await valid_web_session(db, job.web_session_id, actor):
            return None
    elif job.sso_session_id:
        if not await valid_session(db, job.sso_session_id, actor):
            return None
    elif actor.auth_mode != "local":
        return None
    try:
        peer = await authorize_peer(db, actor, job.kind, job.peer_id, issue=True)
    except HTTPException:
        return None
    if job.kind == "gateway":
        if peer.group_id != job.group_id or gateway_parameters(peer) != job.parameters:
            return None
    return actor


def gateway_parameters(gateway):
    return GatewayRegisterRequest(
        gateway_id=gateway.id,
        activation_mode=gateway.activation_mode,
        phone_number=gateway.phone_number,
        mac_address=gateway.mac_address,
    ).model_dump(mode="json")


def owns_delivery(actor, job):
    return (
        actor.id == job.actor_id and getattr(actor, "_provisioning_access", None) == job.access_hash
    )


def job_response(job, actor):
    available = (
        job.state == "ready"
        and job.expires_at > utc_now()
        and owns_delivery(actor, job)
        and job.actor_version == actor.token_version
    )
    return {
        **{
            field: getattr(job, field)
            for field in (
                "id",
                "kind",
                "peer_id",
                "state",
                "created_at",
                "expires_at",
                "downloaded_at",
                "error_code",
            )
        },
        "can_download": available,
        "can_activate": available and job.downloaded_at is not None,
    }


def certificate_response(cert):
    if cert is None:
        return None
    return {
        field: getattr(cert, field)
        for field in (
            "id",
            "serial",
            "not_before",
            "not_after",
            "state",
            "revoked_at",
            "crl_applied_at",
        )
    }


async def state(db, actor, kind, peer_id):
    peer = await authorize_peer(db, actor, kind, peer_id)
    cert = await current_certificate(db, kind, peer_id)
    jobs = (
        await db.scalars(
            select(ProvisioningJob)
            .where(
                ProvisioningJob.kind == kind,
                ProvisioningJob.peer_id == peer_id,
                ProvisioningJob.delivery_mode == "manual",
                # Historical requests belong to the group in which they were
                # created, just like direct access through authorized_job().
                ProvisioningJob.group_id == peer.group_id if kind == "gateway" else True,
            )
            .order_by(ProvisioningJob.created_at.desc())
            .limit(20)
        )
    ).all()
    pending = next((j for j in jobs if j.state in ("queued", "ready")), None)
    credential = await current_credential(db, peer_id) if kind == "gateway" else None
    health = await db.get(Config, "provisioning_worker")
    health_state = (
        "unknown"
        if health is None
        else ("stale" if health.updated_at < utc_now() - timedelta(seconds=30) else health.value)
    )
    from api.services import certificate_renewal

    renewal = await certificate_renewal.pending(db, peer_id) if kind == "gateway" else None
    requested = (
        await db.get(Config, "certificate-renewal:" + str(peer_id)) if kind == "gateway" else None
    )
    from api.models.enrollment import GatewayEnrollment

    enrollment = await db.get(GatewayEnrollment, peer_id) if kind == "gateway" else None
    return {
        "enrollment_pending": bool(
            enrollment and not enrollment.completed_at and enrollment.delivery_mode == "automatic"
        ),
        "remote_renewal_state": renewal.state if renewal else None,
        "remote_renewal_requested": bool(requested),
        "certificate": certificate_response(cert),
        "pending_job": job_response(pending, actor) if pending else None,
        "recent_job": job_response(jobs[0], actor) if jobs else None,
        "registered_at": credential.registered_at if credential else None,
        "cleanup_pending": bool(
            await db.scalar(
                select(Session.id)
                .where(
                    peer_column(kind) == peer_id,
                    occupied(),
                    Session.status.not_in(LIVE),
                )
                .limit(1)
            )
        ),
        "revocation_pending": bool(
            await db.scalar(
                select(VPNCertificate.id)
                .where(
                    VPNCertificate.kind == kind,
                    VPNCertificate.peer_id == peer_id,
                    VPNCertificate.revoked_at.is_not(None),
                    VPNCertificate.crl_applied_at.is_(None),
                )
                .limit(1)
            )
        ),
        "enabled": bool(settings.PROVISIONING_SECRET_KEY.get_secret_value()),
        "issuer_status": (
            health_state if health_state in ("ok", "error", "unknown", "stale") else "unknown"
        ),
        "issuer_updated_at": health.updated_at if health else None,
    }


def audit(db, job, event, actor_id=None):
    record_event(
        db,
        event,
        actor_id or job.actor_id,
        group_id=job.group_id,
        gateway_id=job.peer_id if job.kind == "gateway" else None,
        details={"job_id": str(job.id), "kind": job.kind, "peer_id": str(job.peer_id)},
    )


def mark_revoked(cert):
    if cert and cert.state != "revoked":
        cert.state = "revoked"
        cert.revoked_at = utc_now()


async def cancel_job(db, job, *, reason="cancelled", actor_id=None):
    if job.state not in ("queued", "ready", "failed"):
        return
    job.state = reason
    job.artifact_encrypted = None
    if job.certificate_id:
        mark_revoked(await db.get(VPNCertificate, job.certificate_id))
    if job.credential_id:
        cred = await db.get(GatewayCredential, job.credential_id)
        if cred and not cred.active:
            cred.revoked_at = utc_now()
            cred.sms_secret_encrypted = ""
    audit(db, job, "provisioning_" + reason, actor_id)


async def expire_jobs(db):
    for job in (
        await db.scalars(
            select(ProvisioningJob).where(
                ProvisioningJob.state.in_(("queued", "ready")),
            )
        )
    ).all():
        if job.expires_at <= utc_now() or not await valid_job_actor(db, job):
            await cancel_job(db, job, reason="expired")
    await db.flush()


async def create_job(db, actor, kind, peer_id, key, request, *, new_gateway=None):
    cipher()  # Fail before creating any records when deployment secrets are absent.
    await expire_jobs(db)
    fingerprint = digest(json.dumps([kind, str(peer_id), request], sort_keys=True, default=str))
    previous = await db.scalar(
        select(ProvisioningJob).where(
            ProvisioningJob.actor_id == actor.id,
            ProvisioningJob.request_key == key,
        )
    )
    if previous:
        await authorize_peer(db, actor, previous.kind, previous.peer_id, issue=True)
        if previous.request_hash != fingerprint or not owns_delivery(actor, previous):
            raise HTTPException(
                409, "Provisioning request belongs to a different operation or access"
            )
        return previous
    if kind == "gateway":
        from api.models.enrollment import GatewayEnrollment

        enrollment = await db.get(GatewayEnrollment, peer_id)
        if enrollment and not enrollment.completed_at and enrollment.delivery_mode == "automatic":
            raise HTTPException(409, "Take over the enrollment before preparing a manual package")
        from api.services.certificate_renewal import reconcile, pending

        await reconcile(db)
        if await pending(db, peer_id):
            raise HTTPException(409, "A device certificate renewal is pending")
    recent = await db.scalar(
        select(func.count())
        .select_from(ProvisioningJob)
        .where(
            ProvisioningJob.actor_id == actor.id,
            ProvisioningJob.created_at > utc_now() - timedelta(hours=1),
        )
    )
    if recent >= 60:
        raise HTTPException(
            429, "Provisioning request limit reached; retry later", headers={"Retry-After": "3600"}
        )
    if new_gateway:
        from api.services.enrollment import available_identity

        await available_identity(db, peer_id, new_gateway.serial_number)
        check_permission(actor, Permission.GATEWAYS_PROVISION)
        await validate_group_ids(db, actor, [new_gateway.group_id])
        db.add(
            Gateway(
                id=peer_id,
                group_id=new_gateway.group_id,
                display_name=new_gateway.display_name.strip(),
                serial_number=new_gateway.serial_number,
                imei=new_gateway.imei,
                iccid=new_gateway.iccid,
                mac_address=new_gateway.mac_address,
                phone_number=new_gateway.phone_number,
                activation_mode=new_gateway.activation_mode,
                status="PENDING",
            )
        )
        await db.flush()
    peer = await authorize_peer(db, actor, kind, peer_id, issue=True)
    cert = await current_certificate(db, kind, peer_id)
    if (str(cert.id) if cert else None) != request.get("expected_certificate_id"):
        raise HTTPException(409, "Certificate changed; reload before preparing a profile")
    await expire_jobs(db)
    if await db.scalar(
        select(ProvisioningJob.id).where(
            ProvisioningJob.kind == kind,
            ProvisioningJob.peer_id == peer_id,
            ProvisioningJob.state.in_(("queued", "ready")),
        )
    ):
        raise HTTPException(409, "A profile is already being prepared for this peer")
    now = utc_now()
    job = ProvisioningJob(
        id=uuid4(),
        actor_id=actor.id,
        actor_version=actor.token_version,
        access_hash=actor._provisioning_access,
        access_expires_at=actor._provisioning_expires,
        web_session_id=getattr(actor, "_web_session_id", None),
        sso_session_id=getattr(actor, "_sso_session_id", None),
        request_key=key,
        request_hash=fingerprint,
        kind=kind,
        peer_id=peer_id,
        group_id=peer.group_id if kind == "gateway" else None,
        parameters=gateway_parameters(peer) if kind == "gateway" else {},
        state="queued",
        expires_at=min(
            actor._provisioning_expires,
            now + timedelta(minutes=settings.PROVISIONING_DOWNLOAD_MINUTES),
        ),
    )
    db.add(job)
    audit(db, job, "provisioning_requested")
    await db.flush()
    return job


async def authorized_job(db, actor, job_id, *, delivery=False):
    job = await db.get(ProvisioningJob, job_id, populate_existing=True)
    if not job or job.delivery_mode == "enrollment":
        raise HTTPException(404, "Provisioning request not found")
    peer = await authorize_peer(db, actor, job.kind, job.peer_id)
    if job.kind == "gateway" and peer.group_id != job.group_id:
        raise HTTPException(404, "Provisioning request not found")
    if delivery and (not owns_delivery(actor, job) or not await valid_job_actor(db, job)):
        raise HTTPException(404, "Provisioning delivery not available for this access")
    return job


async def download(db, actor, job):
    if job.state != "ready" or job.expires_at <= utc_now() or not job.artifact_encrypted:
        raise HTTPException(409, "Provisioning download expired or unavailable")
    data = decrypt(job.artifact_encrypted)
    job.downloaded_at = utc_now()
    audit(db, job, "provisioning_downloaded", actor.id if actor else None)
    return data


async def activate(db, actor, job):
    if job.state == "active":
        return
    if job.delivery_mode == "enrollment":
        from api.services.enrollment import valid_delivery

        if actor is not None or not await valid_delivery(db, job):
            raise HTTPException(409, "Enrollment delivery is no longer authorized")
    if job.state != "ready" or job.expires_at <= utc_now() or not job.downloaded_at:
        raise HTTPException(409, "Download the prepared profile before activation")
    if await db.scalar(
        select(Session.id)
        .where(
            peer_column(job.kind) == job.peer_id,
            occupied(),
        )
        .limit(1)
    ):
        raise HTTPException(409, "Close sessions and wait for network cleanup before activation")
    old = await current_certificate(db, job.kind, job.peer_id)
    mark_revoked(old)
    if job.kind == "gateway":
        previous = await current_credential(db, job.peer_id)
        if previous:
            previous.active, previous.revoked_at, previous.sms_secret_encrypted = (
                False,
                utc_now(),
                "",
            )
    await db.flush()  # Release partial unique indexes before activating replacements.
    cert = await db.get(VPNCertificate, job.certificate_id)
    if not cert or cert.state != "candidate" or cert.not_after <= utc_now():
        raise HTTPException(409, "Prepared certificate is no longer available")
    cert.state = "active"
    if job.kind == "gateway":
        credential = await db.get(GatewayCredential, job.credential_id)
        if not credential or credential.revoked_at:
            raise HTTPException(409, "Prepared gateway credential is no longer available")
        credential.active = True
        gateway = await db.get(Gateway, job.peer_id)
        gateway.status, gateway.last_seen = "PENDING", None
    job.state, job.artifact_encrypted = "active", None
    if job.kind == "gateway":
        from api.services.enrollment import complete

        await complete(db, job.peer_id)
    audit(db, job, "provisioning_activated", actor.id if actor else None)


async def revoke_peer(db, actor, kind, peer_id, expected_id):
    peer = await authorize_peer(db, actor, kind, peer_id)
    cert = await current_certificate(db, kind, peer_id)
    if (cert.id if cert else None) != expected_id:
        raise HTTPException(409, "Certificate changed; reload before revoking")
    await invalidate_peer(db, actor, kind, peer)


async def invalidate_peer(db, actor, kind, peer):
    """Caller holds allocation_lock and has authorized this peer's mutation."""
    peer_id = peer.id
    cert = await current_certificate(db, kind, peer_id)
    for job in (
        await db.scalars(
            select(ProvisioningJob).where(
                ProvisioningJob.kind == kind,
                ProvisioningJob.peer_id == peer_id,
                ProvisioningJob.state.in_(("queued", "ready", "failed")),
            )
        )
    ).all():
        await cancel_job(db, job, actor_id=actor.id)
    mark_revoked(cert)
    if kind == "gateway":
        from api.services.enrollment import invalidate

        await invalidate(db, peer_id, actor_id=actor.id)
        credential = await current_credential(db, peer_id)
        if credential:
            credential.active, credential.revoked_at, credential.sms_secret_encrypted = (
                False,
                utc_now(),
                "",
            )
        peer.status = "INACTIVE"
    for session in (
        await db.scalars(
            select(Session)
            .where(
                peer_column(kind) == peer_id,
                Session.status.in_(LIVE),
            )
            .with_for_update()
        )
    ).all():
        session.status, session.ended_at = "CLOSED", utc_now()
        session.error_reason = "VPN credential revoked"
        terminal_event(db, session, actor_id=actor.id)
    from api.services.certificate_renewal import reconcile

    await db.flush()
    await reconcile(db)
    record_event(
        db,
        "vpn_credentials_revoked",
        actor.id,
        group_id=peer.group_id if kind == "gateway" else None,
        gateway_id=peer_id if kind == "gateway" else None,
        details={"kind": kind, "peer_id": str(peer_id)},
    )
