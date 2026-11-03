"""Host-side backup/recovery supervisor. Only fixed Compose services and commands.

Run `python -m scripts.backup_coordinator init`, then `... serve`. The web API
has no Docker socket, CA mount, recovery key, or access to the private work area.
"""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from dotenv import dotenv_values, set_key

from factoryvpn_common.backup_archive import (
    BackupError,
    PKI_FILES,
    CHUNK,
    file_hash,
    load_key,
    pack,
    unpack,
    private_write,
    public_manifest,
    encrypt,
    decrypt,
)
from factoryvpn_common.backup_catalog import Catalog, identifier, now

SERVICES = ("api", "provisioning-worker", "session-worker", "openvpn")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Docker:
    def __init__(self, project, env_file):
        self.project, self.env_file = Path(project).resolve(), Path(env_file).resolve()
        project_name = dotenv_values(self.env_file).get("COMPOSE_PROJECT_NAME", "factoryvpn")
        import re

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project_name):
            raise BackupError("invalid_project")
        self.command = [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--project-directory",
            str(self.project),
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.project / "docker-compose.yml"),
        ]

    def run(self, args, *, data=None, stdout=subprocess.PIPE, timeout=600):
        result = subprocess.run(
            args,
            input=data,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=self.project,
        )
        if result.returncode:
            raise BackupError("service_command_failed")
        return result.stdout

    def compose(self, *args, **kwargs):
        return self.run([*self.command, *args], **kwargs)

    def helper_command(self, operation):
        return [
            *self.command,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "api",
            "python",
            "-m",
            "scripts.backup_database",
            operation,
        ]

    def helper(self, operation, request=None):
        return json.loads(
            self.run(
                self.helper_command(operation), data=json.dumps(request or {}).encode() + b"\n"
            )
        )

    @contextmanager
    def snapshot(self, actor):
        process = subprocess.Popen(
            self.helper_command("snapshot"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.project,
        )
        try:
            process.stdin.write(json.dumps({"actor": actor}).encode() + b"\n")
            process.stdin.flush()
            state = self.read_line(process)
            yield process, state
        finally:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()

    @staticmethod
    def read_line(process):
        import selectors

        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=120):
                raise BackupError("snapshot_timeout")
            line = process.stdout.readline(2 * CHUNK)
            if not line or len(line) >= 2 * CHUNK:
                raise BackupError("snapshot_failed")
            return json.loads(line)
        finally:
            selector.close()

    def bounded_output(self, command, path, maximum, *, timeout=600):
        """Bound disk consumption while the producer runs, including stalled producers."""
        import selectors

        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=self.project
        )
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline, total = time.monotonic() + timeout, 0
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(timeout=remaining):
                        raise BackupError("snapshot_timeout")
                    data = os.read(process.stdout.fileno(), CHUNK)
                    if not data:
                        break
                    total += len(data)
                    if total > maximum:
                        raise BackupError("archive_too_large")
                    out.write(data)
            if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                raise BackupError("database_dump_failed")
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    def dump(self, path, snapshot, maximum):
        # Snapshot ID is generated by PostgreSQL, never supplied by the browser.
        self.bounded_output(
            [
                *self.command,
                "exec",
                "-T",
                "db",
                "pg_dump",
                "-U",
                "factoryvpn",
                "-d",
                "factoryvpn",
                "-Fc",
                "--snapshot=" + snapshot,
            ],
            path,
            maximum,
        )

    def restore_database(self, path):
        self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "factoryvpn",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            data=(
                b"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                b"WHERE datname='factoryvpn' AND pid <> pg_backend_pid();\n"
                b"DROP DATABASE IF EXISTS factoryvpn;\n"
                b"CREATE DATABASE factoryvpn OWNER factoryvpn;\n"
            ),
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "factoryvpn_recovery_schema", self.project / "migrations/versions/0001_initial.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ddl = (
            ";\n".join(module.STATEMENTS)
            + ";\n"
            + (
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY(version_num));\n"
            )
        )
        self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "factoryvpn",
            "-d",
            "factoryvpn",
            "-v",
            "ON_ERROR_STOP=1",
            data=ddl.encode(),
        )
        with path.open("rb") as source:
            result = subprocess.run(
                [
                    *self.command,
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "-U",
                    "factoryvpn",
                    "-d",
                    "factoryvpn",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--data-only",
                    "--disable-triggers",
                ],
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=600,
            )
        if result.returncode:
            raise BackupError("database_restore_failed")
        # Only shipped DDL enters production. Temporarily disabled FK triggers are
        # revalidated by recreating the known constraints after all table data loads.
        self.compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "factoryvpn",
            "-d",
            "factoryvpn",
            "-v",
            "ON_ERROR_STOP=1",
            data=b"""
            BEGIN;
            DO $$ DECLARE item record; BEGIN
              FOR item IN SELECT c.relname, x.conname, pg_get_constraintdef(x.oid) AS definition
                FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND x.contype='f'
              LOOP
                EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', item.relname, item.conname);
                EXECUTE format('ALTER TABLE public.%I ADD CONSTRAINT %I %s', item.relname, item.conname, item.definition);
              END LOOP;
            END $$;
            COMMIT;
        """,
        )

    def test_dump(self, path):
        """Restore in a disposable container with no network and no host mounts."""
        name = "factoryvpn-restore-check-" + uuid4().hex[:12]
        env = {**os.environ, "POSTGRES_PASSWORD": secrets.token_urlsafe(32)}
        created = False
        try:
            result = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--memory",
                    "1g",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "128",
                    "--tmpfs",
                    "/var/lib/postgresql/data:rw,nosuid,size=2g",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_USER=factoryvpn",
                    "-e",
                    "POSTGRES_DB=factoryvpn",
                    "postgres:15-alpine",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            if result.returncode:
                raise BackupError("verification_container_failed")
            created = True
            deadline = time.monotonic() + 60
            while True:
                result = subprocess.run(
                    ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "factoryvpn"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                if result.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise BackupError("verification_timeout")
                time.sleep(0.5)
            with path.open("rb") as stream:
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "pg_restore",
                        "-U",
                        "factoryvpn",
                        "-d",
                        "factoryvpn",
                        "--no-owner",
                        "--no-acl",
                        "--exit-on-error",
                    ],
                    stdin=stream,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=600,
                )
                if result.returncode:
                    raise BackupError("verification_restore_failed")
            # Compare actual objects with freshly initialized DDL in the same PG version.
            self.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "psql",
                    "-U",
                    "factoryvpn",
                    "-d",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                data=b"CREATE DATABASE factoryvpn_expected;\n",
            )
            import importlib.util

            source = self.project / "migrations/versions/0001_initial.py"
            spec = importlib.util.spec_from_file_location("factoryvpn_restore_expected", source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "psql",
                    "-U",
                    "factoryvpn",
                    "-d",
                    "factoryvpn_expected",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                data=(";\n".join(module.STATEMENTS) + ";\n").encode(),
            )
            # PostgreSQL rewrites array coercions during dump/restore. Compare both
            # sides after the same round trip instead of weakening constraint checks.
            schema_dump = self.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_dump",
                    "-U",
                    "factoryvpn",
                    "-d",
                    "factoryvpn_expected",
                    "--schema-only",
                    "-Fc",
                ]
            )
            self.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "psql",
                    "-U",
                    "factoryvpn",
                    "-d",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                ],
                data=b"CREATE DATABASE factoryvpn_normalized;\n",
            )
            self.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    name,
                    "pg_restore",
                    "-U",
                    "factoryvpn",
                    "-d",
                    "factoryvpn_normalized",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                ],
                data=schema_dump,
            )
            query = b"""
            SELECT json_build_object(
              'columns', (SELECT json_agg(r ORDER BY table_name, ordinal_position) FROM
                (SELECT table_name, column_name, ordinal_position, is_nullable, data_type,
                        udt_name, column_default FROM information_schema.columns
                 WHERE table_schema='public' AND table_name <> 'alembic_version') r),
              'constraints', (SELECT json_agg(r ORDER BY relname, conname) FROM
                (SELECT c.relname, x.conname, pg_get_constraintdef(x.oid), x.convalidated
                 FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname='public' AND c.relname <> 'alembic_version') r),
              'indexes', (SELECT json_agg(r ORDER BY indexname) FROM
                (SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public'
                 AND tablename <> 'alembic_version') r));
            """
            structures = [
                self.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        name,
                        "psql",
                        "-U",
                        "factoryvpn",
                        "-d",
                        database,
                        "-At",
                        "-v",
                        "ON_ERROR_STOP=1",
                    ],
                    data=query,
                )
                for database in ("factoryvpn", "factoryvpn_normalized")
            ]
            if structures[0] != structures[1]:
                self.schema_mismatch = [json.loads(value) for value in structures]
                raise BackupError("incompatible_schema")
        finally:
            if created:
                self.run(["docker", "rm", "-f", name], timeout=30)


