"""Isolated CA holder: typed database jobs, no public listener and no shell execution."""

import asyncio
from contextlib import suppress
from datetime import timedelta
import io
import logging
import os
from pathlib import Path
import secrets
import zipfile
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy import select, exists, or_, and_
from api.config import settings
from api.db.session import async_session_maker
import api.models  # noqa: F401
from api.models.gateway import Gateway
from api.models.user import User
from api.models.config import Config
from api.models.provisioning import ProvisioningJob, VPNCertificate, GatewayCredential
from api.services.credentials import encrypt, digest
from api.services.lifecycle import allocation_lock
from api.services.provisioning import expire_jobs, audit, mark_revoked
from api.time_utils import utc_now
from factoryvpn_common.pki import issue_profile, publish_crl
from factoryvpn_common.runtime_info import local_runtime
import json

logger = logging.getLogger(__name__)


async def health(db, value):
    row = await db.get(Config, "provisioning_worker")
    if row is None:
        db.add(Config(key="provisioning_worker", value=value, updated_at=utc_now()))
    else:
        row.value, row.updated_at = value, utc_now()
    runtime = await db.get(Config, "provisioning_runtime")
    if runtime is None:
        db.add(
            Config(
                key="provisioning_runtime", value=json.dumps(local_runtime()), updated_at=utc_now()
            )
        )
    else:
        runtime.value, runtime.updated_at = json.dumps(local_runtime()), utc_now()


def gateway_bundle(job, profile, api_key, sms_key):
    p = job.parameters
    if not settings.PORTAL_PUBLIC_URL:
        raise ValueError("Public HTTPS URL is required")
    env = f"""SERVER_URL={settings.PORTAL_PUBLIC_URL}
GATEWAY_ID={job.peer_id}
GATEWAY_ACTIVATION_MODE={p['activation_mode']}
GATEWAY_PHONE_NUMBER={p['phone_number'] or ''}
GATEWAY_API_KEY={api_key}
GATEWAY_SMS_KEY={sms_key}
MODEM_PATH=/dev/ttyUSB0
MAC_ADDRESS={p['mac_address'] or ''}
GATEWAY_WIFI_INTERFACE=
GATEWAY_HARDWARE_MODEL=
GATEWAY_HARDWARE_REVISION=
LAN_INTERFACE={p['lan_interface']}
LAN_ADDRESS={p['lan_address']}
LAN_CIDR={p['lan_cidr']}
VPN_INTERFACE=tun0
OPENVPN_CONFIG=/etc/factoryvpn/gateway.ovpn
GATEWAY_POLL_INTERVAL=5
GATEWAY_WIFI_CHECK_INTERVAL=30
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in {
            "gateway.env": env.encode(),
            "gateway.ovpn": profile,
            "LEGGIMI.txt": (
                "FactoryVPN — installazione gateway\n\n"
                "Questo archivio contiene chiavi private: conservarlo in modo protetto.\n"
                "Dopo il download, attivare il pacchetto nel portale.\n"
                "Installare gateway.env e gateway.ovpn in /etc/factoryvpn/ con permessi 0600.\n"
                "Configurare la LAN nel sistema operativo e installare l'agente FactoryVPN.\n"
                "Avviare o riavviare factoryvpn-agent.service e controllare la registrazione nel portale.\n"
                "L'agente richiede HTTPS attendibile e connettività indipendente dal tunnel.\n"
                "La registrazione non verifica il tunnel: completare una sessione di prova.\n"
            ).encode(),
        }.items():
            info = zipfile.ZipInfo(filename)
            info.external_attr = 0o100600 << 16
            archive.writestr(info, contents)
    return buffer.getvalue()


async def reconcile_revocations(db, root):
    # Deletion/disable cannot leave an untracked certificate usable in a future session.
    invalid = select(VPNCertificate).where(
        VPNCertificate.state == "active",
        or_(
            and_(
                VPNCertificate.kind == "gateway",
                ~exists(select(Gateway.id).where(Gateway.id == VPNCertificate.peer_id)),
            ),
            and_(
                VPNCertificate.kind == "technician",
                ~exists(
                    select(User.id).where(
                        User.id == VPNCertificate.peer_id, User.is_active.is_(True)
                    )
                ),
            ),
        ),
    )
    for cert in (await db.scalars(invalid)).all():
        mark_revoked(cert)
    await db.flush()
    revoked = (
        await db.scalars(select(VPNCertificate).where(VPNCertificate.state == "revoked"))
    ).all()
    path = root / "server/crl.pem"
    refresh = not path.exists()
    if path.exists():
        refresh = x509.load_pem_x509_crl(path.read_bytes()).next_update_utc < utc_now() + timedelta(
            days=1
        )
    if refresh or any(cert.crl_applied_at is None for cert in revoked):
        publish_crl(root, revoked)
        for cert in revoked:
            cert.crl_applied_at = cert.crl_applied_at or utc_now()


async def cycle(factory, root):
    job_id = None
    try:
        async with factory() as db:
            await allocation_lock(db)
            await expire_jobs(db)
            await reconcile_revocations(db, root)
            job = await db.scalar(
                select(ProvisioningJob)
                .where(ProvisioningJob.state == "queued")
                .order_by(ProvisioningJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job:
                job_id = job.id
                cert, data = issue_profile(
                    root,
                    job.kind,
                    job.peer_id,
                    settings.OPENVPN_SERVER_HOST,
                    settings.OPENVPN_SERVER_PORT,
                )
                row = VPNCertificate(
                    kind=job.kind,
                    peer_id=job.peer_id,
                    serial=str(cert.serial_number),
                    certificate_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
                    not_before=cert.not_valid_before_utc,
                    not_after=cert.not_valid_after_utc,
                    state="candidate",
                )
                db.add(row)
                await db.flush()
                job.certificate_id = row.id
                if job.kind == "gateway":
                    key, sms_key = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
                    credential = GatewayCredential(
                        gateway_id=job.peer_id,
                        secret_hash=digest(key),
                        sms_secret_encrypted=encrypt(sms_key.encode()),
                        active=False,
                    )
                    db.add(credential)
                    await db.flush()
                    job.credential_id = credential.id
                    data = gateway_bundle(job, data, key, sms_key)
                job.artifact_encrypted, job.state = encrypt(data), "ready"
                audit(db, job, "provisioning_ready")
            await health(db, "ok")
            await db.commit()
    except Exception as exc:
        # Exception text may contain filesystem/environment details; emit only its type.
        logger.error("Provisioning cycle failed (%s)", type(exc).__name__)
        async with factory() as db:
            await allocation_lock(db)
            await health(db, "error")
            if job_id:
                job = await db.get(ProvisioningJob, job_id)
                if job and job.state == "queued":
                    job.state, job.error_code = "failed", "issuer_unavailable"
                    audit(db, job, "provisioning_failed")
            await db.commit()
        raise


async def main():
    root = Path(os.environ.get("FACTORYVPN_PKI_ROOT", "/pki"))
    while True:
        with suppress(Exception):
            await cycle(async_session_maker, root)
        await asyncio.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
