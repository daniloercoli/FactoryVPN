"""Fine linea, primo accesso macchina e controllo HQ dell'arruolamento."""

from uuid import UUID
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import select
from api.db.session import get_db
from api.models.provisioning import ProvisioningJob
from api.routers.auth import get_current_user
from api.routers.provisioning import actor_context
from api.schemas.enrollment import (
    FactoryCreateRequest,
    EnrollmentData,
    EnrollmentUpdateRequest,
    EnrollmentApproval,
    EnrollmentBlock,
    EnrollmentManualRequest,
    EnrollmentResponse,
    EnrollmentVersion,
    EnrollmentConfirmation,
    EnrollmentState,
)
from api.schemas.provisioning import JobResponse
from api.schemas.client import PAGINATION_RESPONSES
from api.services import enrollment as service
from api.services import provisioning
from api.services.access_service import lock_actor
from api.services.edit_versions import check_revision
from api.services.enrollment_auth import factory_station, gateway_enrollment

router = APIRouter()


async def global_actor(actor=Depends(get_current_user), db=Depends(get_db)):
    await lock_actor(db, actor)
    if not actor.is_admin:
        raise HTTPException(403, "Global administrator required")
    return actor


async def editable(db, actor, identifier, supplied):
    row = await service.get_enrollment(db, identifier)
    check_revision(actor, supplied, (await service.response(db, row))["revision"])
    return row


async def committed_response(db, row):
    await db.flush()
    result = await service.response(db, row)
    await db.commit()
    return result


@router.post("/factory/gateways", response_model=EnrollmentResponse, status_code=201)
async def factory_create(
    body: FactoryCreateRequest,
    idempotency_key: UUID = Header(),
    station=Depends(factory_station),
    db=Depends(get_db),
):
    row = await service.create(db, station, idempotency_key, body)
    return await committed_response(db, row)


@router.get("/factory/gateways/{identifier}", response_model=EnrollmentResponse)
async def factory_status(identifier: UUID, station=Depends(factory_station), db=Depends(get_db)):
    row = await service.get_enrollment(db, identifier)
    if row.station_id != station:
        raise HTTPException(404, "Enrollment not found")
    return await committed_response(db, row)


@router.get("/gateway-enrollment/{identifier}", response_model=EnrollmentResponse)
async def enrollment_status(row=Depends(gateway_enrollment), db=Depends(get_db)):
    await provisioning.expire_jobs(db)
    return await committed_response(db, row)


@router.put("/gateway-enrollment/{identifier}", response_model=EnrollmentResponse)
async def present(body: EnrollmentData, row=Depends(gateway_enrollment), db=Depends(get_db)):
    await service.update(db, row, body.model_dump(exclude_unset=True))
    return await committed_response(db, row)


@router.post(
    "/gateway-enrollment/{identifier}/package", response_model=EnrollmentResponse, status_code=202
)
async def package(body: EnrollmentVersion, row=Depends(gateway_enrollment), db=Depends(get_db)):
    await service.request_package(db, row, body.configuration_version)
    return await committed_response(db, row)


@router.post(
    "/gateway-enrollment/{identifier}/package/{job_id}/download",
    response_class=Response,
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def download(job_id: UUID, row=Depends(gateway_enrollment), db=Depends(get_db)):
    job = await service.delivery_job(db, row, job_id)
    data = await provisioning.download(db, None, job)
    await db.commit()
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="factoryvpn-gateway-{row.id}.zip"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/gateway-enrollment/{identifier}/package/{job_id}/confirm",
    response_model=EnrollmentConfirmation,
)
async def confirm(
    job_id: UUID, body: EnrollmentVersion, row=Depends(gateway_enrollment), db=Depends(get_db)
):
    job = await service.delivery_job(db, row, job_id, body.configuration_version)
    await provisioning.activate(db, None, job)
    await db.commit()
    return {
        "activated": True,
        "gateway_id": row.id,
        "configuration_version": row.configuration_version,
    }


@router.get(
    "/admin/enrollments", response_model=list[EnrollmentResponse], responses=PAGINATION_RESPONSES
)
async def list_enrollments(
    response: Response,
    q: str | None = Query(None, max_length=100),
    state: EnrollmentState | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    actor=Depends(global_actor),
    db=Depends(get_db),
):
    await provisioning.expire_jobs(db)
    items, total = await service.list_enrollments(db, q=q, state=state, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    await db.commit()
    return items


@router.get("/admin/enrollments/{identifier}", response_model=EnrollmentResponse)
async def detail(identifier: UUID, actor=Depends(global_actor), db=Depends(get_db)):
    await provisioning.expire_jobs(db)
    return await committed_response(db, await service.get_enrollment(db, identifier))


@router.patch("/admin/enrollments/{identifier}", response_model=EnrollmentResponse)
async def update(
    identifier: UUID,
    body: EnrollmentUpdateRequest,
    if_match: str | None = Header(None),
    actor=Depends(global_actor),
    db=Depends(get_db),
):
    row = await editable(db, actor, identifier, if_match)
    await service.update(db, row, body.model_dump(exclude_unset=True), actor=actor)
    return await committed_response(db, row)


@router.post("/admin/enrollments/{identifier}/approve", response_model=EnrollmentResponse)
async def approve(
    identifier: UUID,
    body: EnrollmentApproval,
    if_match: str | None = Header(None),
    actor=Depends(global_actor),
    db=Depends(get_db),
):
    row = await editable(db, actor, identifier, if_match)
    await service.approve(db, row, actor, body.group_id)
    return await committed_response(db, row)


@router.post("/admin/enrollments/{identifier}/block", response_model=EnrollmentResponse)
async def block(
    identifier: UUID,
    body: EnrollmentBlock,
    if_match: str | None = Header(None),
    actor=Depends(global_actor),
    db=Depends(get_db),
):
    row = await editable(db, actor, identifier, if_match)
    await service.block(db, row, actor, body.reason)
    return await committed_response(db, row)


@router.post("/admin/enrollments/{identifier}/manual", response_model=JobResponse, status_code=202)
async def manual(
    identifier: UUID,
    body: EnrollmentManualRequest,
    idempotency_key: UUID = Header(),
    if_match: str | None = Header(None),
    actor=Depends(actor_context),
    db=Depends(get_db),
):
    if not actor.is_admin:
        raise HTTPException(403, "Global administrator required")
    previous = await db.scalar(
        select(ProvisioningJob.id).where(
            ProvisioningJob.actor_id == actor.id,
            ProvisioningJob.request_key == idempotency_key,
        )
    )
    # Una risposta persa ha già cambiato la revisione. Il replay verifica corpo
    # e accesso originali in create_job; solo un nuovo comando richiede If-Match.
    row = (
        await service.get_enrollment(db, identifier)
        if previous is not None
        else await editable(db, actor, identifier, if_match)
    )
    job = await service.manual(db, row, actor, body, idempotency_key)
    await db.commit()
    return provisioning.job_response(job, actor)


@router.post("/admin/enrollments/{identifier}/resume", response_model=EnrollmentResponse)
async def resume(
    identifier: UUID,
    if_match: str | None = Header(None),
    actor=Depends(global_actor),
    db=Depends(get_db),
):
    row = await editable(db, actor, identifier, if_match)
    await service.resume(db, row, actor)
    return await committed_response(db, row)
