"""Exercise backup and recovery on a disposable Compose installation, never production.

Builds the current images, uses random loopback ports and its own PostgreSQL volume.
Requires Docker and a working /dev/net/tun in its Linux VM.
"""

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx
from dotenv import set_key

from scripts.lab_pki import initialize
from scripts.backup_coordinator import Coordinator

ROOT = Path(__file__).resolve().parents[1]


def port(udp=False):
    with socket.socket(type=socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix="factoryvpn-backup-smoke-") as directory:
        project = Path(directory)
        env_file = project / ".env"
        initialize(project / ".runtime", "localhost", env_file)
        https_port, mail_port = port(), port()
        name = "factoryvpn-backup-test-" + uuid4().hex[:10]
        for key, value in {
            "COMPOSE_PROJECT_NAME": name,
            "HTTPS_PORT": str(https_port),
            "HTTP_PORT": str(port()),
            "OPENVPN_SERVER_PORT": str(port(True)),
            "PORTAL_PUBLIC_URL": f"https://localhost:{https_port}",
            "FACTORYVPN_RUNTIME_DIR": str(project / ".runtime"),
            "FACTORYVPN_UID": str(os.getuid()),
            "FACTORYVPN_GID": str(os.getgid()),
            "SMTP_HOST": "test-mail",
            "SMTP_PORT": "2525",
            "SMTP_SECURITY": "plain",
            "SMTP_FROM": "factoryvpn@example.test",
        }.items():
            set_key(str(env_file), key, value)
        # Keep interpolation live: restore rotates secrets in the isolated .env.
        compose = (ROOT / "docker-compose.yml").read_text()
        compose = compose.replace("context: .", "context: " + json.dumps(str(ROOT)))
        compose = compose.replace(
            "./.backup-control:/backup-control",
            str(project / ".backup-control") + ":/backup-control",
        )
        compose = compose.replace(
            "./.backups:/backups:ro", str(project / ".backups") + ":/backups:ro"
        )
        compose = compose.replace(
            "./docker/nginx/nginx.conf:/etc/nginx/nginx.conf:ro",
            str(ROOT / "docker/nginx/nginx.conf") + ":/etc/nginx/nginx.conf:ro",
        )
        for variable in ("HTTP_PORT:-80", "HTTPS_PORT:-443", "OPENVPN_SERVER_PORT:-1194"):
            compose = compose.replace('"${' + variable + "}:", '"127.0.0.1:${' + variable + "}:")
        # SMTP and its code reader exist only on this disposable test network.
        mail_service = f"""  test-mail:
    image: {name}-api
    entrypoint: [python, -m, uvicorn, portal_mail:app, --host, 0.0.0.0, --port, '8001', --no-access-log]
    volumes:
      - {ROOT / 'tests' / 'portal_mail.py'}:/tmp/portal_mail.py:ro
    environment:
      PYTHONPATH: /tmp
      SMTP_PORT: '2525'
      FACTORYVPN_TEST_DATABASE_URL: isolated-smoke
      FACTORYVPN_TEST_SMTP_BIND: 0.0.0.0
    ports: ['127.0.0.1:{mail_port}:8001']
    networks: [backend]
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"]
      interval: 2s
      timeout: 2s
      retries: 20

"""
        compose = compose.replace("  nginx:", mail_service + "  nginx:", 1)
        (project / "docker-compose.yml").write_text(compose)
        # The verifier needs the exact frozen DDL of this build.
        shutil.copytree(
            ROOT / "migrations",
            project / "migrations",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        coordinator = Coordinator(project)
        coordinator.initialize()
        try:
            print("Building isolated backup/recovery installation…", flush=True)
            coordinator.docker.compose(
                "build", "api", "nginx", "openvpn", timeout=600, stdout=subprocess.DEVNULL
            )
            coordinator.docker.compose("up", "-d", "--wait", "--wait-timeout", "120", timeout=180)
            password = secrets.token_urlsafe(24)
            seed = """import asyncio,json,sys
from api.db.session import async_session_maker
from api.models import User
from api.services.auth_service import hash_password
async def main():
 async with async_session_maker() as db:
  db.add(User(username='smoke-root',email='smoke-root@example.test',password_hash=hash_password(json.load(sys.stdin)['password']),is_admin=True,is_active=True))
  await db.commit()
asyncio.run(main())
"""
            coordinator.docker.compose(
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                seed,
                data=json.dumps({"password": password}).encode(),
            )
            origin = f"https://localhost:{https_port}"
            with httpx.Client(
                base_url=origin,
                verify=False,
                timeout=30,
                headers={"Origin": origin, "X-FactoryVPN-Portal": "1"},
            ) as client:

                def complete_email(response, prefix):
                    assert response.status_code == 200, response.json().get(
                        "detail", "Unexpected login response"
                    )
                    code = httpx.get(
                        f"http://127.0.0.1:{mail_port}/messages/smoke-root@example.test"
                    ).json()["code"]
                    return client.post(
                        prefix + "/verify",
                        json={
                            "challenge_token": response.json()["challenge_token"],
                            "code": code,
                        },
                    )

                def login():
                    response = client.post(
                        "/api/v1/web/login", json={"username": "smoke-root", "password": password}
                    )
                    assert response.status_code == 200, response.json().get(
                        "detail", "Unexpected login response"
                    )
                    response = complete_email(response, "/api/v1/web/login")
                    assert response.status_code == 200, response.json().get(
                        "detail", "Unexpected login response"
                    )
                    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]

                login()
                status = client.get("/api/v1/admin/server")
                assert status.status_code == 200, status.text
                readiness = status.json()
                fingerprints = {
                    item["name"]: item["fingerprint_sha256"] for item in readiness["certificates"]
                }
                assert len(fingerprints) == 3 and all(fingerprints.values())
                assert all(item["state"] == "valid" for item in readiness["certificates"])
                assert readiness["backup"]["state"] == "available"
                assert readiness["backup"]["copies"] == 0
                print("Public certificate mounts and read-only backup summary passed.", flush=True)
                response = client.post("/api/v1/backups", headers={"Idempotency-Key": str(uuid4())})
                assert response.status_code == 202, response.text
                coordinator.cycle()
                job = coordinator.catalog.jobs()[0]
                assert job["state"] == "succeeded", job["error"]
                backup_id = job["backup_id"]
                readiness = client.get("/api/v1/admin/server").json()
                assert readiness["backup"]["latest_backup"]["id"] == backup_id
                assert readiness["backup"]["copies"] == 1
                response = client.post(f"/api/v1/backups/{backup_id}/download")
                assert response.status_code == 200
                assert response.content == coordinator.archive(backup_id).read_bytes()
                print(
                    "Encrypted backup and authenticated download passed; testing isolated restore…",
                    flush=True,
                )
                try:
                    coordinator.verify(backup_id)
                except Exception:
                    if hasattr(coordinator.docker, "schema_mismatch"):
                        import difflib

                        actual, expected = coordinator.docker.schema_mismatch
                        print(
                            "\n".join(
                                difflib.unified_diff(
                                    json.dumps(expected, indent=2).splitlines(),
                                    json.dumps(actual, indent=2).splitlines(),
                                    fromfile="expected schema",
                                    tofile="restored schema",
                                )
                            ),
                            flush=True,
                        )
                    raise
                # Change data after the backup; a full restore must remove it.
                readiness = client.get("/api/v1/admin/server").json()
                assert readiness["backup"]["last_test"]["state"] == "succeeded"
                assert readiness["backup"]["last_test"]["backup_id"] == backup_id
                response = client.post("/api/v1/admin/groups", json={"name": "after-backup"})
                assert response.status_code == 201, response.text
                group_id = response.json()["id"]
                # Make the old Bearer's version equal the restored version + 1.
                # JWT key rotation must invalidate it even when counters coincide.
                assert client.post("/api/v1/web/logout-all").status_code == 200
                login()
                old_bearer = complete_email(
                    client.post(
                        "/api/v1/auth/login", json={"username": "smoke-root", "password": password}
                    ),
                    "/api/v1/auth/login",
                ).json()["access_token"]
                response = client.post(
                    f"/api/v1/backups/{backup_id}/commands",
                    headers={"Idempotency-Key": str(uuid4())},
                    json={"action": "restore", "confirmation": "RIPRISTINA " + backup_id},
                )
                assert response.status_code == 202, response.text
                coordinator.cycle()
                job = coordinator.catalog.jobs()[0]
                assert job["state"] == "succeeded", job["error"]
                assert coordinator.catalog.meta("maintenance") == ""
                # Docker Desktop's host-directory mount can briefly retain the
                # removed maintenance marker. Bound that propagation delay.
                deadline = time.monotonic() + 10
                response = client.get("/api/v1/web/session")
                while response.status_code == 503 and time.monotonic() < deadline:
                    time.sleep(0.2)
                    response = client.get("/api/v1/web/session")
                assert response.status_code == 401, (response.status_code, response.text)
                assert (
                    client.get(
                        "/api/v1/auth/me", headers={"Authorization": "Bearer " + old_bearer}
                    ).status_code
                    == 401
                )
                login()
                assert client.get("/api/v1/admin/groups/" + group_id).status_code == 404
                readiness = client.get("/api/v1/admin/server").json()
                assert {
                    item["name"]: item["fingerprint_sha256"] for item in readiness["certificates"]
                } == fingerprints
                assert readiness["backup"]["state"] == "available"
                assert coordinator.catalog.meta("maintenance") == ""
                print(
                    "Full restore passed: old session rejected, fresh login works, historical data restored.",
                    flush=True,
                )
                # Inject a failure after data and keys have already been replaced.
                from factoryvpn_common.backup_archive import BackupError

                response = client.post(
                    "/api/v1/admin/groups", json={"name": "preserve-on-rollback"}
                )
                assert response.status_code == 201
                preserved_group = response.json()["id"]

                def request_restore():
                    login()
                    response = client.post(
                        f"/api/v1/backups/{backup_id}/commands",
                        headers={"Idempotency-Key": str(uuid4())},
                        json={"action": "restore", "confirmation": "RIPRISTINA " + backup_id},
                    )
                    assert response.status_code == 202, response.text

                request_restore()
                original_apply = coordinator.apply
                injected = False

                def fail_once(*args, **kwargs):
                    nonlocal injected
                    original_apply(*args, **kwargs)
                    if not injected:
                        injected = True
                        raise BackupError("injected_restore_failure")

                coordinator.apply = fail_once
                coordinator.cycle()
                coordinator.apply = original_apply
                assert coordinator.catalog.jobs()[0]["state"] == "failed"
                assert coordinator.catalog.meta("maintenance") == ""
                login()
                assert client.get("/api/v1/admin/groups/" + preserved_group).status_code == 200
                print(
                    "Rollback passed after an injected failure following database and key replacement.",
                    flush=True,
                )

                # Simulate abrupt process loss: bypass normal exception recovery.
                class SimulatedCrash(BaseException):
                    pass

                request_restore()

                def crash_after_apply(*args, **kwargs):
                    original_apply(*args, **kwargs)
                    raise SimulatedCrash()

                coordinator.apply = crash_after_apply
                try:
                    coordinator.cycle()
                except SimulatedCrash:
                    pass
                assert coordinator.catalog.meta("maintenance")
                restarted = Coordinator(project)
                restarted.recover()
                assert restarted.catalog.meta("maintenance") == ""
                assert restarted.catalog.jobs()[0]["state"] == "failed"
                login()
                assert client.get("/api/v1/admin/groups/" + preserved_group).status_code == 200
                print("Crash recovery passed using the durable encrypted journal.", flush=True)
        finally:
            print("Removing isolated backup/recovery installation…", flush=True)
            coordinator.docker.compose("down", "--volumes", "--remove-orphans", timeout=90)


if __name__ == "__main__":
    main()
