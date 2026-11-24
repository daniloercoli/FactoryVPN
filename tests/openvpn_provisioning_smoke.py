"""Real OpenVPN handshake/CRL/disconnect checks in the disposable network image.

The HTTP fixture stands in for an authorized session; PostgreSQL tests exercise
the real API's ownership, serial registry and session decisions separately.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parents[1]))
from scripts.bootstrap import initialize  # noqa: E402
from scripts.pki import root_init, prepare, sign_issuer, import_issuer  # noqa: E402
from factoryvpn_common.certificates import fingerprint, new_key, name, PEM  # noqa: E402
from factoryvpn_common.pki import issue_profile, issue_csr, publish_crl  # noqa: E402
from factoryvpn_common.openvpn import ManagementClient  # noqa: E402
from api.time_utils import utc_now  # noqa: E402


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, RuntimeError):
            pass
        time.sleep(0.1)
    raise AssertionError("OpenVPN condition did not complete before timeout")


def main():
    if os.environ.get("FACTORYVPN_NETWORK_TEST") != "1":
        raise SystemExit("Run only in a disposable network test container")
    permitted, observed = set(), []

    class Authorization(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            serial = body.get("certificate_serial")
            if self.path.endswith("/authorize"):
                observed.append(serial)
                allowed = serial in permitted
            else:
                allowed = True  # Source address validation is exercised by API tests.
            self.send_response(200 if allowed else 403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"config": "ifconfig-push 10.100.128.2 255.255.0.0\n"}).encode()
            )

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Authorization)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    processes = []
    with tempfile.TemporaryDirectory(prefix="factoryvpn-openvpn-") as temporary:
        root = Path(temporary)
        offline = root / "offline-test-only"
        password = b"isolated-test-passphrase"
        root_cert = root_init(offline, password)
        request = prepare(root)
        import_issuer(
            root, sign_issuer(offline, password, request.read_bytes()), fingerprint(root_cert)
        )
        initialize(root, "127.0.0.1")
        identifier = uuid4()
        old, old_profile = issue_profile(root, "gateway", identifier, "127.0.0.1", 1194)
        request = prepare(root)
        import_issuer(
            root, sign_issuer(offline, password, request.read_bytes()), fingerprint(root_cert)
        )
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from importlib import import_module

        device_key = new_key()
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(name("untrusted CSR name"))
            .sign(device_key, hashes.SHA256())
        )
        new, issuer_id, chain, trust, generation = issue_csr(
            root, "gateway", identifier, csr.public_bytes(PEM)
        )
        new_profile = (
            import_module("gateway-agent.certificate_renewal")
            .validated_profile(
                old_profile.decode(),
                dict(
                    certificate_pem=new.public_bytes(PEM).decode(),
                    issuer_pem=chain,
                    trust_pem=trust,
                    issuer_id=issuer_id,
                    certificate_sha256=fingerprint(new),
                    trust_generation=generation,
                ),
                device_key,
                identifier,
            )
            .encode()
        )
        publish_crl(root, [])
        old_path, new_path = root / "old.ovpn", root / "new.ovpn"
        old_path.write_bytes(old_profile)
        new_path.write_bytes(new_profile)
        technician_id = uuid4()
        technician_cert, technician_profile = issue_profile(
            root, "technician", technician_id, "127.0.0.1", 1194
        )
        technician_path = root / "technician.ovpn"
        technician_path.write_bytes(technician_profile)
        for path in (old_path, new_path, technician_path):
            path.chmod(0o600)
        socket = str(root / "management.sock")
        management = ManagementClient(socket)
        cn = f"gateway-{identifier}"
        config = (
            (
                Path("/etc/openvpn/server.conf")
                if Path("/etc/openvpn/server.conf").exists()
                else Path(__file__).parents[1] / "docker/openvpn/server.conf"
            )
            .read_text()
            .replace("/etc/openvpn/pki", str(root / "server"))
            .replace("/pki/", str(root) + "/")
            .replace("/run/openvpn/management.sock", socket)
            .replace("dev tun0", "dev factoryvpn0\ndev-type tun")
        )
        server_config = root / "server.conf"
        server_config.write_text(config)
        server_log = root / "server.log"
        env = {
            **os.environ,
            "FACTORYVPN_PKI_ROOT": str(root),
            "API_URL": f"http://127.0.0.1:{http.server_port}",
            "WORKER_API_KEY": "isolated-openvpn-test-key",
        }

        runtime = Path("/run/openvpn")
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        (runtime / "worker-key").write_text(env["WORKER_API_KEY"])
        (runtime / "worker-key").chmod(0o600)

        def start(profile, name):
            process = subprocess.Popen(
                [
                    "openvpn",
                    "--config",
                    str(profile),
                    "--verb",
                    "4",
                    "--route-noexec",
                    "--ifconfig-noexec",
                    "--log",
                    str(root / name),
                ],
                env=env,
            )
            processes.append(process)
            return process

        def stop(process):
            process.terminate()
            process.wait(timeout=5)

        try:
            server = subprocess.Popen(
                [
                    "openvpn",
                    "--config",
                    str(server_config),
                    "--keepalive",
                    "10",
                    "300",
                    "--setenv",
                    "API_URL",
                    env["API_URL"],
                    "--setenv",
                    "FACTORYVPN_PKI_ROOT",
                    str(root),
                    "--log",
                    str(server_log),
                ],
                env=env,
            )
            processes.append(server)
            wait_for(lambda: Path(socket).exists())
            # SIGTERM must notify the server before its 600-second keepalive timeout.
            technician_cn = f"technician-{technician_id}"
            permitted.add(str(technician_cert.serial_number))
            technician = start(technician_path, "technician.log")
            wait_for(lambda: management.clients().get(technician_cn) == "10.100.128.2")
            wait_for(
                lambda: "Initialization Sequence Completed" in (root / "technician.log").read_text()
            )
            disconnected_at = time.monotonic()
            stop(technician)
            assert technician.returncode == 0
            wait_for(lambda: technician_cn not in management.clients(), timeout=5)
            print(
                f"Technician orderly disconnect observed in {time.monotonic() - disconnected_at:.2f} s"
            )
            permitted.clear()
            permitted.add(str(old.serial_number))
            client = start(old_path, "old.log")
            wait_for(lambda: management.clients().get(cn) == "10.100.128.2")
            # Verifica sul client reale i tempi distribuiti dal server.
            wait_for(lambda: "ping-restart 300" in (root / "old.log").read_text())
            assert "ping 10" in (root / "old.log").read_text()
            assert str(old.serial_number) in observed
            # Revoke authorization first, then confirm actual disconnect via management.
            permitted.clear()
            assert management.kill(cn)
            wait_for(lambda: cn not in management.clients())
            stop(client)
            count = len(observed)
            rejected = start(old_path, "rejected.log")
            wait_for(lambda: len(observed) > count)
            assert management.clients().get(cn) != "10.100.128.2"
            stop(rejected)
            management.kill(cn)
            wait_for(lambda: cn not in management.clients())
            # Publish a real CRL and prove TLS rejects the old certificate before the hook.
            publish_crl(
                root,
                [
                    SimpleNamespace(
                        serial=str(old.serial_number),
                        revoked_at=utc_now(),
                        issuer_id=fingerprint(
                            x509.load_pem_x509_certificate((root / "server/chain.pem").read_bytes())
                        ),
                    )
                ],
            )
            permitted.add(str(old.serial_number))
            count = len(observed)
            revoked = start(old_path, "revoked.log")
            wait_for(lambda: "certificate revoked" in server_log.read_text())
            assert len(observed) == count and management.clients().get(cn) != "10.100.128.2"
            stop(revoked)
            permitted.clear()
            permitted.add(str(new.serial_number))
            replacement = start(new_path, "new.log")
            wait_for(lambda: management.clients().get(cn) == "10.100.128.2")
            assert str(new.serial_number) in observed
            stop(replacement)
            management.kill(cn)
            wait_for(lambda: cn not in management.clients())
            # OpenVPN may retain loaded CRLs when a file disappears. The hook
            # must independently deny this connection before calling the API.
            crl_path = root / "crls/bundle.pem"
            bundle = crl_path.read_bytes()
            crl_path.unlink()
            count = len(observed)
            missing = start(new_path, "missing-crl.log")
            wait_for(
                lambda: "VPN authorization hook failed: FileNotFoundError" in server_log.read_text()
            )
            assert len(observed) == count and management.clients().get(cn) != "10.100.128.2"
            stop(missing)
            management.kill(cn)
            wait_for(lambda: cn not in management.clients())
            from factoryvpn_common.certificates import (
                load_issuer,
                now,
                parse_crls,
                write_private,
                validate_crls,
            )
            from datetime import timedelta

            key, issuer = load_issuer(root)
            retained_crls = [crl for crl in parse_crls(bundle) if crl.issuer != issuer.subject]
            expired = (
                x509.CertificateRevocationListBuilder()
                .issuer_name(issuer.subject)
                .last_update(now() - timedelta(days=8))
                .next_update(now() - timedelta(days=1))
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False
                )
                .add_extension(x509.CRLNumber(10), False)
                .sign(key, hashes.SHA256())
            )
            write_private(
                crl_path,
                b"".join(crl.public_bytes(PEM) for crl in retained_crls)
                + expired.public_bytes(PEM),
            )
            try:
                validate_crls(root)
            except ValueError as exc:
                assert "expired" in str(exc)
            else:
                raise AssertionError("Expired CRL accepted")
            expired_client = start(new_path, "expired-crl.log")
            wait_for(
                lambda: "CRL has expired" in server_log.read_text()
                or "VPN authorization hook failed: ValueError" in server_log.read_text()
            )
            assert len(observed) == count
            stop(expired_client)
            crl_path.write_bytes(bundle)
            print(
                "OpenVPN: technician exit notification, keepalive, serial binding, "
                "revoked/missing/expired CRLs, disconnect and replacement passed."
            )
        except Exception:
            # Logs contain public certificate identities, never private profile material.
            print(
                "\n".join(
                    line
                    for line in server_log.read_text().splitlines()
                    if "MANAGEMENT:" not in line
                )[-12000:]
                if server_log.exists()
                else "Server log unavailable"
            )
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    stop(process)
            http.shutdown()


if __name__ == "__main__":
    main()
