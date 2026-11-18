"""Host recovery jobs must survive the loss of an API upload process."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import os
import subprocess
import sys
import pytest

from scripts.backup_coordinator import Coordinator, Docker
from factoryvpn_common.backup_archive import (
    BackupError,
    load_key,
    pack,
    private_write,
)


def test_abandoned_upload_is_removed_and_releases_the_queue(tmp_path):
    (tmp_path / ".env").write_text("")
    coordinator = Coordinator(tmp_path)
    catalog = coordinator.catalog
    catalog.initialize()
    actor = {"user_id": str(uuid4())}
    job = catalog.enqueue("import", actor, uuid4(), uploading=True)
    partial = catalog.root / "imports" / (job["id"] + ".upload")
    partial.write_bytes(b"incomplete")
    with catalog.connect() as db:
        db.execute(
            "UPDATE jobs SET created_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(), job["id"]),
        )
    coordinator.cycle()
    assert not partial.exists()
    assert catalog.jobs()[0]["state"] == "failed"
    assert catalog.enqueue("create", actor, uuid4())["state"] == "queued"


def test_coordinator_commands_use_rotated_deployment_environment(tmp_path, monkeypatch):
    environment = tmp_path / ".env"
    environment.write_text("JWT_SECRET=before\n")
    monkeypatch.setenv("JWT_SECRET", "unrelated-host-key")
    docker = Docker(tmp_path, environment)
    probe = [sys.executable, "-c", "import os; print(os.environ['JWT_SECRET'])"]
    assert docker.run(probe).strip() == b"before"
    environment.write_text("JWT_SECRET=after\n")
    assert docker.run(probe).strip() == b"after"


def test_dump_uses_rotated_deployment_environment(tmp_path, monkeypatch):
    environment = tmp_path / ".env"
    environment.write_text("JWT_SECRET=before\n")
    monkeypatch.setenv("JWT_SECRET", "")
    docker = Docker(tmp_path, environment)
    probe = [sys.executable, "-c", "import os; print(os.environ['JWT_SECRET'])"]
    for value in ("before", "after"):
        environment.write_text("JWT_SECRET=" + value + "\n")
        path = tmp_path / (value + ".dump")
        docker.bounded_output(probe, path, 1024)
        assert path.read_bytes().strip() == value.encode()


def test_database_restore_uses_rotated_deployment_environment(tmp_path, monkeypatch):
    environment = tmp_path / ".env"
    environment.write_text("JWT_SECRET=before\n")
    monkeypatch.setenv("JWT_SECRET", "")
    docker = Docker(tmp_path, environment)
    environment.write_text("JWT_SECRET=after\n")
    schema = tmp_path / "migrations/versions/0001_initial.py"
    schema.parent.mkdir(parents=True)
    schema.write_text("STATEMENTS = ['SELECT 1']\n")
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"isolated database fixture")
    restored = []

    def invoke(args, **kwargs):
        if "pg_restore" in args:
            assert kwargs["stdin"].read() == dump.read_bytes()
            restored.append(kwargs["env"]["JWT_SECRET"])
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr("scripts.backup_coordinator.subprocess.run", invoke)
    docker.restore_database(dump)
    assert restored == ["after"]


@pytest.mark.parametrize("operation", ["apply", "console"])
def test_restore_does_not_load_host_api_settings(tmp_path, operation):
    probe = """
import builtins, getpass, json, sys
from pathlib import Path
import scripts.backup_coordinator as module
from factoryvpn_common.backup_archive import BackupError
from factoryvpn_common.backup_configuration import CONFIG_KEYS

root = Path(sys.argv[1])
(root / '.env').write_text('JWT_SECRET=deployment-key\\n')
coordinator = module.Coordinator(root)
coordinator.initialize()
if sys.argv[2] == 'apply':
    work = root / 'work'
    work.mkdir()
    (work / 'configuration.json').write_text(json.dumps({key: '' for key in CONFIG_KEYS}))
    coordinator.docker.restore_database = lambda _: None
    coordinator.restore_pki = lambda _: None
    coordinator.docker.helper = lambda *args: None
    coordinator.docker.compose = lambda *args: None
    coordinator.apply(work, {})
else:
    module.unpack = lambda *args: {'installation_id': coordinator.catalog.meta('installation_id')}
    coordinator.validate_runtime = lambda _: None
    coordinator.docker.test_dump = lambda _: None
    answers = iter(['recovery-admin', ' Recovery@Example.test ', 'ANNULLA'])
    builtins.input = lambda _: next(answers)
    getpass.getpass = lambda _: 'disposable-recovery-password'
    try:
        coordinator.restore_file(root / 'unused.fixture')
    except BackupError as exc:
        assert str(exc) == 'restore_cancelled', exc
    else:
        raise AssertionError('Expected explicit cancellation before restore')
