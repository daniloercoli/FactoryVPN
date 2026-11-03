"""Explicit human-managed groups; members can discover only their own groups."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Header
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.group import Group, UserGroup
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.user import User
from api.permissions import Permission
from api.routers.auth import get_current_user, require_permission
from api.schemas.group import GroupCreateRequest, GroupResponse, GroupUpdateRequest
from api.services.access_service import (
    check_permission,
    group_for_user,
    lock_actor,
    permissions_for,
    record_event,
    scope,
    users_scope,
)
from api.services.lifecycle import occupied
from api.services.edit_versions import group_revision, check_revision

router = APIRouter()


async def group_response(db, group):
    response = GroupResponse.model_validate(group, from_attributes=True)
    response.revision = await group_revision(db, group)
    return response


@router.get("/groups/", response_model=list[GroupResponse])
async def my_groups(
    response: Response,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    query = select(Group).where(scope(Group.id, actor))
    if q:
        query = query.where(Group.name.icontains(q, autoescape=True))
    response.headers["X-Total-Count"] = str(
        await db.scalar(select(func.count()).select_from(query.subquery()))
    )
    rows = (
        await db.scalars(query.order_by(Group.name, Group.id).offset(offset).limit(limit))
    ).all()
    return [await group_response(db, row) for row in rows]


@router.get("/admin/groups", response_model=list[GroupResponse])
async def list_groups(
    response: Response,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GROUPS_MANAGE)),
):
    return await my_groups(response, q, limit, offset, db, actor)


@router.post("/admin/groups", response_model=GroupResponse, status_code=201)
async def create_group(
    request: GroupCreateRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GROUPS_MANAGE)),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GROUPS_MANAGE)
    group = Group(id=uuid4(), **request.model_dump())
    db.add(group)
    await db.flush()
    if not actor.is_admin:
        db.add(UserGroup(user_id=actor.id, group_id=group.id))
    record_event(db, "group_created", actor.id, group_id=group.id)
    await db.commit()
    return await group_response(db, group)


@router.get("/admin/groups/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GROUPS_MANAGE)),
):
    return await group_response(db, await group_for_user(db, group_id, actor))


@router.put("/admin/groups/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: UUID,
    request: GroupUpdateRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GROUPS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GROUPS_MANAGE)
    group = await group_for_user(db, group_id, actor)
    check_revision(actor, if_match, await group_revision(db, group))
    group.name, group.description = request.name, request.description
    record_event(db, "group_updated", actor.id, group_id=group.id)
    await db.commit()
    return await group_response(db, group)


@router.delete("/admin/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_permission(Permission.GROUPS_MANAGE)),
    if_match: str | None = Header(None),
):
    await lock_actor(db, actor)
    check_permission(actor, Permission.GROUPS_MANAGE)
    group = await group_for_user(db, group_id, actor)
    check_revision(actor, if_match, await group_revision(db, group))
    if await db.scalar(select(Gateway.id).where(Gateway.group_id == group.id).limit(1)):
        raise HTTPException(409, "Reassign the group's gateways before deleting it")
    if await db.scalar(select(Session.id).where(Session.group_id == group.id, occupied()).limit(1)):
        raise HTTPException(409, "Wait for network cleanup before deleting the group")
    # Deleting a populated group changes other accounts' membership, so it also
    # requires the account-management capability. No cross-group account edits.
    if await db.scalar(select(UserGroup.user_id).where(UserGroup.group_id == group.id).limit(1)):
        check_permission(actor, Permission.USERS_MANAGE)
        if not actor.is_admin:
            members = select(User).join(UserGroup).where(UserGroup.group_id == group.id)
            outside_scope = await db.scalar(members.where(~users_scope(actor)).limit(1))
            if outside_scope:
                raise HTTPException(
                    403, "Group contains accounts outside your administration scope"
                )
            accounts = (await db.scalars(members)).all()
            if any(not permissions_for(member) <= permissions_for(actor) for member in accounts):
                raise HTTPException(
                    403, "Group contains accounts outside your administration scope"
                )
    await db.execute(delete(Group).where(Group.id == group.id))
    record_event(db, "group_deleted", actor.id, group_id=group.id)
    await db.commit()
