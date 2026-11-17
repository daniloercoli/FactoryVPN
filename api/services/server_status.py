"""Global status and a bounded, deployment-owned SMTP probe. Never return exceptions."""

import asyncio
import math
from datetime import timedelta
from email.message import EmailMessage
import smtplib
import ssl
from fastapi import HTTPException
from sqlalchemy import text, update
from api.config import settings
from api.models.config import Config
from api.models.server_status import SmtpCheck
from api.models.telemetry import TelemetrySnapshot
from api.models.audit_log import AuditLog
from api.services import mail_service, server_readiness
from api.services.access_service import lock_actor
from api.schemas.server_status import SmtpResult
from api.time_utils import utc_now
from factoryvpn_common.runtime_info import RuntimeInfo, local_runtime, safe_label

_active_probe = None
PROBE_TIMEOUT = 15


def smtp_configured():
    try:
        return bool(settings.SMTP_HOST and mail_service.email_address(settings.SMTP_FROM))
    except ValueError:
        return False


def result(row):
    value = {name: getattr(row, name) for name in SmtpResult.model_fields}
    if value["code"] == "running" and row.started_at < utc_now() - timedelta(seconds=60):
        value["code"] = "timeout"
    return value


def retry_after(row):
    if row is None:
        return 0
    limit = row.started_at + timedelta(seconds=60)
    if row.window_count >= 6:
        limit = max(limit, row.window_at + timedelta(hours=1))
    return max(0, math.ceil((limit - utc_now()).total_seconds()))


def runtime(payload):
    try:
        return RuntimeInfo.model_validate(payload or {}).model_dump()
    except ValueError:
        return RuntimeInfo().model_dump()


async def status(db, actor):
    now = utc_now()
    version = await db.scalar(text("SELECT current_setting('server_version')"))
    worker = await db.get(TelemetrySnapshot, "server:worker")
    worker_info = runtime(worker.payload.get("runtime") if worker else None)
    worker_fresh = worker and now - min(worker.observed_at, worker.received_at) < timedelta(
        seconds=90
    )
    services = worker.payload["services"] if worker_fresh else {}
    provisioning = await db.get(Config, "provisioning_worker")
    provisioning_info = await db.get(Config, "provisioning_runtime")
    try:
        provision_runtime = (
            RuntimeInfo.model_validate_json(provisioning_info.value).model_dump()
            if provisioning_info
            else {}
        )
    except ValueError:
        provision_runtime = {}
    provision_state = (
        provisioning.value
        if provisioning
        and provisioning.updated_at
        and now - provisioning.updated_at < timedelta(seconds=30)
        else "unknown"
    )
    local = local_runtime()
    components = [
        dict(
            name="api", version=local["version"], build=local["build"], observed_at=now, state="ok"
        ),
        dict(name="python_api", version=local["python"], observed_at=now, state="ok"),
        dict(name="postgresql", version=safe_label(version), observed_at=now, state="ok"),
        dict(
            name="worker",
            version=worker_info["version"],
            build=worker_info["build"],
            observed_at=worker.observed_at if worker else None,
            state=services.get("worker", "unknown"),
        ),
        dict(
            name="python_worker",
            version=worker_info["python"],
            observed_at=worker.observed_at if worker else None,
            state=services.get("worker", "unknown"),
        ),
        dict(
            name="openvpn",
            version=worker_info["openvpn"],
            observed_at=worker.observed_at if worker else None,
            state=services.get("openvpn", "unknown"),
        ),
        dict(
            name="provisioning",
            version=provision_runtime.get("version"),
            build=provision_runtime.get("build"),
            observed_at=provisioning_info.updated_at if provisioning_info else None,
            state=provision_state if provision_state in {"ok", "error"} else "unknown",
        ),
    ]
    for component in components:
        if component["name"] in {"worker", "python_worker", "openvpn"}:
            remaining = (
                90 - (now - min(worker.observed_at, worker.received_at)).total_seconds()
                if worker
                else 0
            )
        elif component["name"] == "provisioning":
            remaining = (
                30 - (now - provisioning.updated_at).total_seconds()
                if provisioning and provisioning.updated_at
                else 0
            )
        else:
            remaining = 30
        component["valid_for_seconds"] = max(0, min(90, remaining))
    row = await db.get(SmtpCheck, 1)
    readiness = await asyncio.to_thread(server_readiness.collect, settings, now)
    return dict(
        **readiness,
        observed_at=now,
        components=components,
        api_runtime=local,
        smtp=dict(
            configured=smtp_configured(),
            host=settings.SMTP_HOST or None,
            port=settings.SMTP_PORT,
            security=settings.SMTP_SECURITY,
            sender=settings.SMTP_FROM or None,
            authentication=bool(settings.SMTP_USERNAME),
            custom_ca=bool(settings.SMTP_CA_FILE),
            test_recipient=actor.email if actor.email_verified_at else None,
            last_check=result(row) if row else None,
            retry_after_seconds=retry_after(row),
        ),
    )