assert 'api.config' not in sys.modules
assert 'scripts.backup_database' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path), operation],
        env={**os.environ, "ENVIRONMENT": "production", "JWT_SECRET": "", "WORKER_API_KEY": "short"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_coordinator_restart_does_not_cancel_live_upload(tmp_path):
    (tmp_path / ".env").write_text("")
    coordinator = Coordinator(tmp_path)
    coordinator.catalog.initialize()
    job = coordinator.catalog.enqueue("import", {"user_id": str(uuid4())}, uuid4(), uploading=True)
    partial = coordinator.catalog.root / "imports" / (job["id"] + ".upload")
    partial.write_bytes(b"still uploading")
    coordinator.recover()
    assert partial.exists()
    coordinator.catalog.finish_upload(job["id"])
    assert coordinator.catalog.claim()["id"] == job["id"]


def test_revoked_import_removes_upload_before_releasing_queue(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("")
    coordinator = Coordinator(tmp_path)
    coordinator.catalog.initialize()
    job = coordinator.catalog.enqueue("import", {"user_id": str(uuid4())}, uuid4())
    partial = coordinator.catalog.root / "imports" / (job["id"] + ".upload")
    partial.write_bytes(b"completed upload")

    def reject(*args):
        raise BackupError("authorization_expired")

    monkeypatch.setattr(coordinator.docker, "helper", reject)
    coordinator.cycle()
    assert coordinator.catalog.jobs()[0]["state"] == "failed"
    assert not partial.exists()


@pytest.mark.parametrize("size", [1024, 4096])
def test_dump_stops_at_disk_limit(tmp_path, size):
    (tmp_path / ".env").write_text("")
    docker = Docker(tmp_path, tmp_path / ".env")
    path = tmp_path / "dump"
    command = [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'x' * {size})"]
    if size > 1024:
        with pytest.raises(BackupError, match="archive_too_large"):
            docker.bounded_output(command, path, 1024)
        assert path.stat().st_size <= 1024
    else:
        docker.bounded_output(command, path, 1024)
        assert path.read_bytes() == b"x" * size


def test_dump_timeout_terminates_stalled_producer(tmp_path):
    (tmp_path / ".env").write_text("")
    docker = Docker(tmp_path, tmp_path / ".env")
    with pytest.raises(BackupError, match="snapshot_timeout"):
        docker.bounded_output(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path / "dump",
            1024,
            timeout=0.1,
        )


def test_restore_rejected_before_shutdown_does_not_restart_services(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("")
    coordinator = Coordinator(tmp_path)
    coordinator.catalog.initialize()
    actor = {"user_id": str(uuid4())}
    job = coordinator.catalog.enqueue("restore", actor, uuid4(), uuid4())
    monkeypatch.setattr(coordinator, "verify", lambda _: None)

    def helper(operation, request):
        if operation == "close":
            raise BackupError("authorization_expired")
        return {"authorized": True}

    def unexpected_restart():
        pytest.fail("A rejected restore must not recreate running services")

    monkeypatch.setattr(coordinator.docker, "helper", helper)
    monkeypatch.setattr(coordinator, "start", unexpected_restart)
    with pytest.raises(BackupError, match="authorization_expired"):
        coordinator.restore(job, actor)
    assert not coordinator.catalog.meta("maintenance")


@pytest.mark.parametrize("foreign_installation", [False, True])
def test_import_rejects_incompatible_backup_before_publishing(
    tmp_path, monkeypatch, foreign_installation
):
    (tmp_path / ".env").write_text("")
    coordinator = Coordinator(tmp_path)
    coordinator.initialize()
    job = coordinator.catalog.enqueue("import", {"user_id": str(uuid4())}, uuid4())
    source = tmp_path / "source"
    from scripts.lab_pki import initialize

    initialize(source / "runtime", "vpn.example.test")
    for name in ("database.dump", "configuration.json"):
        private_write(source / name, b"{}")
    upload = coordinator.catalog.root / "imports" / (job["id"] + ".upload")
    pack(
        source,
        upload,
        load_key(coordinator.key_file),
        {
            "installation_id": (
                str(uuid4())
                if foreign_installation
                else coordinator.catalog.meta("installation_id")
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "postgres_major": 15,
        },
    )
    # PKI validity is independent of the origin/installation checks under test.
    monkeypatch.setattr(coordinator, "validate_runtime", lambda _: None)
    monkeypatch.setattr(coordinator.docker, "helper", lambda *args: {"valid": False})
    expected = "different_installation" if foreign_installation else "incompatible_origin"
    with pytest.raises(BackupError, match=expected):
        coordinator.import_archive(job)
    assert not coordinator.catalog.backups() and not upload.exists()
    assert not list(coordinator.archives.iterdir())
