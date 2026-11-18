"""Private coordinator protocol, run inside the API image; never an HTTP endpoint.

JSON travels over stdin/stdout pipes. It may contain configuration secrets and must
never be logged. The host coordinator is the only caller.
"""

import asyncio
import json
import sys
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import delete, select, text
from factoryvpn_common.backup_configuration import CONFIG_KEYS

from api.config import settings
from api.db.session import engine, async_session_maker
from api.models import (
    User,
    UserGroup,
    ExternalIdentity,
    WebSession,
    SSOSession,
    SSOLoginAttempt,
    Session,
    SessionRequest,
    ProvisioningJob,
    VPNCertificate,
    GatewayCredential,
    Gateway,
    Group,
    EmailChallenge,
    ConsoleAccessCode,
    TelemetrySnapshot,
    Config,
    CertificateRenewal,
)
from api.services.lifecycle import allocation_lock
from api.services import backup_access
from api.services.web_session_service import valid_web_session
from api.time_utils import utc_now

def reveal(value):
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if hasattr(value, "model_fields"):
        return {key: reveal(getattr(value, key)) for key in value.model_fields}
    if isinstance(value, dict):
        return {key: reveal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [reveal(item) for item in value]
    return value


def output(value):
    print(json.dumps(value, default=str), flush=True)


async def line():
    return await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), timeout=600)


async def authorized(db, actor, *, fresh=True):
    if actor.get("console"):
        # Only the host CLI can invoke this module, never use this claim in web jobs.
        return
    user = await db.get(User, UUID(actor["user_id"]))
    row = await valid_web_session(db, UUID(actor["web_session_id"]), user)
    if (
        not user
        or not user.is_admin
        or not row
        or user.token_version != actor["version"]
        or (fresh and not backup_access.fresh_authentication(row, user))
    ):
        raise ValueError("authorization_expired")


async def snapshot(actor):
    # A separate exported read snapshot survives releasing the short lifecycle lock.
    # Copy PKI while the lock is held, then let normal traffic resume during pg_dump.
    async with async_session_maker() as guard:
        await allocation_lock(guard, maintenance=True)
        await authorized(guard, actor)
        async with engine.connect() as snap:
            await snap.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            snapshot_id = await snap.scalar(text("SELECT pg_export_snapshot()"))
            output(
                {
                    "snapshot": snapshot_id,
                    "configuration": {key: reveal(getattr(settings, key)) for key in CONFIG_KEYS},
                }
            )
            if (await line()).strip() != "release":
                raise ValueError("protocol_error")
            await guard.rollback()
            output({"released": True})
            if (await line()).strip() != "done":
                raise ValueError("protocol_error")
            await snap.rollback()


async def security_state(db):
    users = {}
    groups = {}
    for member in (await db.scalars(select(UserGroup))).all():
        groups.setdefault(str(member.user_id), []).append(str(member.group_id))
    for user in (await db.scalars(select(User))).all():
        users[str(user.id)] = {
            "is_active": user.is_active,
            "is_admin": user.is_admin,
            "auth_mode": user.auth_mode,
            "password_hash": user.password_hash,
            "permissions": user.permissions,
            "groups": groups.get(str(user.id), []),
            "email": user.email,
            "email_verified_at": user.email_verified_at,
            "email_two_step": user.email_two_step,
        }
    identities = [
        {
            key: getattr(row, key)
            for key in ("id", "user_id", "provider_id", "issuer", "subject", "created_at")
        }
        for row in (await db.scalars(select(ExternalIdentity))).all()
    ]
    return {
        "users": users,
        "identities": identities,
        "gateways": {
            str(row.id): str(row.group_id) if row.group_id else None
            for row in (await db.scalars(select(Gateway))).all()
        },
        "certificates": [
            row.serial
            for row in (
                await db.scalars(select(VPNCertificate).where(VPNCertificate.state == "active"))
            ).all()
        ],
        "credentials": [
            row.secret_hash
            for row in (
                await db.scalars(
                    select(GatewayCredential).where(
                        GatewayCredential.active.is_(True), GatewayCredential.revoked_at.is_(None)
                    )
                )
            ).all()
        ],
    }


