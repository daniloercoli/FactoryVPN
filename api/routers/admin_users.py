"""Group-scoped account administration."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, Header
from sqlalchemy import or_, select, func
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.user import User
from api.models.group import UserGroup
from api.permissions import ALL_PERMISSIONS, Permission
from api.routers.auth import get_current_user, require_permission
from api.schemas.admin import UserCreateRequest, UserResponse, UserUpdateRequest
from api.services import user_service
from api.services.access_service import users_scope

router = APIRouter()


@router.get("/permissions", response_model=list[str])
async def permission_catalog(_: User = Depends(get_current_user)):
    return ALL_PERMISSIONS


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    response: Response,
    q: str | None = Query(None, max_length=100),
    group_id: UUID | None = None,
    is_active: bool | None = None,
    auth_mode: Literal["local", "sso"] | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
):
    query = select(User).where(users_scope(actor))
    if group_id is not None:
        query = query.where(
            User.id.in_(select(UserGroup.user_id).where(UserGroup.group_id == group_id))
        )
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    if auth_mode is not None:
        query = query.where(User.auth_mode == auth_mode)
    if q:
        query = query.where(
            or_(
                User.username.icontains(q, autoescape=True),
                User.full_name.icontains(q, autoescape=True),
                User.email.icontains(q, autoescape=True),
            )
        )
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(query.subquery()))
    )
    users = (
        await db.scalars(query.order_by(User.username, User.id).offset(offset).limit(limit))
    ).all()
    return [await user_service.user_response(db, user) for user in users]


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    request: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
):
    return await user_service.user_response(db, await user_service.create_user(db, actor, request))


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
):
    return await user_service.user_response(db, await user_service.managed_user(db, actor, user_id))


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    request: UserUpdateRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
    if_match: str | None = Header(None),
):
    return await user_service.user_response(
        db, await user_service.update_user(db, actor, user_id, request, if_match=if_match)
    )


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.USERS_MANAGE)),
    if_match: str | None = Header(None),
):
    await user_service.delete_user(db, actor, user_id, if_match=if_match)
