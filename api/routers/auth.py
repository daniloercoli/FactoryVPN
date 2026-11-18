"""Local authentication with stable identity, revocation and personal account APIs."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyCookie
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.user import User
from api.permissions import Permission
from api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    ProfileUpdateRequest,
    PasswordChangeRequest,
)
from api.schemas.admin import UserCreateRequest
from api.schemas.client import CurrentUserResponse
from api.services.auth_service import (
    verify_token,
    issue_access_token,
    authenticate_local,
    change_local_password,
)
from api.services.access_service import (
    check_permission,
    lock_actor,
    permissions_for,
    record_user_event,
    terminate_user_sessions,
    user_group_ids,
)
from api.time_utils import utc_now
from api.schemas.sso import LogoutResponse
from api.schemas.email_security import EmailChallengeResponse, EmailProof, EmailResend
from api.services import email_security as email_auth

router = APIRouter()
security = HTTPBearer(auto_error=False)
portal_security = APIKeyCookie(
    name="__Host-factoryvpn_portal", scheme_name="PortalCookie", auto_error=False
)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
    _portal_cookie: str | None = Depends(portal_security),
) -> User:
    from api.services import web_session_service as web

    cookie = request.cookies.get(web.cookie_name())
    if request.headers.get("authorization") and cookie:
        raise HTTPException(401, "Do not combine portal and Bearer credentials")
    if credentials is None:
        if request.headers.get("authorization") or not cookie:
            raise HTTPException(403, "Not authenticated")
        # Token-issuing legacy endpoints remain Bearer-only.
        if request.url.path in ("/api/v1/auth/password", "/api/v1/auth/register"):
            raise HTTPException(403, "Use the portal account endpoints")
        return await web.authenticate(request, db)
    payload = verify_token(credentials.credentials)
    try:
        identifier = UUID(payload["sub"])
        version = payload["ver"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValueError("Invalid token version")
    except (TypeError, KeyError, ValueError, AttributeError):
        raise HTTPException(401, "Invalid authentication credentials") from None
    user = await db.get(User, identifier)
    if not user or not user.is_active or user.token_version != version:
        raise HTTPException(401, "Invalid authentication credentials")
    if "sid" in payload:
        from api.services.sso_service import valid_session

        try:
            sid = UUID(payload["sid"])
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(401, "Invalid authentication credentials") from None
        if not await valid_session(db, sid, user):
            raise HTTPException(401, "SSO session no longer authorized")
        user._sso_session_id = sid
    elif user.auth_mode != "local":
        raise HTTPException(401, "SSO authentication required")
    else:
        method = payload.get("second_factor_method")
        if method is None and payload.get("email_verified") is True:
            method = "email"
        if method not in (None, "email", "console"):
            raise HTTPException(401, "Invalid authentication credentials")
        if method == "email" and payload.get("email_verified") is not True:
            raise HTTPException(401, "Invalid authentication credentials")
        if email_auth.required(user) and method is None:
            raise HTTPException(401, "Email verification required")
        user._email_verified = method == "email"
        user._second_factor_method = method
        if method:
            from datetime import datetime, timezone

            try:
                user._second_factor_at = datetime.fromtimestamp(
                    payload["second_factor_at"], timezone.utc
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                raise HTTPException(401, "Invalid authentication credentials") from None
    return user


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(403, "Global administrator required")
    return current_user


def require_permission(permission: Permission):
    async def dependency(current_user: User = Depends(get_current_user)):
        check_permission(current_user, permission)
        return current_user

    return dependency


def login_response(user):
    return LoginResponse(
        access_token=issue_access_token(user), user_id=str(user.id), username=user.username
    )


@router.post("/login", response_model=LoginResponse | EmailChallengeResponse)
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await authenticate_local(db, request.username, request.password)
    if request.emergency_code is not None:
        from api.services import console_access

        await console_access.verify(db, user, request.emergency_code)
    elif email_auth.required(user):
        return await email_auth.start(db, user, "login_api")
    user.last_login = utc_now()
    await db.commit()
    return login_response(user)


@router.post("/login/verify", response_model=LoginResponse)
async def verify_login(body: EmailProof, db: AsyncSession = Depends(get_db)):
    _, user = await email_auth.verify(db, body.challenge_token, body.code, {"login_api"})
    user.email_verified_at = user.email_verified_at or utc_now()
    user.last_login = utc_now()
    await db.commit()
    return login_response(user)


@router.post("/login/resend", response_model=EmailChallengeResponse)
async def resend_login(body: EmailResend, db: AsyncSession = Depends(get_db)):
    return await email_auth.resend(db, body.challenge_token, {"login_api"})


@router.post("/register", response_model=LoginResponse, deprecated=True)
async def register(
    request: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """Legacy global-admin creation. The account has no groups and no resource access.

    Prefer POST /admin/users to assign groups and permissions atomically.
    """
    from api.services.user_service import create_user

    user = await create_user(db, actor, UserCreateRequest(**request.model_dump()))
    return login_response(user)


async def current_user_response(db, user):
    from api.services.edit_versions import user_revision

    return {
        "revision": await user_revision(db, user),
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "email_verified": bool(user.email_verified_at),
        "email_two_step": bool(user.email_two_step),
        "email_two_step_required": email_auth.privileged(user),
        "full_name": user.full_name,
        "is_admin": user.is_admin,
        "last_login": user.last_login,
        "auth_mode": user.auth_mode,
        "permissions": sorted(permissions_for(user)),
        "group_ids": sorted(await user_group_ids(db, user.id), key=str),
    }


@router.get("/me", response_model=CurrentUserResponse)
async def get_me(
    db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)
):
    return await current_user_response(db, current_user)


@router.patch("/me", response_model=CurrentUserResponse)
async def update_profile(
    request: ProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    if_match: str | None = Header(None),
):
    await lock_actor(db, user)
    from api.services.edit_versions import user_revision, check_revision

    check_revision(user, if_match, await user_revision(db, user))
    if "email" in request.model_fields_set and request.email != user.email:
        raise HTTPException(409, "Use the verified email change procedure")
    for field in request.model_fields_set:
        setattr(user, field, getattr(request, field))
    record_user_event(db, "profile_updated", user.id, user.id, await user_group_ids(db, user.id))
    await db.commit()
    return await current_user_response(db, user)


@router.post("/password", response_model=LoginResponse)
async def change_password(
    request: PasswordChangeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await change_local_password(db, user, request.current_password, request.new_password)
    await db.commit()
    return login_response(user)


@router.post("/logout", response_model=LogoutResponse)
async def logout(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    """Invalidate all JWTs and close every VPN session belonging to this account."""
    await lock_actor(db, user)
    from api.services.sso_service import revoke_user_sso
    from api.services.oidc import OIDCAdapter
    from api.models.sso import SSOSession

    provider_ids = set(
        (
            await db.scalars(
                select(SSOSession.provider_id).where(
                    SSOSession.user_id == user.id, SSOSession.revoked_at.is_(None)
                )
            )
        ).all()
    )
    await revoke_user_sso(db, user, "User signed out")
    user.token_version += 1
    await terminate_user_sessions(db, user, reason="User signed out")
    record_user_event(db, "user_logged_out", user.id, user.id, await user_group_ids(db, user.id))
    await db.commit()
    urls = []
    for identifier in provider_ids:
        try:
            url = await OIDCAdapter(identifier).logout_url()
            if url:
                urls.append(url)
        except Exception:
            pass  # Local logout is complete; maintenance retries remote token revocation.
    return {"message": "All account sessions have been signed out", "provider_logout_urls": urls}