async def sanitize(db, current, *, actor_id=None, recovery=None):
    """Restore historical data without restoring historical authority or live work."""
    now = utc_now()
    await db.execute(delete(WebSession))
    await db.execute(delete(SSOSession))
    await db.execute(delete(SSOLoginAttempt))
    await db.execute(delete(EmailChallenge))
    await db.execute(delete(ConsoleAccessCode))
    await db.execute(delete(SessionRequest))
    await db.execute(delete(TelemetrySnapshot))
    await db.execute(delete(Config).where(Config.key.in_(("provisioning_worker",))))
    users = (await db.scalars(select(User))).all()
    known = current.get("users", {})
    disabled = set()
    # Free unique email values before applying the current identity bindings.
    for user in users:
        user.email = None
    await db.flush()
    for user in users:
        previous = known.get(str(user.id))
        if previous:
            user.is_active = bool(user.is_active and previous["is_active"])
            user.is_admin = bool(user.is_admin and previous["is_admin"])
            user.permissions = sorted(set(user.permissions) & set(previous["permissions"]))
            user.auth_mode, user.password_hash = previous["auth_mode"], previous["password_hash"]
            user.email = previous["email"]
            verified = previous["email_verified_at"]
            if isinstance(verified, str):
                from datetime import datetime

                verified = datetime.fromisoformat(verified)
            user.email_verified_at = verified
            user.email_two_step = previous["email_two_step"]
            await db.execute(
                delete(UserGroup).where(
                    UserGroup.user_id == user.id,
                    UserGroup.group_id.not_in([UUID(g) for g in previous["groups"]]),
                )
            )
        else:
            user.is_active, user.is_admin, user.permissions = False, False, []
            user.email_verified_at, user.email_two_step = None, True
            await db.execute(delete(UserGroup).where(UserGroup.user_id == user.id))
        user.token_version += 1
        user.email_last_sent_at = user.email_window_at = user.email_locked_until = None
        user.email_send_count = user.email_failures = 0
        if not user.is_active:
            disabled.add(user.id)
    await db.execute(delete(ExternalIdentity))
    user_ids = {user.id for user in users}
    for identity in current.get("identities", []):
        if UUID(str(identity["user_id"])) in user_ids:
            from datetime import datetime

            values = dict(identity)
            for key in ("id", "user_id"):
                values[key] = UUID(str(values[key]))
            if isinstance(values["created_at"], str):
                values["created_at"] = datetime.fromisoformat(values["created_at"])
            db.add(ExternalIdentity(**values))
    if recovery:
        from api.services.auth_service import hash_password

        if any(user.username == recovery["username"] for user in users):
            raise ValueError("recovery_username_exists")
        db.add(
            User(
                username=recovery["username"],
                password_hash=hash_password(recovery["password"]),
                is_active=True,
                is_admin=True,
                permissions=[],
                email=recovery.get("email"),
            )
        )
    elif actor_id and not any(str(u.id) == actor_id and u.is_admin and u.is_active for u in users):
        raise ValueError("recovery_administrator_missing")
    moved = set()
    restored_groups = set((await db.scalars(select(Group.id))).all())
    for gateway in (await db.scalars(select(Gateway))).all():
        current_group = current.get("gateways", {}).get(str(gateway.id))
        current_group = UUID(current_group) if current_group else None
        if current_group != gateway.group_id:
            moved.add(gateway.id)
        # Restoring inventory must never give the previous group access again.
        # Deleted gateways or groups absent from the archive stay global-admin-only.
        gateway.group_id = current_group if current_group in restored_groups else None
        gateway.last_seen, gateway.status = None, "OFFLINE"
    for cert in (await db.scalars(select(VPNCertificate))).all():
        if (
            cert.state != "active"
            or cert.serial not in current.get("certificates", [])
            or cert.peer_id in moved
            or cert.peer_id in disabled
        ):
            cert.state, cert.revoked_at, cert.crl_applied_at = (
                "revoked",
                cert.revoked_at or now,
                None,
            )
    for credential in (await db.scalars(select(GatewayCredential))).all():
        if (
            not credential.active
            or credential.revoked_at
            or credential.secret_hash not in current.get("credentials", [])
            or credential.gateway_id in moved
        ):
            credential.active, credential.revoked_at = False, credential.revoked_at or now
    for job in (await db.scalars(select(ProvisioningJob))).all():
        job.artifact_encrypted = None
        if job.state in ("queued", "ready"):
            job.state, job.error_code = "cancelled", "installation_restored"
    for renewal in (await db.scalars(select(CertificateRenewal))).all():
        candidate = (
            await db.get(VPNCertificate, renewal.certificate_id) if renewal.certificate_id else None
        )
        if renewal.state in ("active", "confirmed") and candidate and candidate.state == "active":
            # The device can replay confirmation using its durable new private key.
            continue
        if renewal.state in ("challenge", "queued", "ready", "active"):
            renewal.state, renewal.error_code, renewal.maintenance_until = (
                "cancelled",
                "installation_restored",
                None,
            )
    from api.models.enrollment import GatewayEnrollment

    # Il restore non riabilita segreti iniziali o approvazioni di fabbrica storiche.
    for enrollment in (await db.scalars(select(GatewayEnrollment))).all():
        enrollment.secret_hash = None
        if enrollment.completed_at is None:
            enrollment.approved_at = None
            enrollment.blocked_at, enrollment.blocked_reason = now, "installation_restored"
    await db.execute(delete(Config).where(Config.key.like("enrollment-rate:%")))
    for session in (await db.scalars(select(Session))).all():
        if session.status in ("REQUESTED", "ACTIVE"):
            session.status, session.ended_at = "CLOSED", now
            session.error_reason = "Installation restored"
        # Coordinator must have destroyed the old OpenVPN network namespace first.
        session.cleaned_at = now
        session.gateway_ready_at, session.network_ready_at, session.worker_seen_at = (
            None,
            None,
            None,
        )
    from api.services.access_service import record_event

    record_event(db, "installation_restored", UUID(actor_id) if actor_id else None)
    await db.commit()


