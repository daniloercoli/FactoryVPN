"""Read public certificates and recovery metadata; never expose paths or secrets."""

from datetime import datetime
import math
import os
from pathlib import Path
import sqlite3
import stat

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes

from api.schemas.server_status import (
    BackupCopySummary,
    BackupOperationSummary,
    BackupSummary,
    BackupTestSummary,
    CertificateStatus,
)

CERTIFICATE_MAX_BYTES = 64 * 1024


def certificate_status(name, path, now):
    base = dict(name=name, observed_at=now, valid_for_seconds=30)
    if not path:
        return CertificateStatus(**base, state="not_configured")
    try:
        # A deployment mistake pointing at a FIFO must not block an API thread.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Regular public certificate required")
            data = stream.read(CERTIFICATE_MAX_BYTES + 1)
        if len(data) > CERTIFICATE_MAX_BYTES:
            raise ValueError("Certificate too large")
        cert = x509.load_pem_x509_certificate(data)
        start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
        remaining = (end - now).total_seconds()
        state = (
            "not_yet_valid"
            if now < start
            else "expired" if remaining <= 0 else "expiring" if remaining <= 30 * 86400 else "valid"
        )
        boundary = (
            (start - now).total_seconds()
            if state == "not_yet_valid"
            else remaining - 30 * 86400 if state == "valid" else remaining
        )
        if boundary > 0:
            base["valid_for_seconds"] = min(30, boundary)
        return CertificateStatus(
            **base,
            state=state,
            valid_from=start,
            expires_at=end,
            days_remaining=math.ceil(remaining / 86400),
            fingerprint_sha256=cert.fingerprint(hashes.SHA256()).hex(),
        )
    except FileNotFoundError:
        return CertificateStatus(**base, state="missing")
    except (OSError, ValueError, UnsupportedAlgorithm):
        return CertificateStatus(**base, state="unreadable")


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def operation(row):
    if row is None:
        return None
    return BackupOperationSummary(
        kind=row["kind"],
        state=row["state"],
        created_at=timestamp(row["created_at"]),
        finished_at=timestamp(row["finished_at"]) if row["finished_at"] else None,
    )


def backup_summary(control_dir, archive_dir, now):
    base = dict(observed_at=now, valid_for_seconds=30)
    if not control_dir or not archive_dir:
        return BackupSummary(**base, state="not_configured")
    try:
        path = Path(control_dir).absolute() / "catalog.sqlite"
        # mode=ro cannot create a missing catalog. A short lock timeout bounds the
        # read during coordinator updates; no manifest/actor/secret is selected.
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            heartbeat = db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
            count = db.execute("SELECT count(*) FROM backups WHERE state='verified'").fetchone()[0]
            latest = db.execute(
                "SELECT id,created_at,tested_at FROM backups WHERE state='verified' "
                "ORDER BY created_at DESC,id DESC LIMIT 1"
            ).fetchone()
            tested = db.execute(
                "SELECT id,tested_at FROM backups WHERE tested_at IS NOT NULL "
                "ORDER BY tested_at DESC,id DESC LIMIT 1"
            ).fetchone()
            test_job = db.execute(
                "SELECT backup_id,finished_at,state FROM jobs WHERE kind='verify' "
                "AND state IN ('succeeded','failed') AND finished_at IS NOT NULL "
                "ORDER BY finished_at DESC,id DESC LIMIT 1"
            ).fetchone()
            fields = "kind,state,created_at,finished_at"
            last = db.execute(
                f"SELECT {fields} FROM jobs ORDER BY created_at DESC,id DESC LIMIT 1"
            ).fetchone()
            active = db.execute(
                f"SELECT {fields} FROM jobs WHERE state IN ('queued','running','uploading') "
                "ORDER BY created_at DESC,id DESC LIMIT 1"
            ).fetchone()
        finally:
            db.close()
        seen = timestamp(heartbeat[0]) if heartbeat else None
        age = (now - seen).total_seconds() if seen else None
        coordinator = "unknown" if age is None or age < 0 else "online" if age < 30 else "offline"
        last_test = (
            BackupTestSummary(
                backup_id=test_job["backup_id"],
                finished_at=timestamp(test_job["finished_at"]),
                state=test_job["state"],
            )
            if test_job
            else None
        )
        if tested and (last_test is None or timestamp(tested["tested_at"]) > last_test.finished_at):
            last_test = BackupTestSummary(
                backup_id=tested["id"],
                finished_at=timestamp(tested["tested_at"]),
                state="succeeded",
            )
        return BackupSummary(
            **{**base, "valid_for_seconds": min(30, 30 - age) if coordinator == "online" else 30},
            state="available",
            coordinator_state=coordinator,
            coordinator_observed_at=seen,
            copies=count,
            latest_backup=(
                BackupCopySummary(
                    id=latest["id"],
                    created_at=timestamp(latest["created_at"]),
                    tested_at=timestamp(latest["tested_at"]) if latest["tested_at"] else None,
                )
                if latest
                else None
            ),
            last_test=last_test,
            last_operation=operation(last),
            active_operation=operation(active),
        )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return BackupSummary(**base, state="unavailable")


def collect(settings, now):
    return dict(
        certificates=[
            certificate_status(name, path, now)
            for name, path in (
                ("https", settings.STATUS_HTTPS_CERTIFICATE_FILE),
                ("openvpn", settings.STATUS_VPN_CERTIFICATE_FILE),
                ("openvpn_ca", settings.STATUS_VPN_CA_FILE),
            )
        ],
        backup=backup_summary(settings.BACKUP_CONTROL_DIR, settings.BACKUP_ARCHIVE_DIR, now),
    )
