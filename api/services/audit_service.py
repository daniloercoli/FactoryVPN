"""Append historical identifiers without coupling audit retention to entity deletion."""

import logging
from sqlalchemy.ext.asyncio import AsyncSession
from api.models.audit_log import AuditLog
from api.time_utils import utc_now

logger = logging.getLogger(__name__)


async def log_event(
    db: AsyncSession,
    event_type: str,
    user_id=None,
    session_id=None,
    gateway_id=None,
    ip_address=None,
    details=None,
) -> bool:
    try:
        # A bad audit payload cannot poison the caller's transaction/session.
        async with db.begin_nested():
            db.add(
                AuditLog(
                    event_type=event_type,
                    timestamp=utc_now(),
                    user_id=user_id,
                    session_id=session_id,
                    gateway_id=gateway_id,
                    ip_address=ip_address,
                    details=details or {},
                )
            )
            await db.flush()
    except Exception:
        logger.exception("Failed to append audit event %s", event_type)
        return False
    await db.commit()
    return True
