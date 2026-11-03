from fastapi import APIRouter, Depends
from api.db.session import get_db
from api.routers.auth import require_admin
from api.schemas.server_status import ServerStatus, SmtpRequest, SmtpResult
from api.services import server_status as service

router = APIRouter()


@router.get("/admin/server", response_model=ServerStatus)
async def server_status(actor=Depends(require_admin), db=Depends(get_db)):
    return await service.status(db, actor)


@router.post("/admin/server/smtp-check", response_model=SmtpResult)
async def smtp_check(body: SmtpRequest, actor=Depends(require_admin), db=Depends(get_db)):
    return await service.check(db, actor, body)