def probe(recipient=None):
    if not smtp_configured():
        return "not_configured"
    try:
        with mail_service.transport(timeout=3) as smtp:
            if smtp.noop()[0] != 250:
                return "connection_failed"
            if recipient:
                message = EmailMessage()
                message["From"] = mail_service.email_address(settings.SMTP_FROM)
                message["To"] = mail_service.email_address(recipient)
                message["Subject"] = f"{settings.APP_NAME} — verifica SMTP"
                message.set_content(
                    f"Questa email di prova è stata richiesta dalla pagina Stato server di {settings.APP_NAME}.\nLa ricezione conferma la consegna a questa casella. Non contiene codici di accesso.\n"
                )
                if smtp.send_message(message):
                    return "recipient_rejected"
                return "accepted"
            return "connected"
    except (ssl.SSLError, smtplib.SMTPNotSupportedError):
        return "tls_failed"
    except smtplib.SMTPAuthenticationError:
        return "authentication_failed"
    except smtplib.SMTPRecipientsRefused:
        return "recipient_rejected"
    except TimeoutError:
        return "timeout"
    except Exception:
        return "connection_failed"


async def check(db, actor, body):
    global _active_probe
    await lock_actor(db, actor)
    if not actor.is_admin:
        raise HTTPException(403, "Global administrator required")
    row = await db.get(SmtpCheck, 1, populate_existing=True)
    if row and row.request_id == body.request_id:
        if row.mode != body.mode:
            raise HTTPException(409, "Check identifier already used")
        return result(row)
    if _active_probe is not None and not _active_probe.done():
        raise HTTPException(429, "SMTP check in progress", headers={"Retry-After": "60"})
    delay = retry_after(row)
    if delay:
        raise HTTPException(
            429, "Wait before repeating the SMTP check", headers={"Retry-After": str(delay)}
        )
    recipient = None
    if body.mode == "send":
        if not actor.email_verified_at:
            raise HTTPException(409, "A verified account email is required")
        try:
            recipient = mail_service.email_address(actor.email)
        except ValueError:
            raise HTTPException(409, "A verified account email is required") from None
    now = utc_now()
    if row is None:
        row = SmtpCheck(id=1, window_at=now, window_count=0)
        db.add(row)
    if row.window_at <= now - timedelta(hours=1):
        row.window_at, row.window_count = now, 0
    row.window_count += 1
    row.request_id, row.actor_id, row.mode = body.request_id, actor.id, body.mode
    row.started_at, row.finished_at, row.code = now, None, "running"
    db.add(
        AuditLog(
            event_type="smtp_check_requested",
            user_id=actor.id,
            details={"mode": body.mode, "request_id": str(body.request_id)},
        )
    )
    await db.commit()  # No SMTP operation holds the VPN allocation lock.
    _active_probe = asyncio.create_task(asyncio.to_thread(probe, recipient))
    _active_probe.add_done_callback(lambda task: None if task.cancelled() else task.exception())
    try:
        code = await asyncio.wait_for(asyncio.shield(_active_probe), timeout=PROBE_TIMEOUT)
    except TimeoutError:
        code = "timeout"  # An unconfirmed send is not reported as definitely undelivered.
    finished = utc_now()
    # Another process may have accepted a newer request while this process was
    # suspended. Compare at write time; never overwrite or return its result.
    await db.execute(
        update(SmtpCheck)
        .where(SmtpCheck.id == 1, SmtpCheck.request_id == body.request_id)
        .values(code=code, finished_at=finished)
        .execution_options(synchronize_session=False)
    )
    db.add(
        AuditLog(
            event_type="smtp_check_finished",
            user_id=actor.id,
            details={"mode": body.mode, "code": code, "request_id": str(body.request_id)},
        )
    )
    await db.commit()
    await lock_actor(db, actor)
    if not actor.is_admin:
        raise HTTPException(403, "Global administrator required")
    return dict(
        request_id=body.request_id, mode=body.mode, code=code, started_at=now, finished_at=finished
    )
