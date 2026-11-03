"""Account administration with bounded delegation and atomic security audit."""

from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from api.models.group import UserGroup
from api.models.session import Session
from api.models.user import User
from api.permissions import Permission
from api.schemas.admin import UserResponse
from api.services.auth_service import hash_password
from api.services.access_service import (
    check_permission,
    lock_actor,
    permissions_for,
    record_user_event,
    terminate_user_sessions,
    user_group_ids,
    users_scope,
    validate_group_ids,
)
from api.services.lifecycle import occupied
from api.services.edit_versions import user_revision, check_revision


async def user_response(db, user):
    from api.services.email_security import privileged

    groups = await user_group_ids(db, user.id)
    return UserResponse(
        revision=await user_revision(db, user, groups),
        id=str(user.id),
        auth_mode=user.auth_mode,
        username=user.username,
        email=user.email,
        email_verified=bool(user.email_verified_at),
        email_two_step=bool(user.email_two_step),
        email_two_step_required=privileged(user),
        full_name=user.full_name,
        is_active=user.is_active,
        is_admin=user.is_admin,
        permissions=sorted(user.permissions),
        group_ids=sorted(groups, key=str),
        created_at=user.created_at,
        last_login=user.last_login,
    )


async def managed_user(db, actor, user_id, *, mutate=False):
    check_permission(actor, Permission.USERS_MANAGE)
    user = await db.scalar(select(User).where(User.id == user_id, users_scope(actor)))
    if user is None:
        raise HTTPException(404, "User not found")
    if mutate and not actor.is_admin and not permissions_for(user) <= permissions_for(actor):
        raise HTTPException(403, "Cannot modify an account with greater permissions")
    return user


async def create_user(db, actor, request):
    await lock_actor(db, actor)
    check_permission(actor, Permission.USERS_MANAGE)
    groups = await validate_group_ids(db, actor, request.group_ids)
    if request.auth_mode == "sso" and not actor.is_admin:
        raise HTTPException(403, "Only global administrators can create SSO accounts")
    if not actor.is_admin:
        if request.is_admin or not set(request.permissions) <= permissions_for(actor):
            raise HTTPException(403, "Cannot grant greater permissions")
        if not groups:
            raise HTTPException(403, "Delegated accounts require an assigned group")
    user = User(
        id=uuid4(),
        username=request.username,
        password_hash=hash_password(request.password) if request.password else None,
        auth_mode=request.auth_mode,
        email=request.email,
        full_name=request.full_name,
        is_admin=request.is_admin,
        is_active=True,
        permissions=[p.value for p in request.permissions],
        token_version=1,
    )
    db.add(user)
    await db.flush()
    db.add_all([UserGroup(user_id=user.id, group_id=group_id) for group_id in groups])
    record_user_event(db, "user_created", actor.id, user.id, groups)
    await db.commit()
    await db.refresh(user)
    return user


async def preserve_last_admin(db, user, *, active, admin):
    if user.is_admin and user.is_active and not (active and admin):
        count = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True), User.is_active.is_(True))
        )
        if count <= 1:
            raise HTTPException(409, "Cannot remove the last active global administrator")
        await preserve_local_admin(db, user)


async def preserve_local_admin(db, user):
    if user.is_admin and user.is_active and user.auth_mode == "local":
        count = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True), User.is_active.is_(True), User.auth_mode == "local")
        )
        if count <= 1:
            raise HTTPException(409, "Cannot remove the last active local global administrator")


async def update_user(db, actor, user_id, request, *, if_match=None):
    await lock_actor(db, actor)
    user = await managed_user(db, actor, user_id, mutate=True)
    check_revision(actor, if_match, await user_revision(db, user))
    if "email" in request.model_fields_set and request.email != user.email:
        raise HTTPException(409, "Use the email recovery procedure")
    if request.password is not None:
        from api.services.email_security import require_recent

        require_recent(actor)
    old_groups = await user_group_ids(db, user.id)
    new_groups = old_groups
    if request.group_ids is not None:
        new_groups = await validate_group_ids(db, actor, request.group_ids)
    if not actor.is_admin:
        if request.is_admin is not None:
            raise HTTPException(403, "Only global administrators can change global roles")
        if request.permissions is not None and not set(request.permissions) <= permissions_for(
            actor
        ):
            raise HTTPException(403, "Cannot grant greater permissions")
        if not new_groups:
            raise HTTPException(403, "Delegated accounts require an assigned group")
    await preserve_last_admin(
        db,
        user,
        active=user.is_active if request.is_active is None else request.is_active,
        admin=user.is_admin if request.is_admin is None else request.is_admin,
    )
    fields = request.model_fields_set
    for field in ("email", "full_name"):
        if field in fields:
            setattr(user, field, getattr(request, field))
    for field in ("is_active", "is_admin"):
        if getattr(request, field) is not None:
            setattr(user, field, getattr(request, field))
    if request.permissions is not None:
        user.permissions = [p.value for p in request.permissions]
    if request.password is not None:
        if user.auth_mode != "local":
            raise HTTPException(409, "Password resets are managed by the SSO provider")
        user.password_hash = hash_password(request.password)
    if request.group_ids is not None:
        await db.execute(delete(UserGroup).where(UserGroup.user_id == user.id))
        db.add_all([UserGroup(user_id=user.id, group_id=group) for group in new_groups])
    if fields & {"is_active", "is_admin", "password", "permissions", "group_ids"}:
        from api.services.sso_service import revoke_user_sso

        await revoke_user_sso(db, user, "Account credentials or access permissions changed")
        user.token_version += 1
        await db.flush()
        await terminate_user_sessions(
            db,
            user,
            reason="Account credentials or access permissions changed",
            only_unauthorized=request.password is None and user.is_active,
            actor_id=actor.id,
        )
        if not user.is_active:
            from api.services.provisioning import invalidate_peer

            # Persist revocation before acknowledging disable, independently of
            # issuer availability or a subsequent rapid re-enable.
            await invalidate_peer(db, actor, "technician", user)
    record_user_event(db, "user_updated", actor.id, user.id, old_groups | new_groups)
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db, actor, user_id, *, if_match=None):
    await lock_actor(db, actor)
    user = await managed_user(db, actor, user_id, mutate=True)
    check_revision(actor, if_match, await user_revision(db, user))
    if user.id == actor.id:
        raise HTTPException(400, "Cannot delete the current account")
    await preserve_last_admin(db, user, active=False, admin=False)
    if await db.scalar(
        select(Session.id).where(Session.technician_id == user.id, occupied()).limit(1)
    ):
        raise HTTPException(409, "Close sessions and wait for network cleanup first")
    groups = await user_group_ids(db, user.id)
    from api.services.sso_service import revoke_user_sso

    await revoke_user_sso(db, user, "Account deleted")
    from api.services.provisioning import invalidate_peer

    await invalidate_peer(db, actor, "technician", user)
    # SQL deletion preserves database SET NULL semantics for historical sessions.
    await db.execute(delete(User).where(User.id == user.id))
    record_user_event(db, "user_deleted", actor.id, user.id, groups)
    await db.commit()
