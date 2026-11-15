"""Browser tests (or a disposable preview) inside test_with_postgres --portal.

Never uses the deployment database, credentials, SMS, or a real identity provider.
The parent owns PostgreSQL; this process owns and cleans up all three web servers.
"""

import os
from pathlib import Path
import socket
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import ipaddress
import json
import secrets

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def certificate(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "FactoryVPN browser tests")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = Path(folder) / "cert.pem", Path(folder) / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def wait_http(url, process, *, cert=None):
    context = ssl.create_default_context(cafile=str(cert)) if cert else None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Test server exited before becoming ready: {url}")
        try:
            with urllib.request.urlopen(url, timeout=2, context=context) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"Test server did not become ready: {url}")


def main():
    if (
        not os.getenv("FACTORYVPN_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL") != os.environ["FACTORYVPN_TEST_DATABASE_URL"]
    ):
        raise RuntimeError("Run only through python -m scripts.test_with_postgres --portal")
    api_port, portal_port, idp_port, mail_port, smtp_port = (port() for _ in range(5))
    api_origin = f"http://127.0.0.1:{api_port}"
    portal_origin = f"http://127.0.0.1:{portal_port}"
    idp_origin = f"https://127.0.0.1:{idp_port}"
    children = []
    with tempfile.TemporaryDirectory(prefix="factoryvpn-browser-") as temporary:
        cert, key = certificate(temporary)
        from scripts.lab_pki import initialize

        pki = Path(temporary) / "pki"
        initialize(pki, "vpn.example.test")
        from factoryvpn_common.backup_catalog import Catalog
        from factoryvpn_common.backup_archive import file_hash

        backup_control = Catalog(Path(temporary) / "backup-control")
        backup_control.initialize()
        backup_archives = Path(temporary) / "backups"
        backup_archives.mkdir()
        fixture_backup_id = "00000000-0000-4000-8000-000000000099"
        fixture_backup = backup_archives / (fixture_backup_id + ".fvbackup")
        fixture_backup.write_bytes(
            b"FactoryVPN browser download fixture; archive engine tested separately."
        )
        backup_control.add_backup(
            fixture_backup_id, fixture_backup.stat().st_size, file_hash(fixture_backup), {}
        )
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT) + os.pathsep + str(ROOT / "tests"),
            "PORTAL_PUBLIC_URL": portal_origin,
            "SSO_PUBLIC_URL": portal_origin,
            "SSO_TOKEN_KEY": Fernet.generate_key().decode(),
            "PROVISIONING_SECRET_KEY": Fernet.generate_key().decode(),
            "FACTORYVPN_PKI_ROOT": str(pki),
            "BACKUP_CONTROL_DIR": str(backup_control.root),
            "BACKUP_ARCHIVE_DIR": str(backup_archives),
            "STATUS_HTTPS_CERTIFICATE_FILE": str(cert),
            "STATUS_VPN_CERTIFICATE_FILE": str(pki / "server/server.crt"),
            "STATUS_VPN_CA_FILE": str(pki / "server/ca.crt"),
            "STATUS_PKI_ROOT": str(pki),
            "OPENVPN_SERVER_HOST": "vpn.example.test",
            "SSO_PROVIDERS": json.dumps(
                {
                    "test": {
                        "kind": "cognito",
                        "label": "Provider di test",
                        "issuer": idp_origin,
                        "client_id": "factoryvpn-browser",
                        "client_secret": "isolated-test-client-secret",
                        "logout_endpoint": idp_origin + "/logout",
                    }
                }
            ),
            "FACTORYVPN_TEST_IDP_URL": idp_origin,
            "FACTORYVPN_TEST_CA": str(cert),
            "FACTORYVPN_API_URL": api_origin,
            "FACTORYVPN_PORTAL_URL": portal_origin,
            "BROWSER": "none",
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(smtp_port),
            "SMTP_SECURITY": "plain",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM": "factoryvpn@example.test",
            "SMTP_CA_FILE": "",
            "EMAIL_CODE_KEY": secrets.token_urlsafe(48),
            "FACTORYVPN_MAIL_URL": f"http://127.0.0.1:{mail_port}",
            "FACTORYVPN_TEST_GATEWAY_KEYS": json.dumps(
                {
                    f"00000000-0000-4000-8000-{number:012d}": secrets.token_urlsafe(32)
                    for number in (20, 21, 22, 23)
                }
            ),
        }
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
        )
        subprocess.run([sys.executable, "-m", "portal_seed"], cwd=ROOT, env=env, check=True)
        log_name = "preview-servers.log" if "--preview" in sys.argv[1:] else "servers.log"
        log_path = ROOT / "portal" / "test-results" / log_name
        log_path.parent.mkdir(exist_ok=True)
        with log_path.open("w") as log:

            def start(command, cwd=ROOT):
                proc = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                children.append(proc)
                return proc

            try:
                # UI tests exercise queue submission only; no deployment is controlled here.
                start(
                    [
                        sys.executable,
                        "-c",
                        "import os,time; from factoryvpn_common.backup_catalog import Catalog,now; c=Catalog(os.environ['BACKUP_CONTROL_DIR']);\nwhile True: c.meta('heartbeat',now()); time.sleep(5)",
                    ]
                )
                mail = start(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "portal_mail:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(mail_port),
                        "--no-access-log",
                    ]
                )
                wait_http(env["FACTORYVPN_MAIL_URL"] + "/health", mail)
                idp = start(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "portal_idp:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(idp_port),
                        "--ssl-certfile",
                        str(cert),
                        "--ssl-keyfile",
                        str(key),
                        "--no-access-log",
                    ]
                )
                wait_http(idp_origin + "/health", idp, cert=cert)
                backend = start(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "portal_backend:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(api_port),
                        "--no-access-log",
                    ]
                )
                wait_http(api_origin + "/health/ready", backend)
                start([sys.executable, "-m", "scripts.provisioning_worker"])
                frontend = start(
                    ["npm", "run", "dev", "--", "--port", str(portal_port)], cwd=ROOT / "portal"
                )
                wait_http(portal_origin + "/app/", frontend)
                print(f"Isolated portal ready: {portal_origin}/app/", flush=True)
                if "--preview" in sys.argv[1:]:
                    print(
                        "Preview users: root / delegate / technician. Test password: factoryvpn-browser-tests. Ctrl+C removes the preview.",
                        flush=True,
                    )
                    while all(child.poll() is None for child in children):
                        time.sleep(1)
                    return 1
                return subprocess.run(
                    ["npm", "run", "test:e2e", "--", *sys.argv[1:]], cwd=ROOT / "portal", env=env
                ).returncode
            finally:
                for child in reversed(children):
                    if child.poll() is not None:
                        continue
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
