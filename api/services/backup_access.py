"""Global recovery authorization and maintenance barrier shared with the lifecycle."""

from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from fastapi import HTTPException

from api.config import settings
from api.services.access_service import lock_actor
from api.services.web_session_service import valid_web_session
from api.time_utils import utc_now
from factoryvpn_common.backup_catalog import Catalog

cleanup_request = ContextVar("factoryvpn_cleanup_request", default=False)


def catalog():
    if not settings.BACKUP_CONTROL_DIR or not settings.BACKUP_ARCHIVE_DIR:
        raise HTTPException(503, "Backup service is not configured")
    result = Catalog(settings.BACKUP_CONTROL_DIR)
    if not result.path.is_file():
        raise HTTPException(503, "Backup coordinator requires initialization")
    return result


def maintenance_active():
    if not settings.BACKUP_CONTROL_DIR:
        return False
    try:
        # A durable atomic marker also works while SQLite is busy with a job update.
        return (Path(settings.BACKUP_CONTROL_DIR) / "maintenance").exists()
    except OSError:
        return True  # Lost recovery state cannot reopen VPN access.


def fresh_authentication(row, user):
    cutoff = utc_now() - timedelta(minutes=5)
    return bool(
        row.reauthenticated_at
        and row.reauthenticated_at >= cutoff
        and (user.auth_mode == "sso" or (row.second_factor_at and row.second_factor_at >= cutoff))
    )


async def authorize(db, user, *, fresh=False):
    await lock_actor(db, user)
    if not user.is_admin:
        raise HTTPException(403, "Global administrator required")
    web_id = getattr(user, "_web_session_id", None)
    row = await valid_web_session(db, web_id, user) if web_id else None
    if not row:
        raise HTTPException(401, "A portal session is required")
    if fresh and not fresh_authentication(row, user):
        raise HTTPException(428, "Backup operations require recent authentication")
    return row


def actor_claim(user, row):
    return {"user_id": str(user.id), "version": user.token_version, "web_session_id": str(row.id)}


def archive_path(backup_id):
    from factoryvpn_common.backup_catalog import identifier

    path = Path(settings.BACKUP_ARCHIVE_DIR) / (identifier(backup_id) + ".fvbackup")
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "Backup unavailable")
    return path