class Coordinator:
    def __init__(self, project, env_file=None):
        self.project = Path(project).resolve()
        self.env_file = Path(env_file or self.project / ".env").resolve()
        self.docker = Docker(self.project, self.env_file)
        self.catalog = Catalog(self.project / ".backup-control")
        self.archives = self.project / ".backups"
        self.private = self.project / ".backup-private"
        self.key_file = self.private / "recovery.key"
        values = dotenv_values(self.env_file)
        runtime = values.get("FACTORYVPN_RUNTIME_DIR", ".runtime")
        self.runtime = (self.project / runtime).resolve()
        self.maximum = int(values.get("BACKUP_MAX_BYTES", str(512 * CHUNK)))

    def initialize(self, recovery_key=None):
        for directory in (self.archives, self.private):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.catalog.initialize()
        if not self.key_file.exists():
            key = load_key(recovery_key) if recovery_key else os.urandom(32)
            private_write(self.key_file, base64.b64encode(key) + b"\n")
        elif recovery_key and load_key(recovery_key) != load_key(self.key_file):
            raise BackupError("existing_recovery_key")
        for name, value in (("FACTORYVPN_UID", os.getuid()), ("FACTORYVPN_GID", os.getgid())):
            set_key(str(self.env_file), name, str(value), quote_mode="always")

    @contextmanager
    def exclusive(self):
        with (self.private / "coordinator.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    @contextmanager
    def temporary(self):
        if shutil.disk_usage(self.private).free < self.maximum * 3:
            raise BackupError("insufficient_disk")
        with tempfile.TemporaryDirectory(prefix="job-", dir=self.private) as folder:
            yield Path(folder)

    def archive(self, backup_id):
        return self.archives / (identifier(backup_id) + ".fvbackup")

    def phase(self, job, value):
        self.catalog.update_job(job["id"], phase=value)

    def create(self, actor, *, protected=False):
        backup_id = str(uuid4())
        target = self.archive(backup_id)
        with self.temporary() as work:
            with self.docker.snapshot(actor) as (process, snapshot):
                for name in PKI_FILES:
                    path = self.runtime / name
                    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
                        raise BackupError("invalid_runtime")
                    if path.stat().st_size > CHUNK:
                        raise BackupError("invalid_runtime")
                    private_write(work / "runtime" / name, path.read_bytes())
                private_write(
                    work / "configuration.json", json.dumps(snapshot["configuration"]).encode()
                )
                process.stdin.write(b"release\n")
                process.stdin.flush()
                if not self.docker.read_line(process).get("released"):
                    raise BackupError("snapshot_failed")
                self.docker.dump(work / "database.dump", snapshot["snapshot"], self.maximum)
                process.stdin.write(b"done\n")
                process.stdin.flush()
                if process.wait(timeout=30):
                    raise BackupError("snapshot_failed")
            if sum(p.stat().st_size for p in work.rglob("*") if p.is_file()) > self.maximum - CHUNK:
                raise BackupError("archive_too_large")
            temporary = self.archives / (backup_id + ".partial")
            try:
                manifest = pack(
                    work,
                    temporary,
                    load_key(self.key_file),
                    {
                        "created_at": now(),
                        "installation_id": self.catalog.meta("installation_id"),
                        "postgres_major": 15,
                    },
                )
                with self.temporary() as checked:
                    unpack(temporary, checked, load_key(self.key_file), self.maximum)
                os.replace(temporary, target)
                sync_directory(self.archives)
                self.catalog.add_backup(
                    backup_id,
                    target.stat().st_size,
                    file_hash(target),
                    public_manifest(manifest),
                    protected=protected,
                )
            finally:
                temporary.unlink(missing_ok=True)
        return backup_id

    def validate_runtime(self, work):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from scripts.bootstrap import load_ca

        root = work / "runtime"
        _, ca = load_ca(root)
        crl = x509.load_pem_x509_crl((root / "server/crl.pem").read_bytes())
        if not crl.is_signature_valid(ca.public_key()):
            raise BackupError("invalid_runtime")
        for cert_path, key_path in (
            ("ca/ca.crt", "ca/ca.key"),
            ("server/server.crt", "server/server.key"),
            ("https/cert.pem", "https/key.pem"),
        ):
            cert = x509.load_pem_x509_certificate((root / cert_path).read_bytes())
            key = serialization.load_pem_private_key((root / key_path).read_bytes(), password=None)
            if cert.public_key() != key.public_key():
                raise BackupError("invalid_runtime")
            if cert.not_valid_after_utc <= datetime.now(timezone.utc):
                raise BackupError("certificate_expired")
            if cert_path in ("ca/ca.crt", "server/server.crt"):
                cert.verify_directly_issued_by(ca)
        if (root / "ca/ca.crt").read_bytes() != (root / "server/ca.crt").read_bytes():
            raise BackupError("invalid_runtime")

    @contextmanager
    def verified(self, backup_id):
        row = self.catalog.backup(backup_id)
        path = self.archive(backup_id)
        if path.is_symlink() or file_hash(path) != row["sha256"]:
            raise BackupError("checksum_failed")
        with self.temporary() as work:
            manifest = unpack(path, work, load_key(self.key_file), self.maximum)
            if manifest["installation_id"] != self.catalog.meta("installation_id"):
                raise BackupError("different_installation")
            self.validate_runtime(work)
            yield work

    def test_restore(self, work):
        result = self.docker.helper(
            "validate_configuration",
            {"configuration": json.loads((work / "configuration.json").read_bytes())},
        )
        if not result.get("valid"):
            raise BackupError("incompatible_origin")
        self.docker.test_dump(work / "database.dump")

    def verify(self, backup_id):
        with self.verified(backup_id) as work:
            self.test_restore(work)
        self.catalog.tested(backup_id)

    def import_archive(self, job):
        source = self.catalog.root / "imports" / (identifier(job["id"]) + ".upload")
        backup_id = str(uuid4())
        try:
            with self.temporary() as work:
                manifest = unpack(source, work, load_key(self.key_file), self.maximum)
                if manifest["installation_id"] != self.catalog.meta("installation_id"):
                    raise BackupError("different_installation")
                self.validate_runtime(work)
                self.test_restore(work)
                os.replace(source, self.archive(backup_id))
                sync_directory(self.archives)
                self.catalog.add_backup(
                    backup_id,
                    self.archive(backup_id).stat().st_size,
                    file_hash(self.archive(backup_id)),
                    public_manifest(manifest),
                )
                self.catalog.tested(backup_id)
            return backup_id
        finally:
            source.unlink(missing_ok=True)

    def apply(self, work, security, actor_id=None, recovery=None):
        configuration = json.loads((work / "configuration.json").read_bytes())
        from scripts.backup_database import CONFIG_KEYS

        if set(configuration) != set(CONFIG_KEYS):
            raise BackupError("invalid_configuration")
        # Deployment endpoints stay those of the recovery target, including database password.
        keep = {"OPENVPN_SERVER_HOST", "OPENVPN_SERVER_PORT", "PORTAL_PUBLIC_URL", "SSO_PUBLIC_URL"}
        env_target = self.private / "environment.pending"
        env_target.unlink(missing_ok=True)
        private_write(env_target, self.env_file.read_bytes())
        for key, value in configuration.items():
            if key not in keep:
                value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                set_key(str(env_target), key, value, quote_mode="always")
        for key in ("JWT_SECRET", "WORKER_API_KEY"):
            set_key(str(env_target), key, secrets.token_urlsafe(48), quote_mode="always")
        self.docker.restore_database(work / "database.dump")
        for name in PKI_FILES:
            target = self.runtime / name
            if any(p.is_symlink() for p in (target, *target.parents)):
                raise BackupError("invalid_runtime")
            temporary = target.with_name(target.name + ".restore-pending")
            temporary.unlink(missing_ok=True)
            private_write(temporary, (work / "runtime" / name).read_bytes())
            os.replace(temporary, target)
            sync_directory(target.parent)
        with env_target.open("rb") as persisted:
            os.fsync(persisted.fileno())
        os.replace(env_target, self.env_file)
        sync_directory(self.env_file.parent)
        self.docker.helper(
            "sanitize", {"security": security, "actor_id": actor_id, "recovery": recovery}
        )
        # Publish the sanitized certificate registry before opening the VPN listener.
        self.docker.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "provisioning-worker",
            "python",
            "-m",
            "scripts.backup_publish_crl",
        )

    def start(self):
        self.docker.compose(
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--wait",
            "--wait-timeout",
            "120",
            *SERVICES,
            timeout=180,
        )
        self.docker.helper("check")
        self.docker.compose(
            "exec",
            "-T",
            "openvpn",
            "python",
            "-c",
            "from factoryvpn_common.openvpn import ManagementClient; ManagementClient().clients()",
        )
        self.docker.compose(
            "exec",
            "-T",
            "session-worker",
            "python",
            "-c",
            "import os,httpx; r=httpx.get(os.environ['API_URL']+'/api/v1/internal/sessions/work', headers={'X-Worker-Key':os.environ['WORKER_API_KEY']},timeout=10); r.raise_for_status()",
        )
        # Nginx resolves its upstream at startup. Recreate it only after the new
        # API container is healthy, so it cannot retain the previous container IP.
        self.docker.compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "nginx")

    def stop(self):
        self.docker.compose("stop", *SERVICES)
        # Destroy namespace/socket state before considering restored allocations released.
        self.docker.compose("rm", "-f", "session-worker", "openvpn")

    def save_journal(self, value):
        path = self.private / "restore.pending"
        path.unlink(missing_ok=True)
        with path.open("xb") as out:
            os.chmod(path, 0o600)
            encrypt(io.BytesIO(json.dumps(value).encode()), out, load_key(self.key_file))
            out.flush()
            os.fsync(out.fileno())
        os.replace(path, self.private / "restore.journal")
        sync_directory(self.private)

    def rollback(self):
        journal = self.private / "restore.journal"
        if not journal.exists():
            return False
        out = io.BytesIO()
        with journal.open("rb") as source:
            decrypt(source, out, load_key(self.key_file), self.maximum)
        state = json.loads(out.getvalue())
        self.stop()
        # Preserve the exact pre-restore deployment config, then rotate access keys in apply().
        pending = self.private / "environment.rollback"
        pending.unlink(missing_ok=True)
        private_write(pending, state["environment"].encode())
        os.replace(pending, self.env_file)
        with self.verified(state["safety_id"]) as work:
            self.apply(work, state["security"], state.get("actor_id"))
        self.start()
        journal.unlink()
        sync_directory(self.private)
        self.catalog.meta("maintenance", "")
        self.catalog.event("restore_rolled_back", target=state["safety_id"])
        return True

    def restore(self, job, actor, recovery=None):
        backup_id = job["backup_id"]
        self.phase(job, "verifying")
        self.verify(backup_id)
        # Last authorization check before accepting the disruptive operation.
        self.docker.helper("authorize", {"actor": actor})
        self.catalog.meta("maintenance", job["id"])
        self.phase(job, "closing_sessions")
        stopped = False
        try:
            self.docker.helper("close", {"actor": actor})
            deadline = time.monotonic() + 90
            while not self.docker.helper("drained").get("drained"):
                if time.monotonic() > deadline:
                    raise BackupError("vpn_cleanup_timeout")
                time.sleep(2)
            stopped = True
            self.stop()
            console = {"console": True}
            security = self.docker.helper("security", {"actor": console})
            self.phase(job, "safety_backup")
            safety_id = self.create(console, protected=True)
            self.save_journal(
                {
                    "safety_id": safety_id,
                    "security": security,
                    "actor_id": actor.get("user_id"),
                    "environment": self.env_file.read_text(),
                }
            )
            self.phase(job, "restoring")
            with self.verified(backup_id) as work:
                self.apply(work, {} if recovery else security, actor.get("user_id"), recovery)
            self.phase(job, "restarting")
            self.start()
            (self.private / "restore.journal").unlink()
            sync_directory(self.private)
            self.catalog.meta("maintenance", "")
            with self.catalog.connect() as db:
                db.execute("UPDATE backups SET protected=0 WHERE id <> ?", (safety_id,))
            self.retain()
        except Exception:
            if not self.rollback():
                if stopped:
                    self.start()
                self.catalog.meta("maintenance", "")
            raise

    def restore_file(self, path):
        """Console-only disaster recovery: explicitly re-enroll old access identities."""
        import getpass

        with self.temporary() as work:
            manifest = unpack(path, work, load_key(self.key_file), self.maximum)
            self.validate_runtime(work)
            self.docker.test_dump(work / "database.dump")
        if manifest["installation_id"] != self.catalog.meta("installation_id"):
            if self.catalog.backups() or self.catalog.jobs():
                raise BackupError("different_installation")
            self.catalog.meta("installation_id", manifest["installation_id"])
        username = input("New recovery administrator username: ").strip()
        from api.services.mail_service import email_address

        email = email_address(input("New recovery administrator email: "))
        import re

        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
            raise BackupError("invalid_recovery_username")
        password = getpass.getpass("New recovery administrator password (at least 12 characters): ")
        if (
            len(password) < 12
            or len(password) > 128
            or password != getpass.getpass("Repeat password: ")
        ):
            raise BackupError("invalid_recovery_password")
        if (
            input("This replaces FactoryVPN data and disables old credentials. Type RIPRISTINA: ")
            != "RIPRISTINA"
        ):
            raise BackupError("restore_cancelled")
        backup_id = str(uuid4())
        with open(path, "rb") as source, self.archive(backup_id).open("xb") as target:
            os.chmod(self.archive(backup_id), 0o600)
            shutil.copyfileobj(source, target, CHUNK)
        self.catalog.add_backup(
            backup_id,
            self.archive(backup_id).stat().st_size,
            file_hash(self.archive(backup_id)),
            public_manifest(manifest),
            protected=True,
        )
        actor = {"console": True, "user_id": None}
        job = self.catalog.enqueue("restore", actor, uuid4(), backup_id)
        self.catalog.claim()
        try:
            self.restore(job, actor, {"username": username, "password": password, "email": email})
            self.catalog.update_job(job["id"], state="succeeded", phase="complete")
            self.catalog.event("disaster_recovery_completed", target=backup_id)
        except Exception:
            self.catalog.update_job(job["id"], state="failed", error="restore_failed")
            raise

    def delete(self, backup_id):
        row = self.catalog.backup(backup_id)
        if row["protected"]:
            raise BackupError("protected_backup")
        self.archive(backup_id).unlink(missing_ok=True)
        with self.catalog.connect() as db:
            db.execute("DELETE FROM backups WHERE id=?", (identifier(backup_id),))

    def retain(self):
        backups = [b for b in self.catalog.backups() if not b["protected"]]
        count = int(self.catalog.meta("retention") or "10")
        for row in backups[count:]:
            self.delete(row["id"])
            self.catalog.event("backup_retention_deleted", target=row["id"])

    def recover(self):
        if not self.rollback() and self.catalog.meta("maintenance"):
            self.start()
            self.catalog.meta("maintenance", "")
        for job in self.catalog.jobs():
            if job["state"] == "running":
                self.catalog.update_job(job["id"], state="failed", error="operation_interrupted")
                (self.catalog.root / "imports" / (job["id"] + ".upload")).unlink(missing_ok=True)
        for path in self.private.glob("job-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        for path in self.archives.glob("*.partial"):
            path.unlink()

    def cycle(self):
        self.catalog.meta("heartbeat", now())
        # API process loss cannot reserve the global operation slot indefinitely.
        for stale in self.catalog.jobs():
            if (
                stale["state"] == "uploading"
                and (
                    datetime.now(timezone.utc) - datetime.fromisoformat(stale["created_at"])
                ).total_seconds()
                > 600
            ):
                self.catalog.update_job(stale["id"], state="failed", error="upload_interrupted")
                (self.catalog.root / "imports" / (identifier(stale["id"]) + ".upload")).unlink(
                    missing_ok=True
                )
        job = self.catalog.claim()
        if not job:
            return
        actor = json.loads(job["actor"])
        try:
            if actor.get("console"):
                raise BackupError("invalid_actor")
            self.docker.helper("authorize", {"actor": actor})
            self.phase(job, "running")
            backup_id = job["backup_id"]
            if job["kind"] == "create":
                backup_id = self.create(actor)
                self.retain()
            elif job["kind"] == "import":
                backup_id = self.import_archive(job)
            elif job["kind"] == "verify":
                self.verify(backup_id)
            elif job["kind"] == "restore":
                self.restore(job, actor)
            elif job["kind"] == "delete":
                self.delete(backup_id)
            else:
                raise BackupError("invalid_operation")
            self.catalog.update_job(
                job["id"], state="succeeded", phase="complete", backup_id=backup_id
            )
            self.catalog.event("backup_" + job["kind"], actor["user_id"], backup_id)
        except Exception as exc:
            code = str(exc) if isinstance(exc, BackupError) else "operation_failed"
            self.catalog.update_job(job["id"], state="failed", error=code)
            self.catalog.event("backup_failed", actor.get("user_id"), job["id"], code=code)
            # A failed rollback leaves maintenance in place and requires console recovery.
            if self.catalog.meta("maintenance"):
                raise BackupError("console_recovery_required") from None
        finally:
            if job["kind"] == "import":
                (self.catalog.root / "imports" / (identifier(job["id"]) + ".upload")).unlink(
                    missing_ok=True
                )

    @contextmanager
    def heartbeat(self):
        import threading

        stopped = threading.Event()

        def pulse():
            while not stopped.is_set():
                self.catalog.meta("heartbeat", now())
                stopped.wait(5)

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "serve", "once", "create", "recover", "restore-file")
    )
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--recovery-key-file", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    coordinator = Coordinator(args.project, args.env_file)
    if args.command == "init":
        coordinator.initialize(args.recovery_key_file)
        print(
            "FactoryVPN backup initialized. Store an offline copy of .backup-private/recovery.key."
        )
        return
    load_key(coordinator.key_file)
    with coordinator.exclusive(), coordinator.heartbeat():
        coordinator.recover()
        if args.command == "recover":
            return
        if args.command == "restore-file":
            if not args.archive:
                raise BackupError("archive_required")
            coordinator.restore_file(args.archive)
            return
        if args.command == "create":
            print(coordinator.create({"console": True}))
            return
        while True:
            coordinator.cycle()
            if args.command == "once":
                return
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except (BackupError, OSError, subprocess.SubprocessError) as exc:
        print(
            str(exc) if isinstance(exc, BackupError) else "Backup coordinator failed",
            file=__import__("sys").stderr,
        )
        raise SystemExit(1)
