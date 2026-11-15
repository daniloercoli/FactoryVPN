"""Run pytest with a disposable, local Docker PostgreSQL database.

Usage: python -m scripts.test_with_postgres [pytest arguments]
The container uses a random loopback port and in-memory storage. Only the
container created by this invocation is removed, including when tests fail.
"""

import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "postgres:15-alpine"  # Match the production Compose major version.


def docker(*arguments, **kwargs):
    return subprocess.run(["docker", *arguments], check=True, text=True, timeout=30, **kwargs)


def main() -> int:
    name = "factoryvpn-test-" + uuid4().hex[:12]
    password = secrets.token_hex(24)
    docker_env = {**os.environ, "POSTGRES_PASSWORD": password}
    container_id = None
    try:
        # Fail promptly if Docker is unavailable instead of silently skipping DB tests.
        docker("info", "--format", "{{.ServerVersion}}", stdout=subprocess.DEVNULL)
        print(f"Creating {name} with {IMAGE}…", flush=True)
        result = subprocess.run(
            [
                "docker",
                "create",
                "--name",
                name,
                "--rm",
                "--label",
                "eu.ercoliconsulting.factoryvpn.purpose=tests",
                "--publish",
                "127.0.0.1::5432",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,noexec,nosuid,size=512m",
                "--env",
                "POSTGRES_USER=factoryvpn_test",
                "--env",
                "POSTGRES_DB=factoryvpn_test",
                "--env",
                "POSTGRES_PASSWORD",
                IMAGE,
            ],
            env=docker_env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            timeout=180,
        )
        container_id = result.stdout.strip()
        docker("start", container_id, stdout=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while True:
            # TCP excludes the temporary Unix-only bootstrap PostgreSQL server.
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_id,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "factoryvpn_test",
                    "-d",
                    "factoryvpn_test",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL did not become ready within 60 seconds")
            time.sleep(1)
        binding = docker("port", container_id, "5432/tcp", stdout=subprocess.PIPE).stdout.strip()
        if not binding.startswith("127.0.0.1:") or "\n" in binding:
            raise RuntimeError("Expected one local Docker port binding")
        url = f"postgresql+asyncpg://factoryvpn_test:{password}@{binding}/factoryvpn_test"
        test_env = {
            **os.environ,
            "FACTORYVPN_TEST_DATABASE_URL": url,
            "DATABASE_URL": url,
            "ENVIRONMENT": "development",
            "JWT_SECRET": secrets.token_hex(32),
            "WORKER_API_KEY": secrets.token_hex(32),
            "PROVISIONING_SECRET_KEY": "",
            "FACTORY_STATIONS": "{}",
            "FACTORY_REQUESTS_PER_MINUTE": "60",
            "ENROLLMENT_REQUESTS_PER_MINUTE": "30",
            "ENROLLMENT_PACKAGES_PER_HOUR": "6",
            "SSO_PROVIDERS": "{}",
            "PORTAL_PUBLIC_URL": "",
            "PORTAL_SESSION_MINUTES": "720",
            "VPN_SESSION_MINUTES": "180",
            "VPN_LEASE_SECONDS": "300",
            "GATEWAY_POLL_INTERVAL": "5",
            "GATEWAY_WIFI_CHECK_INTERVAL": "30",
            "CONTROL_API_TIMEOUT_SECONDS": "10",
            "GATEWAY_CONNECT_TIMEOUT_SECONDS": "30",
            "SSO_PUBLIC_URL": "",
            "SSO_TOKEN_KEY": "",
            "SSO_CLIENT_REDIRECT_URIS": '["http://127.0.0.1:8765/callback"]',
            "SSO_SESSION_MINUTES": "720",
            "SSO_CHECK_INTERVAL_SECONDS": "60",
            "SSO_MAX_STALENESS_SECONDS": "120",
            "SMS_PROVIDER": "generic",
            "SMS_PROVIDER_URL": "",
            "SMS_PROVIDER_API_KEY": "",
            "TWILIO_ACCOUNT_SID": "",
            "TWILIO_AUTH_TOKEN": "",
            "TWILIO_FROM": "",
        }
        arguments = sys.argv[1:] or ["-q"]
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        portal = arguments[:1] == ["--portal"]
        module = "scripts.portal_e2e" if portal else "pytest"
        if portal:
            arguments = arguments[1:]
        print(f"PostgreSQL ready at {binding}; running {module}.", flush=True)
        return subprocess.run(
            [sys.executable, "-m", module, *arguments], cwd=ROOT, env=test_env
        ).returncode
    finally:
        if container_id:
            print(f"Removing disposable PostgreSQL container {name}…", flush=True)
            docker("rm", "--force", container_id, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"PostgreSQL test run failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
