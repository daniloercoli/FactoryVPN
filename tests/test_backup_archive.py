"""Archive authenticity, extraction boundaries, and recovery job durability."""

import io
import os
import tarfile
from uuid import uuid4

import pytest

from factoryvpn_common.backup_archive import (
    BackupError,
    CHUNK,
    decrypt,
    encrypt,
    pack,
    private_write,
    unpack,
)
from factoryvpn_common.backup_catalog import Catalog, public_job


@pytest.mark.parametrize("size", [0, 17, CHUNK, CHUNK + 31])
def test_stream_round_trip_and_fail_closed(size):
    key, data = os.urandom(32), os.urandom(size)
    archive = io.BytesIO()
    encrypt(io.BytesIO(data), archive, key)
    out = io.BytesIO()
    decrypt(io.BytesIO(archive.getvalue()), out, key, size)
    assert out.getvalue() == data
    encoded = archive.getvalue()
    for changed in (
        encoded[:-1],
        encoded + b"extra",
        encoded[:40] + bytes([encoded[40] ^ 1]) + encoded[41:],
    ):
        with pytest.raises(BackupError):
            decrypt(io.BytesIO(changed), io.BytesIO(), key, size)
    with pytest.raises(BackupError):
        decrypt(io.BytesIO(encoded), io.BytesIO(), os.urandom(32), size)
    if size:
        with pytest.raises(BackupError, match="archive_too_large"):
            decrypt(io.BytesIO(encoded), io.BytesIO(), key, size - 1)


def test_package_checks_complete_manifest(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    from scripts.lab_pki import initialize

    initialize(source / "runtime", "vpn.example.test")
    for name in ("database.dump", "configuration.json"):
        private_write(source / name, name.encode())
    key = os.urandom(32)
    path = tmp_path / "backup"
    manifest = pack(
        source,
        path,
        key,
        {
            "postgres_major": 15,
            "installation_id": str(uuid4()),
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    assert unpack(path, target, key, 1024 * 1024) == manifest
    assert (target / "database.dump").read_bytes() == b"database.dump"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("database.dump", tarfile.SYMTYPE),
        ("database.dump", tarfile.LNKTYPE),
    ],
)
def test_no_archive_paths_or_links_are_extracted(tmp_path, name, kind):
    plain = io.BytesIO()
    with tarfile.open(fileobj=plain, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, "../escape"
        archive.addfile(member, io.BytesIO())
    key = os.urandom(32)
    path = tmp_path / "archive"
    with path.open("wb") as out:
        encrypt(io.BytesIO(plain.getvalue()), out, key)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(BackupError, match="invalid_file"):
        unpack(path, work, key, CHUNK)
    assert not (tmp_path / "escape").exists()


def test_jobs_are_idempotent_serialized_and_not_in_the_restored_database(tmp_path):
    catalog = Catalog(tmp_path / "control")
    catalog.initialize()
    actor = {"user_id": str(uuid4()), "web_session_id": str(uuid4()), "version": 1}
    key = str(uuid4())
    job = catalog.enqueue("create", actor, key)
    assert catalog.enqueue("create", actor, key)["id"] == job["id"]
    with pytest.raises(BackupError, match="request_conflict"):
        catalog.enqueue("delete", actor, key)
    with pytest.raises(BackupError, match="operation_in_progress"):
        catalog.enqueue("create", actor, uuid4())
    restarted = Catalog(tmp_path / "control")
    assert restarted.claim()["id"] == job["id"]
    assert restarted.claim() is None
    assert "actor" not in public_job(job)
    restarted.update_job(job["id"], state="succeeded")
    assert restarted.enqueue("create", actor, uuid4())["id"] != job["id"]
