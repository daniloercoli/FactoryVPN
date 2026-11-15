"""Gateway-only HTTPS certificate renewal; no browser session or VPN required."""

from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException
from api.db.session import get_db
from api.services.machine_auth import get_gateway_identity
from api.services import certificate_renewal as service
from api.schemas.certificate_renewal import (
    RenewalChallengeRequest,
    RenewalProof,
    RenewalActivation,
    RenewalResponse,
    RenewalStatus,
)

router = APIRouter()


@router.get("/certificates", response_model=RenewalStatus)
async def status(identity=Depends(get_gateway_identity), db=Depends(get_db)):
    result = await service.status(db, UUID(identity))
    await db.commit()
    return result


@router.post("/certificates/renewals", response_model=RenewalResponse, status_code=201)
async def challenge(
    request: RenewalChallengeRequest,
    idempotency_key: UUID = Header(),
    identity=Depends(get_gateway_identity),
    db=Depends(get_db),
):
    try:
        job = await service.challenge(db, UUID(identity), idempotency_key, request)
    except ValueError:
        raise HTTPException(422, "A valid P-256 client CSR is required") from None
    result = service.response(job)
    await db.commit()
    return result


@router.get("/certificates/renewals/{job_id}", response_model=RenewalResponse)
async def job_status(job_id: UUID, identity=Depends(get_gateway_identity), db=Depends(get_db)):
    job = await service.authorized_job(db, UUID(identity), job_id)
    result = service.response(job)
    await db.commit()
    return result


@router.post("/certificates/renewals/{job_id}/submit", response_model=RenewalResponse)
async def submit(
    job_id: UUID, proof: RenewalProof, identity=Depends(get_gateway_identity), db=Depends(get_db)
):
    job = await service.authorized_job(db, UUID(identity), job_id)
    await service.submit(db, job, proof.signature)
    result = service.response(job)
    await db.commit()
    return result


@router.post("/certificates/renewals/{job_id}/activate", response_model=RenewalResponse)
async def activate(
    job_id: UUID,
    proof: RenewalActivation,
    identity=Depends(get_gateway_identity),
    db=Depends(get_db),
):
    job = await service.authorized_job(db, UUID(identity), job_id)
    await service.activate(db, job, proof)
    result = service.response(job)
    await db.commit()
    return result


@router.post("/certificates/renewals/{job_id}/confirm", response_model=RenewalResponse)
async def confirm(
    job_id: UUID, proof: RenewalProof, identity=Depends(get_gateway_identity), db=Depends(get_db)
):
    job = await service.authorized_job(db, UUID(identity), job_id)
    await service.confirm(db, job, proof.signature)
    result = service.response(job)
    await db.commit()
    return result


@router.post("/certificates/renewals/{job_id}/cancel", response_model=RenewalResponse)
async def cancel(job_id: UUID, identity=Depends(get_gateway_identity), db=Depends(get_db)):
    job = await service.authorized_job(db, UUID(identity), job_id)
    if job.state in ("active", "confirmed"):
        raise HTTPException(409, "An activated certificate cannot be rolled back")
    await service.cancel(db, job)
    result = service.response(job)
    await db.commit()
    return result
