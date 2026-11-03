"""Recovery control plane outside the application database. Only UUID file names."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from factoryvpn_common.backup_archive import BackupError


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    return str(UUID(str(value)))


class Catalog:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "catalog.sqlite"

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "imports").mkdir(exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript(
                """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS backups (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT, state TEXT NOT NULL, manifest TEXT, verified_at TEXT,
                tested_at TEXT, protected INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, backup_id TEXT, actor TEXT NOT NULL,
                request_key TEXT UNIQUE NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL,
                created_at TEXT NOT NULL, finished_at TEXT, error TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_backup_operation ON jobs ((1))
                WHERE state IN ('queued','running','uploading');
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, action TEXT NOT NULL,
                actor_id TEXT, target_id TEXT, detail TEXT NOT NULL);
            """
            )
            db.execute("INSERT OR IGNORE INTO meta VALUES ('installation_id', ?)", (str(uuid4()),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('retention', '10')")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def meta(self, key, value=None):
        if key == "maintenance":
            marker = self.root / "maintenance"
            if value is None:
                return marker.read_text() if marker.exists() else ""
            if value:
                from factoryvpn_common.backup_archive import private_write

                pending = self.root / ("maintenance-" + uuid4().hex)
                private_write(pending, str(value).encode())
                os.replace(pending, marker)
            else:
                marker.unlink(missing_ok=True)
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return str(value)
        with self.connect() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def event(self, action, actor=None, target=None, **detail):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(timestamp,action,actor_id,target_id,detail) VALUES (?,?,?,?,?)",
                (now(), action, actor, target, json.dumps(detail)),
            )

    def enqueue(self, kind, actor, request_key, backup_id=None, *, uploading=False):
        if kind not in {"create", "verify", "restore", "delete", "import"}:
            raise BackupError("invalid_operation")
        request_key = identifier(request_key)
        backup_id = identifier(backup_id) if backup_id else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if old:
                if (
                    old["kind"] != kind
                    or old["backup_id"] != backup_id
                    or json.loads(old["actor"])["user_id"] != actor["user_id"]
                ):
                    raise BackupError("request_conflict")
                return dict(old)
            if db.execute(
                "SELECT 1 FROM jobs WHERE state IN ('queued','running','uploading')"
            ).fetchone():
                raise BackupError("operation_in_progress")
            job_id = str(uuid4())
            db.execute(
                "INSERT INTO jobs(id,kind,backup_id,actor,request_key,state,phase,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    kind,
                    backup_id,
                    json.dumps(actor),
                    request_key,
                    "uploading" if uploading else "queued",
                    "queued",
                    now(),
                ),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self):
        with self.connect() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30")
            ]

    def backups(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM backups ORDER BY created_at DESC")]

    def backup(self, value):
        with self.connect() as db:
            row = db.execute("SELECT * FROM backups WHERE id=?", (identifier(value),)).fetchone()
            if not row:
                raise BackupError("backup_not_found")
            return dict(row)

    def update_job(self, job_id, *, phase=None, state=None, error=None, backup_id=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET phase=COALESCE(?,phase), state=COALESCE(?,state), "
                "error=?, backup_id=COALESCE(?,backup_id), finished_at=? WHERE id=?",
                (
                    phase,
                    state,
                    error,
                    backup_id,
                    now() if state in {"succeeded", "failed"} else None,
                    identifier(job_id),
                ),
            )

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row:
                db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
                return dict(row)

    def finish_upload(self, job_id):
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET state='queued' WHERE id=? AND state='uploading'",
                (identifier(job_id),),
            )
            if result.rowcount != 1:
                raise BackupError("upload_interrupted")

    def add_backup(self, backup_id, size, sha256, manifest, *, protected=False):
        with self.connect() as db:
            db.execute(
                "INSERT INTO backups(id,created_at,size,sha256,state,manifest,verified_at,protected) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    identifier(backup_id),
                    manifest.get("created_at", now()),
                    size,
                    sha256,
                    "verified",
                    json.dumps(manifest),
                    now(),
                    int(protected),
                ),
            )

    def tested(self, backup_id):
        with self.connect() as db:
            db.execute("UPDATE backups SET tested_at=? WHERE id=?", (now(), identifier(backup_id)))


def public_job(job):
    return {
        key: job[key]
        for key in (
            "id",
            "kind",
            "backup_id",
            "state",
            "phase",
            "created_at",
            "finished_at",
            "error",
        )
    }
