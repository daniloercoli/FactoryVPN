"""Install the default-deny VPN guard before accepting any client."""

from importlib import import_module
import os
from pathlib import Path

key = os.environ.get("WORKER_API_KEY", "")
if len(key) < 32:
    raise SystemExit("WORKER_API_KEY is required")
run = Path("/run/openvpn")
run.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(run, 0o700)
credential = run / "worker-key"
credential.write_text(key)
credential.chmod(0o600)
# OpenVPN scripts receive a curated environment. Keep credentials in the private
# runtime directory; pass only the nonsensitive API URL with --setenv.
import_module("session-worker.firewall_manager").FirewallManager().create_table()
os.execvp(
    "openvpn",
    [
        "openvpn",
        "--config",
        "/etc/openvpn/server.conf",
        "--setenv",
        "API_URL",
        os.environ.get("API_URL", "http://api:8000"),
    ],
)
