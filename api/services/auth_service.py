"""
Authentication service
"""

from datetime import timedelta
from uuid import uuid4
from api.time_utils import utc_now
from jose import JWTError, jwt
import argon2
from argon2 import PasswordHasher
from api.config import settings

ph = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    type=argon2.Type.ID,
)


def hash_password(password: str) -> str:
    """Hash a password using Argon2"""
    return ph.hash(password)


def verify_password(password: str, hash: str) -> bool:
    """Verify a password against a hash"""
    try:
        ph.verify(hash, password)
        return True
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    """Create a JWT access token"""
    to_encode = data.copy()
    expire = utc_now() + (expires_delta or timedelta(minutes=settings.JWT_EXPIRE_MINUTES))
    # Separate logins must not share private provisioning deliveries, even when
    # their second-resolution timestamps and all other identity claims match.
    to_encode.update({"exp": expire, "iat": utc_now(), "jti": str(uuid4())})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    """Verify a JWT token and return payload"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def issue_access_token(user, *, sso_session=None, email_verified=False):
    data = {"sub": str(user.id), "ver": user.token_version}
    if sso_session is not None:
        data["sid"] = str(sso_session.id)
        lifetime = min(
            timedelta(minutes=settings.JWT_EXPIRE_MINUTES), sso_session.expires_at - utc_now()
        )
        return create_access_token(data, expires_delta=lifetime)
    method = getattr(user, "_second_factor_method", None)
    if email_verified or getattr(user, "_email_verified", False):
        method = "email"
    data["email_verified"] = method == "email"
    if method in {"email", "console"}:
        data["second_factor_method"] = method
        data["second_factor_at"] = getattr(user, "_second_factor_at", utc_now()).timestamp()
    return create_access_token(data)


async def authenticate_local(db, username, password):
    """Authenticate and lock the account; caller commits the resulting login session."""
    from fastapi import HTTPException
    from sqlalchemy import select
    from api.models.user import User
    from api.services.access_service import lock_actor

    user = await db.scalar(select(User).where(User.username == username))
    if (
        not user
        or not user.is_active
        or user.auth_mode != "local"
        or not verify_password(password, user.password_hash)
    ):
        raise HTTPException(401, "Invalid credentials")
    await lock_actor(db, user)
    if user.auth_mode != "local":
        raise HTTPException(401, "Invalid credentials")
    return user


async def change_local_password(db, user, current_password, new_password):
    from fastapi import HTTPException
    from api.services.access_service import (
        lock_actor,
        terminate_user_sessions,
        record_user_event,
        user_group_ids,
    )

    await lock_actor(db, user)
    from api.services.email_security import require_recent

    require_recent(user)
    if user.auth_mode != "local":
        raise HTTPException(409, "Password changes are managed by the SSO provider")
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    await terminate_user_sessions(db, user, reason="Password changed")
    record_user_event(db, "password_changed", user.id, user.id, await user_group_ids(db, user.id))
