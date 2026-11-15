"""Authenticated provisioning commands; the issuer alone holds the CA key."""

from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from api.db.session import get_db
from api.routers.auth import get_current_user
from api.models.web_session import WebSession
from api.schemas.provisioning import (
    GatewayProvisionRequest,
    ProfileRequest,
    JobResponse,
    ProvisioningState,
    PeerKind,
)
from api.services import provisioning as service
from api.services.access_service import lock_actor
from api.services.auth_service import verify_token
from api.services.credentials import digest
from api.time_utils import utc_now

router = APIRouter()


async def actor_context(request: Request, actor=Depends(get_current_user), db=Depends(get_db)):
    if getattr(actor, "_web_session_id", None):
        row = await db.get(WebSession, actor._web_session_id)
        actor._provisioning_access = digest("web:" + str(row.id))
        actor._provisioning_expires = row.expires_at
    else:
        token = request.headers.get("authorization", "").partition(" ")[2]
        payload = verify_token(token)
        actor._provisioning_access = digest("bearer:" + token)
        actor._provisioning_expires = datetime.fromtimestamp(payload["exp"], timezone.utc)
    await lock_actor(db, actor)
    if actor._provisioning_expires <= utc_now():
        raise HTTPException(401, "Provisioning access expired; sign in again")
    return actor


@router.post("/provisioning/gateways", response_model=JobResponse, status_code=202)
async def provision_gateway(
    request: GatewayProvisionRequest,
    idempotency_key: UUID = Header(),
    db=Depends(get_db),
    actor=Depends(actor_context),
):
    job = await service.create_job(
        db,
        actor,
        "gateway",
        request.gateway_id,
        idempotency_key,
        {**request.model_dump(mode="json"), "expected_certificate_id": None},
        new_gateway=request,
    )
    await db.commit()
    return service.job_response(job, actor)


@router.get("/provisioning/peers/{kind}/{peer_id}", response_model=ProvisioningState)
async def peer_state(
    kind: PeerKind, peer_id: UUID, db=Depends(get_db), actor=Depends(actor_context)
):
    await service.expire_jobs(db)
    result = await service.state(db, actor, kind, peer_id)
    await db.commit()
    return result


@router.post(
    "/provisioning/peers/{kind}/{peer_id}/prepare", response_model=JobResponse, status_code=202
)
async def prepare(
    kind: PeerKind,
    peer_id: UUID,
    request: ProfileRequest,
    idempotency_key: UUID = Header(),
    db=Depends(get_db),
    actor=Depends(actor_context),
):
    job = await service.create_job(
        db, actor, kind, peer_id, idempotency_key, request.model_dump(mode="json")
    )
    await db.commit()
    return service.job_response(job, actor)


@router.post(
    "/provisioning/peers/gateway/{peer_id}/renew-certificate", response_model=ProvisioningState
)
async def request_remote_renewal(peer_id: UUID, db=Depends(get_db), actor=Depends(actor_context)):
    from api.models.config import Config
    from api.services.credentials import current_credential
    from api.services.access_service import record_event

    gateway = await service.authorize_peer(db, actor, "gateway", peer_id, issue=True)
    cert = await service.current_certificate(db, "gateway", peer_id)
    credential = await current_credential(db, peer_id)
    if not cert or not credential or not credential.registered_at:
        raise HTTPException(409, "A registered gateway with active credentials is required")
    key = "certificate-renewal:" + str(peer_id)
    if await db.get(Config, key) is None:
        db.add(Config(key=key, value="requested"))
        record_event(
            db,
            "certificate_renewal_scheduled",
            actor.id,
            group_id=gateway.group_id,
            gateway_id=peer_id,
        )
    result = await service.state(db, actor, "gateway", peer_id)
    await db.commit()
    return result


@router.post("/provisioning/peers/{kind}/{peer_id}/revoke", response_model=ProvisioningState)
async def revoke(
    kind: PeerKind,
    peer_id: UUID,
    request: ProfileRequest,
    db=Depends(get_db),
    actor=Depends(actor_context),
):
    await service.revoke_peer(db, actor, kind, peer_id, request.expected_certificate_id)
    result = await service.state(db, actor, kind, peer_id)
    await db.commit()
    return result


@router.get("/provisioning/jobs/{job_id}", response_model=JobResponse)
async def job_status(job_id: UUID, db=Depends(get_db), actor=Depends(actor_context)):
    await service.expire_jobs(db)
    job = await service.authorized_job(db, actor, job_id)
    result = service.job_response(job, actor)
    await db.commit()
    return result


@router.post(
    "/provisioning/jobs/{job_id}/download",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def download(job_id: UUID, db=Depends(get_db), actor=Depends(actor_context)):
    # POST requires CSRF for cookies; neither secrets nor bearer download links enter URLs.
    job = await service.authorized_job(db, actor, job_id, delivery=True)
    data = await service.download(db, actor, job)
    extension = "zip" if job.kind == "gateway" else "ovpn"
    await db.commit()
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="factoryvpn-{job.kind}-{job.peer_id}.{extension}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/provisioning/jobs/{job_id}/activate", response_model=JobResponse)
async def activate(job_id: UUID, db=Depends(get_db), actor=Depends(actor_context)):
    job = await service.authorized_job(db, actor, job_id, delivery=True)
    await service.activate(db, actor, job)
    await db.commit()
    return service.job_response(job, actor)


@router.post("/provisioning/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel(job_id: UUID, db=Depends(get_db), actor=Depends(actor_context)):
    job = await service.authorized_job(db, actor, job_id)
    if job.state == "active":
        raise HTTPException(409, "Revoke the active credentials instead")
    await service.cancel_job(db, job, actor_id=actor.id)
    await db.commit()
    return service.job_response(job, actor)
