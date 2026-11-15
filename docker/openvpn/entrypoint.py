"""Install the default-deny VPN guard before accepting any client."""

from importlib import import_module
import os
from pathlib import Path
from factoryvpn_common.certificates import validate_crls
from factoryvpn_common.timing import env_seconds

# Il keepalive rileva l'assenza del peer; non rinnova il lease del firewall.
ping = env_seconds("OPENVPN_PING_SECONDS", 10, 1, 60)
restart = env_seconds("OPENVPN_RESTART_SECONDS", 300, 30, 900)
if restart < ping * 2:
    raise ValueError("OPENVPN_RESTART_SECONDS deve essere almeno il doppio di OPENVPN_PING_SECONDS")

validate_crls(Path("/pki"))

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
        "--keepalive",
        str(ping),
        str(restart),
        "--setenv",
        "API_URL",
        os.environ.get("API_URL", "http://api:8000"),
    ],
)
