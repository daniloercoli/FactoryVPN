"""Personal email security and explicitly authorized recovery. No mail or code listing API."""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from uuid import UUID
from api.db.session import get_db
from api.models import User, WebSession
from api.routers.web_auth import browser_user, payload
from api.schemas.email_security import (
    EmailStart,
    EmailProof,
    EmailResend,
    EmailStatus,
    EmailChallengeResponse,
    EmailComplete,
    EmailRecovery,
)
from api.schemas.backup import BackupReauthentication
from api.schemas.web import WebSessionResponse
from api.services import email_security as service, mail_service, web_session_service as web
from api.services.access_service import lock_actor, record_user_event, user_group_ids
from api.services.auth_service import verify_password
from api.time_utils import utc_now

router = APIRouter()
PURPOSES = {"enable", "disable", "change_old", "change_new", "confirm"}


@router.get("/web/email", response_model=EmailStatus)
async def status(user=Depends(browser_user)):
    return {
        "enabled": service.required(user),
        "required": service.privileged(user),
        "verified": bool(user.email_verified_at),
        "email": user.email,
        "smtp_configured": mail_service.configured(),
    }


async def local_password(db, user, password):
    await lock_actor(db, user)
    if user.auth_mode != "local":
        raise HTTPException(409, "Email verification is managed by the SSO provider")
    if not verify_password(password, user.password_hash):
        raise HTTPException(400, "Invalid confirmation password")


@router.post("/web/email/confirm", response_model=EmailChallengeResponse | EmailComplete)
async def confirm(body: BackupReauthentication, db=Depends(get_db), user=Depends(browser_user)):
    await local_password(db, user, body.password)
    if body.emergency_code is not None:
        from api.services import console_access

        session = await db.get(WebSession, user._web_session_id)
        await console_access.confirm(db, user, body.emergency_code, session)
        await db.commit()
        return {"completed": True}
    return await service.start(db, user, "confirm", session_id=user._web_session_id)


@router.post("/web/email/start", response_model=EmailChallengeResponse)
async def start(body: EmailStart, db=Depends(get_db), user=Depends(browser_user)):
    await local_password(db, user, body.password)
    if body.action == "disable":
        if service.privileged(user):
            raise HTTPException(403, "Administrators must keep two-step verification")
        if not user.email_two_step:
            raise HTTPException(409, "Email verification is not enabled")
        return await service.start(db, user, "disable", session_id=user._web_session_id)
    destination = body.email if body.action == "change" else user.email or body.email
    try:
        destination = mail_service.email_address(destination)
    except ValueError:
        raise HTTPException(422, "Valid email required") from None
    if await db.scalar(select(User.id).where(User.email == destination, User.id != user.id)):
        raise HTTPException(409, "Email address unavailable")
    if body.action == "enable" and user.email_two_step:
        raise HTTPException(409, "Email verification already enabled")
    if body.action == "change" and user.email_verified_at:
        return await service.start(
            db, user, "change_old", next_email=destination, session_id=user._web_session_id
        )
    return await service.start(
        db,
        user,
        "enable" if body.action == "enable" else "change_new",
        email=destination,
        session_id=user._web_session_id,
    )


@router.post("/web/email/resend", response_model=EmailChallengeResponse)
async def resend(body: EmailResend, db=Depends(get_db), user=Depends(browser_user)):
    return await service.resend(db, body.challenge_token, PURPOSES, actor=user)


@router.post(
    "/web/email/verify", response_model=WebSessionResponse | EmailChallengeResponse | EmailComplete
)
async def verify(
    body: EmailProof,
    request: Request,
    response: Response,
    db=Depends(get_db),
    user=Depends(browser_user),
):
    row, user = await service.verify(db, body.challenge_token, body.code, PURPOSES, actor=user)
    if row.purpose == "change_old":
        return await service.start(
            db,
            user,
            "change_new",
            email=row.next_email,
            session_id=row.web_session_id,
            transition=True,
        )
    if row.purpose == "confirm":
        session = await db.get(WebSession, row.web_session_id)
        session.reauthenticated_at = session.second_factor_at = utc_now()
        session.second_factor_method = "email"
        user.email_verified_at = user.email_verified_at or utc_now()
        await db.commit()
        return {"completed": True}
    if row.purpose in {"enable", "change_new"}:
        if await db.scalar(select(User.id).where(User.email == row.email, User.id != user.id)):
            raise HTTPException(409, "Email address unavailable")
        user.email, user.email_verified_at = row.email, utc_now()
    if row.purpose in {"enable", "disable"}:
        if row.purpose == "disable" and service.privileged(user):
            raise HTTPException(403, "Administrators must keep two-step verification")
        user.email_two_step = row.purpose == "enable"
    await service.invalidate_access(db, user, user.id)
    record_user_event(
        db, "email_security_changed", user.id, user.id, await user_group_ids(db, user.id)
    )
    session, csrf = await web.issue(
        db, response, user, previous_secret=request.cookies.get(web.cookie_name())
    )
    await db.commit()
    return await payload(db, user, session, csrf)


@router.post("/admin/users/{user_id}/email-recovery", response_model=EmailComplete)
async def recover(
    user_id: UUID,
    body: EmailRecovery,
    if_match: str | None = Header(None),
    db=Depends(get_db),
    actor=Depends(browser_user),
):
    from api.services.backup_access import authorize
    from api.services.edit_versions import user_revision, check_revision

    await authorize(db, actor, fresh=True)
    service.require_recent(actor)
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if actor.id == user.id:
        raise HTTPException(409, "Use personal email change or console recovery")
    check_revision(actor, if_match, await user_revision(db, user))
    if await db.scalar(select(User.id).where(User.email == body.email, User.id != user.id)):
        raise HTTPException(409, "Email address unavailable")
    await service.recover_email(db, user, body.email, actor.id)
    await db.commit()
    return {"completed": True}