async def main():
    request = json.loads(await line())
    operation = sys.argv[1]
    if operation == "snapshot":
        return await snapshot(request["actor"])
    async with async_session_maker() as db:
        if operation == "authorize":
            await allocation_lock(db, maintenance=True)
            await authorized(db, request["actor"])
            output({"authorized": True})
        elif operation == "security":
            await allocation_lock(db, maintenance=True)
            await authorized(db, request["actor"])
            output(await security_state(db))
        elif operation == "sanitize":
            await sanitize(
                db,
                request.get("security", {}),
                actor_id=request.get("actor_id"),
                recovery=request.get("recovery"),
            )
            output({"sanitized": True})
        elif operation == "close":
            await allocation_lock(db, maintenance=True)
            await authorized(db, request["actor"])
            for session in (
                await db.scalars(
                    select(Session)
                    .where(Session.status.in_(("REQUESTED", "ACTIVE")))
                    .with_for_update()
                )
            ).all():
                session.status, session.ended_at = "CLOSED", utc_now()
                session.error_reason = "Installation maintenance"
            await db.commit()
            output({"closed": True})
        elif operation == "drained":
            occupied = await db.scalar(
                select(Session.id).where(Session.cleaned_at.is_(None)).limit(1)
            )
            output({"drained": occupied is None})
        elif operation == "check":
            from api.db.session import init_db

            await init_db()
            output({"ready": True})
        elif operation == "validate_configuration":
            from api.config import Settings

            configuration = request["configuration"]
            if set(configuration) != set(CONFIG_KEYS):
                raise ValueError("invalid_configuration")
            for field in (
                "PORTAL_PUBLIC_URL",
                "SSO_PUBLIC_URL",
                "OPENVPN_SERVER_HOST",
                "OPENVPN_SERVER_PORT",
            ):
                if configuration[field] != getattr(settings, field):
                    output({"valid": False, "code": "incompatible_origin"})
                    return
            values = {key: reveal(getattr(settings, key)) for key in settings.model_fields}
            Settings(_env_file=None, **{**values, **configuration})
            output({"valid": True})
        else:
            raise ValueError("unknown_operation")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # Detailed exception text may contain secrets; the coordinator records a code.
        sys.exit(1)
