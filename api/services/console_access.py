"""Deployment-authorized second factor, independent of SMTP and email ownership."""

from datetime import timedelta
import hashlib
import re
import secrets
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from api.models import ConsoleAccessCode
from api.services.access_service import record_event
from api.services.lifecycle import allocation_lock
from api.time_utils import utc_now

PURPOSES = {"login", "confirm"}
INVALID_CODE = "Incorrect or expired console access code"


def audit_text(value, label, maximum):
    if not isinstance(value, str):
        raise ValueError(f"A valid {label} is required (maximum {maximum} characters)")
    value = value.strip()
    if not value or len(value) > maximum or any(not c.isprintable() for c in value):
        raise ValueError(f"A valid {label} is required (maximum {maximum} characters)")
    return value


def code_hash(code):
    # Grouping and letter case are presentation only; never store the plaintext code.
    normalized = (
        code.strip().replace("-", "").lower()
        if isinstance(code, str) and len(code) <= 64
        else ""
    )
    if not re.fullmatch(r"[0-9a-f]{24}", normalized):
        normalized = ""
    return hashlib.sha256(normalized.encode()).hexdigest()


async def local_account(db, user):
    await allocation_lock(db)
    if user is not None:
        await db.refresh(user)
    if not user or not user.is_active or user.auth_mode != "local":
        raise ValueError("Active local account required")


def audit(db, event_type, user, purpose, **details):
    record_event(
        db,
        event_type,
        None if event_type == "console_code_issued" else user.id,
        details={"target_user_id": str(user.id), "purpose": purpose, **details},
    )


async def issue(db, user, *, operator, reason, purpose="login"):
    """Return (row, plaintext code); the caller commits before showing the code once."""
    if purpose not in PURPOSES:
        raise ValueError("Console code purpose must be login or confirm")
    operator = audit_text(operator, "operator", 100)
    reason = audit_text(reason, "reason", 500)
    await local_account(db, user)
    now = utc_now()
    await db.execute(
        update(ConsoleAccessCode)
        .where(
            ConsoleAccessCode.user_id == user.id,
            ConsoleAccessCode.purpose == purpose,
            ConsoleAccessCode.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    raw = secrets.token_hex(12)
    code = "-".join(raw[offset : offset + 6] for offset in range(0, len(raw), 6))
    row = ConsoleAccessCode(
        id=uuid4(),
        user_id=user.id,
        token_version=user.token_version,
        purpose=purpose,
        code_hash=code_hash(code),
        attempts=0,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    db.add(row)
    audit(db, "console_code_issued", user, purpose, operator=operator, reason=reason)
    return row, code


async def verify(db, user, code, *, purpose="login"):
    """Consume a proof atomically with its login/confirmation; persist failed attempts."""
    if purpose not in PURPOSES:
        raise ValueError("Console code purpose must be login or confirm")
    try:
        await local_account(db, user)
    except ValueError:
        raise HTTPException(400, INVALID_CODE) from None
    row = await db.scalar(
        select(ConsoleAccessCode)
        .where(
            ConsoleAccessCode.user_id == user.id,
            ConsoleAccessCode.purpose == purpose,
            ConsoleAccessCode.consumed_at.is_(None),
        )
        .order_by(ConsoleAccessCode.created_at.desc(), ConsoleAccessCode.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utc_now()
    valid = bool(
        row
        and row.token_version == user.token_version
        and row.expires_at > now
        and row.attempts < 5
    )
    if valid:
        row.attempts += 1
        if secrets.compare_digest(row.code_hash, code_hash(code)):
            row.consumed_at = now
            user._second_factor_method = "console"
            user._second_factor_at = now
            user._email_verified = False
            audit(db, "console_code_verified", user, purpose)
            return row
        if row.attempts >= 5:
            row.consumed_at = now
    audit(db, "console_code_failed", user, purpose)
    await db.commit()  # The dependency rollback must not undo the rejected attempt.
    raise HTTPException(400, INVALID_CODE)


async def confirm(db, user, code, session):
    """Refresh this portal session's proof without replacing it or closing its VPNs."""
    row = await verify(db, user, code, purpose="confirm")
    session.reauthenticated_at = session.second_factor_at = user._second_factor_at
    session.second_factor_method = "console"
    return row
