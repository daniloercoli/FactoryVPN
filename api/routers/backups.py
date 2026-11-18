"""Global administrators only: encrypted backups and queued recovery commands."""

from datetime import datetime, timedelta
import os
import asyncio
import shutil
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.config import settings
from api.db.session import get_db
from api.models.sso import SSOSession
from api.models.user import User
from api.routers.auth import get_current_user
from api.schemas.backup import (
    BackupCommand,
    BackupReauthentication,
    BackupRetention,
    BackupJobResponse,
    BackupStatus,
)
from api.services import backup_access as access
from api.services.auth_service import verify_password
from api.services.web_session_service import valid_web_session
from api.time_utils import utc_now
from factoryvpn_common.backup_archive import BackupError, CHUNK
from factoryvpn_common.backup_catalog import public_job
from api.schemas.email_security import EmailChallengeResponse, EmailComplete

router = APIRouter()


def translate(exc):
    code = str(exc)
    raise HTTPException(404 if code == "backup_not_found" else 409, "Backup: " + code) from None


def enqueue(control, kind, user, row, key, backup_id=None, **kwargs):
    try:
        job = control.enqueue(kind, access.actor_claim(user, row), key, backup_id, **kwargs)
    except BackupError as exc:
        translate(exc)
    control.event("backup_requested", str(user.id), job["id"], kind=kind)
    return job


@router.get("", response_model=BackupStatus)
async def status(db=Depends(get_db), user=Depends(get_current_user)):
    row = await access.authorize(db, user)
    fresh = access.fresh_authentication(row, user)
    provider = await db.get(SSOSession, row.sso_session_id) if row.sso_session_id else None
    base = {
        "enabled": False,
        "coordinator_online": False,
        "needs_reauthentication": not fresh,
        "sso_provider_id": provider.provider_id if provider else None,
        "maximum_bytes": settings.BACKUP_MAX_BYTES,
        "retention": 10,
        "backups": [],
        "jobs": [],
    }
    try:
        control = access.catalog()
    except HTTPException as exc:
        if exc.status_code == 503:
            return base
        raise
    heartbeat = control.meta("heartbeat")
    online = bool(
        heartbeat and datetime.fromisoformat(heartbeat) > utc_now() - timedelta(seconds=30)
    )
    # A long operation has a durable running state even between heartbeat writes.
    jobs = control.jobs()
    return {
        **base,
        "enabled": True,
        "coordinator_online": online,
        "retention": int(control.meta("retention") or "10"),
        "backups": control.backups(),
        "jobs": [public_job(job) for job in jobs],
    }


@router.post("/reauthenticate", response_model=EmailChallengeResponse | EmailComplete)
async def reauthenticate(
    body: BackupReauthentication, db=Depends(get_db), user=Depends(get_current_user)
):
    row = await access.authorize(db, user)
    if user.auth_mode != "local":
        raise HTTPException(409, "Use the SSO provider to authenticate again")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid confirmation password")
    if body.emergency_code is not None:
        from api.services import console_access

        await console_access.confirm(db, user, body.emergency_code, row)
        await db.commit()
        return {"completed": True}
    from api.services import email_security

    return await email_security.start(db, user, "confirm", session_id=row.id)


@router.post("", response_model=BackupJobResponse, status_code=202)
async def create(
    idempotency_key: UUID = Header(), db=Depends(get_db), user=Depends(get_current_user)
):
    row = await access.authorize(db, user, fresh=True)
    result = enqueue(access.catalog(), "create", user, row, idempotency_key)
    await db.commit()
    return public_job(result)


@router.put("/retention", status_code=204)
async def retention(body: BackupRetention, db=Depends(get_db), user=Depends(get_current_user)):
    await access.authorize(db, user, fresh=True)
    control = access.catalog()
    control.meta("retention", body.count)
    control.event("backup_retention_changed", str(user.id), count=body.count)
    await db.commit()


@router.post(
    "/import",
    response_model=BackupJobResponse,
    status_code=202,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload(
    request: Request,
    idempotency_key: UUID = Header(),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    row = await access.authorize(db, user, fresh=True)
    control = access.catalog()
    if request.headers.get("content-type") != "application/octet-stream":
        raise HTTPException(415, "Encrypted backup required")
    if shutil.disk_usage(control.root).free < settings.BACKUP_MAX_BYTES * 4:
        raise HTTPException(507, "Backup: insufficient_disk")
    if request.headers.get("content-length", "0").isdigit():
        if int(request.headers.get("content-length", "0")) > settings.BACKUP_MAX_BYTES:
            raise HTTPException(413, "Backup exceeds upload limit")
    job = enqueue(control, "import", user, row, idempotency_key, uploading=True)
    await db.commit()  # Never hold the lifecycle lock during a transfer.
    path = control.root / "imports" / (job["id"] + ".upload")
    if job["state"] != "uploading" or path.exists():
        raise HTTPException(409, "Backup upload already submitted")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise HTTPException(409, "Backup upload already submitted") from None
    try:
        total = 0
        # O_EXCL prevents two uploads sharing an idempotency key from replacing a file.
        with os.fdopen(descriptor, "wb") as stream:
            async with asyncio.timeout(300):
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > settings.BACKUP_MAX_BYTES:
                        raise HTTPException(413, "Backup exceeds upload limit")
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        await access.authorize(db, user, fresh=True)
        try:
            control.finish_upload(job["id"])
        except BackupError as exc:
            translate(exc)
        await db.commit()
        return public_job({**job, "state": "queued"})
    except BaseException:
        path.unlink(missing_ok=True)
        control.update_job(job["id"], state="failed", error="upload_failed")
        raise


@router.post("/{backup_id}/commands", response_model=BackupJobResponse, status_code=202)
async def command(
    backup_id: UUID,
    body: BackupCommand,
    idempotency_key: UUID = Header(),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    row = await access.authorize(db, user, fresh=True)
    control = access.catalog()
    try:
        record = control.backup(backup_id)
    except BackupError as exc:
        translate(exc)
    if body.action == "restore" and body.confirmation != "RIPRISTINA " + str(backup_id):
        raise HTTPException(422, "Backup restore requires explicit confirmation")
    if body.action == "delete" and record["protected"]:
        raise HTTPException(409, "Backup: protected_backup")
    result = enqueue(control, body.action, user, row, idempotency_key, backup_id)
    await db.commit()
    return public_job(result)


@router.post(
    "/{backup_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
async def download(backup_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    row = await access.authorize(db, user, fresh=True)
    control = access.catalog()
    try:
        control.backup(backup_id)
    except BackupError as exc:
        translate(exc)
    path = access.archive_path(backup_id)
    user_id, session_id = user.id, row.id
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    await db.commit()
    control.event("backup_download_started", str(user_id), str(backup_id))

    async def chunks():
        with open(path, "rb") as stream:
            async with factory() as connection:
                while data := stream.read(CHUNK):
                    actor = await connection.get(User, user_id, populate_existing=True)
                    if (
                        not actor
                        or not actor.is_admin
                        or access.maintenance_active()
                        or not await valid_web_session(connection, session_id, actor)
                    ):
                        return  # Revocation mid-stream leaves an unusable authenticated archive.
                    await connection.rollback()
                    yield data
        control.event("backup_download_completed", str(user_id), str(backup_id))

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="factoryvpn-{backup_id}.fvbackup"',
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(path.stat().st_size),
        },
    )
