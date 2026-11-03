"""Explicit identity linking and authentication policy, restricted to global administrators."""

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.session import get_db
from api.models.user import User
from api.models.sso import ExternalIdentity
from api.routers.auth import require_admin
from api.routers.sso import adapter_for
from api.schemas.sso import IdentityLinkRequest, IdentityResponse, AuthenticationModeRequest
from api.schemas.admin import UserResponse
from api.services.access_service import (
    lock_actor,
    record_user_event,
    user_group_ids,
    terminate_user_sessions,
)
from api.services.user_service import user_response, preserve_local_admin
from api.services.auth_service import hash_password
from api.services.sso_service import revoke_user_sso
from api.services.edit_versions import user_revision, check_revision

router = APIRouter()


async def target_user(db, actor, user_id):
    await lock_actor(db, actor)
    if not actor.is_admin:
        raise HTTPException(403, "Global administrator required")
    user = await db.get(User, user_id, populate_existing=True)
    if not user:
        raise HTTPException(404, "User not found")
    return user


@router.get("/users/{user_id}/identities", response_model=list[IdentityResponse])
async def identities(
    user_id: UUID, db: AsyncSession = Depends(get_db), actor: User = Depends(require_admin)
):
    await target_user(db, actor, user_id)
    return (
        await db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user_id))
    ).all()


@router.post("/users/{user_id}/identities", response_model=IdentityResponse, status_code=201)
async def link_identity(
    user_id: UUID,
    body: IdentityLinkRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
    if_match: str | None = Header(None),
):
    user = await target_user(db, actor, user_id)
    check_revision(actor, if_match, await user_revision(db, user))
    adapter = adapter_for(body.provider_id)
    identity = ExternalIdentity(
        user_id=user.id,
        provider_id=body.provider_id,
        issuer=adapter.provider.issuer,
        subject=body.subject,
    )
    db.add(identity)
    record_user_event(
        db, "sso_identity_linked", actor.id, user.id, await user_group_ids(db, user.id)
    )
    await db.commit()
    await db.refresh(identity)
    return identity


@router.delete("/users/{user_id}/identities/{identity_id}", status_code=204)
async def unlink_identity(
    user_id: UUID,
    identity_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
    if_match: str | None = Header(None),
):
    user = await target_user(db, actor, user_id)
    check_revision(actor, if_match, await user_revision(db, user))
    identity = await db.get(ExternalIdentity, identity_id)
    if not identity or identity.user_id != user.id:
        raise HTTPException(404, "Identity not found")
    await revoke_user_sso(db, user, "External identity unlinked")
    user.token_version += 1
    await terminate_user_sessions(db, user, reason="External identity unlinked", actor_id=actor.id)
    await db.delete(identity)
    record_user_event(
        db, "sso_identity_unlinked", actor.id, user.id, await user_group_ids(db, user.id)
    )
    await db.commit()


@router.put("/users/{user_id}/authentication", response_model=UserResponse)
async def authentication_mode(
    user_id: UUID,
    body: AuthenticationModeRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_admin),
    if_match: str | None = Header(None),
):
    user = await target_user(db, actor, user_id)
    check_revision(actor, if_match, await user_revision(db, user))
    if body.auth_mode == "sso":
        await preserve_local_admin(db, user)
        if not await db.scalar(
            select(ExternalIdentity.id).where(ExternalIdentity.user_id == user.id).limit(1)
        ):
            raise HTTPException(409, "Link an external identity before enabling SSO")
    await revoke_user_sso(db, user, "Authentication method changed")
    await terminate_user_sessions(
        db, user, reason="Authentication method changed", actor_id=actor.id
    )
    user.auth_mode = body.auth_mode
    user.password_hash = hash_password(body.password) if body.password else None
    user.token_version += 1
    record_user_event(
        db, "authentication_method_changed", actor.id, user.id, await user_group_ids(db, user.id)
    )
    await db.commit()
    return await user_response(db, user)
