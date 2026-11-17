"""One provisioned Wi-Fi gateway, using the real agent inside its Docker network."""

import argparse
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import threading

from dotenv import dotenv_values

STATE_FILE = Path("/run/factoryvpn-gateway.json")
DEMO_RESPONSE = b"FactoryVPN gateway simulator\n"


def gateway_environment(directory):
    """Keep the portal package authoritative; only local HTTPS trust is optional."""
    directory = Path(directory).resolve()
    package = directory / "gateway.env"
    profile = directory / "gateway.ovpn"
    if not package.is_file() or not profile.is_file():
        raise ValueError("Provide gateway.env and gateway.ovpn in the configuration directory")
    if (directory / "enrollment.json").exists():
        raise ValueError("Use a manually provisioned Wi-Fi package without enrollment.json")
    enrollment = import_module("gateway-agent.enrollment")
    try:
        values = dotenv_values(package, interpolate=False)
    except (OSError, UnicodeError):
        raise ValueError("Cannot read gateway.env") from None
    if any(
        key not in enrollment.PACKAGE_SETTINGS or value is None for key, value in values.items()
    ):
        raise ValueError("gateway.env contains unsupported or missing settings")
    if values.get("GATEWAY_ACTIVATION_MODE", "sms").lower() != "wifi":
        raise ValueError("The standalone simulator requires a Wi-Fi/polling gateway package")
    try:
        enrollment.origin(values.get("SERVER_URL", ""))
    except (ValueError, TypeError):
        raise ValueError(
            "gateway.env requires an HTTPS server origin without credentials or path"
        ) from None
    environment = {
        key: value for key, value in os.environ.items() if key not in enrollment.PACKAGE_SETTINGS
    }
    environment.update(values)
    # The profile and renewal journal stay together in the writable mounted directory.
    environment["OPENVPN_CONFIG"] = str(profile)
    environment["FACTORYVPN_ENROLLMENT_FILE"] = str(directory / "enrollment.json")
    environment.pop("SSL_CERT_FILE", None)
    environment.pop("SSL_CERT_DIR", None)
    certificate = directory / "https-ca.pem"
    if certificate.exists():
        try:
            ssl.create_default_context(cafile=str(certificate))
        except (OSError, ValueError):
            raise ValueError("Cannot load https-ca.pem as an HTTPS CA bundle") from None
        environment["SSL_CERT_FILE"] = str(certificate)
    return environment


class DemoHandler(BaseHTTPRequestHandler):
    """A fixed response on the gateway, with no filesystem or configuration access."""

    server_version = "FactoryVPN"
    sys_version = ""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(DEMO_RESPONSE)))
        self.end_headers()
        self.wfile.write(DEMO_RESPONSE)

    def log_message(self, *_args):
        pass


def healthcheck(state_file=STATE_FILE):
    """Check process/HTTP liveness; this does not assert registration or VPN readiness."""
    try:
        state = json.loads(Path(state_file).read_text())
        for key in ("runner_pid", "agent_pid"):
            pid = state[key]
            if not isinstance(pid, int) or pid <= 0:
                return False
            os.kill(pid, 0)
        if state["http_port"]:
            connection = HTTPConnection("127.0.0.1", state["http_port"], timeout=2)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                return (
                    response.status == 200
                    and response.read(len(DEMO_RESPONSE) + 1) == DEMO_RESPONSE
                )
            finally:
                connection.close()
        return True
    except (OSError, ValueError, KeyError, TypeError, HTTPException):
        return False


def serve(directory, http_port=8080, state_file=STATE_FILE):
    environment = gateway_environment(directory)
    state_file = Path(state_file)
    state_file.unlink(missing_ok=True)
    server, thread, child = None, None, None
    previous_handlers = {}
    try:
        if http_port:
            server = ThreadingHTTPServer(("0.0.0.0", http_port), DemoHandler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
        child = subprocess.Popen([sys.executable, "-m", "gateway-agent.main"], env=environment)

        def forward(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, forward)
        state_file.write_text(
            json.dumps(
                {"runner_pid": os.getpid(), "agent_pid": child.pid, "http_port": http_port}
            )
        )
        print(
            "FactoryVPN standalone gateway started; health checks report liveness only.",
            flush=True,
        )
        while child.poll() is None:
            if thread is not None and not thread.is_alive():
                raise RuntimeError("Gateway demo HTTP service stopped unexpectedly")
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        state_file.unlink(missing_ok=True)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "healthcheck"))
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/factoryvpn"))
    parser.add_argument(
        "--http-port",
        type=int,
        default=os.environ.get("FACTORYVPN_SIMULATOR_HTTP_PORT", "8080"),
        help="Demo HTTP port; 0 disables it",
    )
    args = parser.parse_args()
    if not 0 <= args.http_port <= 65535:
        parser.error("--http-port must be between 0 and 65535")
    if args.action == "healthcheck":
        return 0 if healthcheck() else 1
    try:
        return serve(args.config_dir, args.http_port)
    except (OSError, ValueError, RuntimeError) as exc:
        # Do not print exception payloads containing credentials or package values.
        print(
            "FactoryVPN standalone gateway could not start or stopped unexpectedly.",
            file=sys.stderr,
        )
        if isinstance(exc, (ValueError, RuntimeError)):
            print(str(exc), file=sys.stderr)
        return 78


if __name__ == "__main__":
    sys.exit(main())
