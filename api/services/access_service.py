"""Resource authorization shared by human APIs and the VPN lifecycle."""

from api.services.session_events import terminal_event

from fastapi import HTTPException
from sqlalchemy import and_, exists, select, true

from api.models.group import Group, UserGroup
from api.models.user import User
from api.models.gateway import Gateway
from api.models.session import Session
from api.models.audit_log import AuditLog
from api.permissions import ALL_PERMISSIONS, Permission
from api.time_utils import utc_now


def permissions_for(user):
    return set(ALL_PERMISSIONS if user.is_admin else (user.permissions or []))


def has_permission(user, permission):
    return bool(user.is_admin or permission in (getattr(user, "permissions", None) or []))


def check_permission(user, permission):
    if not has_permission(user, permission):
        raise HTTPException(403, "Permission required: " + permission)


def group_ids_query(user):
    return select(UserGroup.group_id).where(UserGroup.user_id == user.id)


def scope(column, user):
    return true() if user.is_admin else column.in_(group_ids_query(user))


def users_scope(actor):
    if actor.is_admin:
        return true()
    member = UserGroup.__table__.alias("target_membership")
    return and_(
        User.is_admin.is_(False),
        exists(
            select(1).where(
                member.c.user_id == User.id, member.c.group_id.in_(group_ids_query(actor))
            )
        ),
        ~exists(
            select(1).where(
                member.c.user_id == User.id, member.c.group_id.not_in(group_ids_query(actor))
            )
        ),
    )


async def user_group_ids(db, user_id):
    return set(
        (await db.scalars(select(UserGroup.group_id).where(UserGroup.user_id == user_id))).all()
    )


async def can_connect(db, user, gateway):
    if not user or not user.is_active or not gateway or not gateway.group_id:
        return False
    if not has_permission(user, Permission.CONNECT):
        return False
    if user.is_admin:
        return True
    return (
        await db.scalar(
            select(UserGroup.user_id).where(
                UserGroup.user_id == user.id, UserGroup.group_id == gateway.group_id
            )
        )
        is not None
    )


async def gateway_for_user(db, gateway_id, user):
    gateway = await db.scalar(
        select(Gateway).where(Gateway.id == gateway_id, scope(Gateway.group_id, user))
    )
    if gateway is None:
        raise HTTPException(404, "Gateway not found")
    return gateway


async def group_for_user(db, group_id, user):
    group = await db.scalar(select(Group).where(Group.id == group_id, scope(Group.id, user)))
    if group is None:
        raise HTTPException(404, "Group not found")
    return group


async def validate_group_ids(db, actor, identifiers):
    identifiers = set(identifiers)
    if not identifiers:
        return identifiers
    found = set(
        (
            await db.scalars(
                select(Group.id).where(Group.id.in_(identifiers), scope(Group.id, actor))
            )
        ).all()
    )
    if found != identifiers:
        raise HTTPException(404, "Group not found")
    return identifiers


async def lock_actor(db, actor):
    # Re-read privileges after the lock: a dependency may have loaded the user
    # before another administrator revoked their token or delegated permissions.
    from api.services.lifecycle import allocation_lock

    version = actor.token_version
    await allocation_lock(db)
    await db.refresh(actor)
    if not actor.is_active or actor.token_version != version:
        raise HTTPException(401, "Credentials changed; sign in again")
    if getattr(actor, "_web_session_id", None):
        from api.services.web_session_service import valid_web_session

        if not await valid_web_session(db, actor._web_session_id, actor):
            raise HTTPException(401, "Portal session no longer authorized")
    if getattr(actor, "_sso_session_id", None):
        from api.services.sso_service import valid_session

        if not await valid_session(db, actor._sso_session_id, actor):
            raise HTTPException(401, "SSO session no longer authorized")


async def terminate_user_sessions(db, user, *, reason, only_unauthorized=False, actor_id=None):
    sessions = (
        await db.scalars(
            select(Session)
            .where(Session.technician_id == user.id, Session.status.in_(("REQUESTED", "ACTIVE")))
            .with_for_update()
        )
    ).all()
    for session in sessions:
        if only_unauthorized:
            gateway = await db.get(Gateway, session.gateway_id)
            if (
                gateway
                and session.group_id == gateway.group_id
                and await can_connect(db, user, gateway)
            ):
                continue
        session.status = "FAILED"
        session.ended_at = utc_now()
        session.error_reason = reason
        terminal_event(db, session, actor_id=actor_id or user.id)
        record_event(
            db,
            "session_revoked",
            actor_id if actor_id is not None else user.id,
            group_id=session.group_id,
            session_id=session.id,
            gateway_id=session.gateway_id,
            details={"reason": reason},
        )


def record_event(
    db, event_type, actor_id, *, group_id=None, session_id=None, gateway_id=None, details=None
):
    """Security events commit or roll back together with the authorized mutation."""
    db.add(
        AuditLog(
            event_type=event_type,
            user_id=actor_id,
            group_id=group_id,
            session_id=session_id,
            gateway_id=gateway_id,
            details=details or {},
        )
    )


def record_user_event(db, event_type, actor_id, target_id, group_ids):
    for group_id in sorted(group_ids, key=str) or [None]:
        record_event(
            db, event_type, actor_id, group_id=group_id, details={"target_user_id": str(target_id)}
        )
