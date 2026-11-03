"""Configured certificates and recovery summaries never trigger operations."""

from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from api.services.server_readiness import backup_summary, certificate_status, CERTIFICATE_MAX_BYTES
from factoryvpn_common.backup_catalog import Catalog

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def write_certificate(path, start=NOW - timedelta(days=1), end=NOW + timedelta(days=90)):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "private-subject-not-returned")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert


@pytest.mark.parametrize(
    "start,end,state",
    [
        (-1, 31, "valid"),
        (-1, 30, "expiring"),
        (-1, 1, "expiring"),
        (-2, 0, "expired"),
        (-2, -1, "expired"),
        (1, 90, "not_yet_valid"),
    ],
)
def test_certificate_dates_and_safe_fingerprint(tmp_path, start, end, state):
    path = tmp_path / "certificate.pem"
    cert = write_certificate(path, NOW + timedelta(days=start), NOW + timedelta(days=end))
    result = certificate_status("https", str(path), NOW)
    assert result.state == state
    assert result.days_remaining == end
    assert result.expires_at == NOW + timedelta(days=end)
    assert result.fingerprint_sha256 == cert.fingerprint(hashes.SHA256()).hex()
    assert "private-subject" not in result.model_dump_json()
    assert str(path) not in result.model_dump_json()


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo", "invalid", "large"])
def test_certificate_read_failures_are_bounded(tmp_path, kind):
    path = tmp_path / "certificate.pem"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "invalid":
        path.write_text("private-content")
    elif kind == "large":
        write_certificate(path)
        with path.open("ab") as stream:
            stream.write(b"x" * CERTIFICATE_MAX_BYTES)
    result = certificate_status("https", str(path), NOW)
    assert result.state == ("missing" if kind == "missing" else "unreadable")
    assert result.expires_at is None and result.fingerprint_sha256 is None
    assert "private" not in result.model_dump_json()


def test_certificate_not_configured_and_replacement(tmp_path):
    assert certificate_status("https", "", NOW).state == "not_configured"
    path = tmp_path / "cert.pem"
    write_certificate(path)
    first = certificate_status("https", str(path), NOW)
    pending = tmp_path / "new.pem"
    write_certificate(pending, end=NOW + timedelta(days=20))
    pending.replace(path)
    second = certificate_status("https", str(path), NOW)
    assert second.state == "expiring" and first.fingerprint_sha256 != second.fingerprint_sha256


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr("factoryvpn_common.backup_catalog.now", lambda: NOW.isoformat())
    result = Catalog(tmp_path / "control")
    result.initialize()
    return result


@pytest.mark.parametrize("seconds", [10, 30 * 86400 + 10])
def test_certificate_state_expires_at_next_time_boundary(tmp_path, seconds):
    path = tmp_path / "cert.pem"
    write_certificate(path, end=NOW + timedelta(seconds=seconds))
    assert certificate_status("https", str(path), NOW).valid_for_seconds == 10


def summary(catalog):
    return backup_summary(str(catalog.root), "/unused-private-archives", NOW)


def test_backup_not_configured_or_missing_does_not_initialize(tmp_path):
    assert backup_summary("", "", NOW).state == "not_configured"
    assert backup_summary(str(tmp_path / "absent"), "/unused", NOW).state == "unavailable"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "age,state",
    [(None, "unknown"), (-60, "unknown"), (0, "online"), (29, "online"), (30, "offline")],
)
def test_backup_heartbeat_and_empty_catalog(catalog, age, state):
    if age is not None:
        catalog.meta("heartbeat", (NOW - timedelta(seconds=age)).isoformat())
    before = catalog.path.read_bytes()
    result = summary(catalog)
    assert result.state == "available" and result.copies == 0
    assert result.latest_backup is None and result.last_test is None
    assert result.coordinator_state == state
    assert result.valid_for_seconds == (30 - age if state == "online" else 30)
    assert catalog.path.read_bytes() == before


def test_backup_summary_keeps_latest_failure_and_current_operation(catalog):
    identifier = uuid4()
    catalog.add_backup(identifier, 100, "private-hash", {"password": "private-secret"})
    with catalog.connect() as db:
        db.execute(
            "UPDATE backups SET created_at=?,tested_at=? WHERE id=?",
            (
                (NOW - timedelta(days=1)).isoformat(),
                (NOW - timedelta(hours=2)).isoformat(),
                str(identifier),
            ),
        )
    failed = catalog.enqueue("verify", {"user_id": "private-actor"}, uuid4(), identifier)
    catalog.update_job(failed["id"], state="failed", error="private-exception")
    with catalog.connect() as db:
        db.execute(
            "UPDATE jobs SET finished_at=?,created_at=? WHERE id=?",
            (
                (NOW - timedelta(hours=1)).isoformat(),
                (NOW - timedelta(hours=1)).isoformat(),
                failed["id"],
            ),
        )
    catalog.enqueue("create", {"user_id": "private-actor"}, uuid4())
    result = summary(catalog)
    assert result.copies == 1 and result.latest_backup.id == identifier
    assert result.latest_backup.tested_at == NOW - timedelta(hours=2)
    assert result.last_test.state == "failed"
    assert result.active_operation.kind == "create" and result.active_operation.state == "queued"
    assert result.last_operation == result.active_operation
    assert "private" not in result.model_dump_json()
    with catalog.connect() as db:
        db.execute("UPDATE backups SET tested_at=?", (NOW.isoformat(),))
    assert summary(catalog).last_test.state == "succeeded"


@pytest.mark.parametrize("broken", ["schema", "heartbeat", "row", "locked"])
def test_backup_catalog_errors_do_not_break_status_or_leak_details(catalog, broken):
    if broken == "schema":
        with catalog.connect() as db:
            db.execute("DROP TABLE jobs")
    elif broken == "heartbeat":
        catalog.meta("heartbeat", "private-invalid-date")
    elif broken == "row":
        catalog.add_backup(uuid4(), 1, "private", {})
        with catalog.connect() as db:
            db.execute("UPDATE backups SET created_at='private-invalid-date'")
    if broken == "locked":
        with catalog.connect() as db:
            db.execute("BEGIN EXCLUSIVE")
            result = summary(catalog)
    else:
        result = summary(catalog)
    assert result.state == "unavailable" and result.copies is None
    assert "private" not in json.dumps(result.model_dump(mode="json"))
