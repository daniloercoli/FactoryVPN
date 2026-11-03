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
from factoryvpn_common.pki import issue_profile, publish_crl  # noqa: E402
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
        initialize(root, "127.0.0.1")
        identifier = uuid4()
        old, old_profile = issue_profile(root, "technician", identifier, "127.0.0.1", 1194)
        new, new_profile = issue_profile(root, "technician", identifier, "127.0.0.1", 1194)
        old_path, new_path = root / "old.ovpn", root / "new.ovpn"
        old_path.write_bytes(old_profile)
        new_path.write_bytes(new_profile)
        for path in (old_path, new_path):
            path.chmod(0o600)
        socket = str(root / "management.sock")
        management = ManagementClient(socket)
        cn = f"technician-{identifier}"
        config = (
            Path("/etc/openvpn/server.conf")
            .read_text()
            .replace("/etc/openvpn/pki", str(root / "server"))
            .replace("/run/openvpn/management.sock", socket)
            .replace("dev tun0", "dev factoryvpn0\ndev-type tun")
        )
        server_config = root / "server.conf"
        server_config.write_text(config)
        server_log = root / "server.log"
        env = {
            **os.environ,
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
                    "--setenv",
                    "API_URL",
                    env["API_URL"],
                    "--log",
                    str(server_log),
                ],
                env=env,
            )
            processes.append(server)
            wait_for(lambda: Path(socket).exists())
            permitted.add(str(old.serial_number))
            client = start(old_path, "old.log")
            wait_for(lambda: management.clients().get(cn) == "10.100.128.2")
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
                root, [SimpleNamespace(serial=str(old.serial_number), revoked_at=utc_now())]
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
            print(
                "OpenVPN: serial binding, denied old profile, CRL reload, disconnect and replacement passed."
            )
        except Exception:
            # Logs contain public certificate identities, never private profile material.
            print(
                server_log.read_text()[-8000:] if server_log.exists() else "Server log unavailable"
            )
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    stop(process)
            http.shutdown()


if __name__ == "__main__":
    main()
