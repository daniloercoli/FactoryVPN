"""OpenVPN hooks use real argv/environment and fail closed if authorization fails."""

import os
from pathlib import Path
import sys

# OpenVPN strips PYTHONPATH when invoking file-based hooks.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from factoryvpn_common.certificates import validate_crls  # noqa: E402


def api_call(action, common_name, address=None, certificate_serial=None):
    key = os.environ.get("WORKER_API_KEY", "")
    if not key:
        key = Path("/run/openvpn/worker-key").read_text().strip()
    if not key:
        raise RuntimeError("WORKER_API_KEY is required")
    response = httpx.post(
        os.environ.get("API_URL", "http://api:8000").rstrip("/") + "/api/v1/internal/vpn/" + action,
        headers={"X-Worker-Key": key},
        json={
            "common_name": common_name,
            "address": address,
            **({"certificate_serial": certificate_serial} if action == "authorize" else {}),
        },
        timeout=8,
    )
    response.raise_for_status()
    return response.json()


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args[0] == "connect" and len(args) == 2:
        validate_crls(Path(os.environ.get("FACTORYVPN_PKI_ROOT", "/pki")))
        result = api_call(
            "authorize",
            os.environ.get("common_name", ""),
            certificate_serial=os.environ.get("tls_serial_0", ""),
        )
        Path(args[1]).write_text(result["config"], encoding="utf-8")
    elif args[0] == "learn" and len(args) >= 3:
        operation, address = args[1:3]
        if operation == "delete":
            return
        if operation not in ("add", "update") or len(args) != 4:
            raise ValueError("Invalid learn-address arguments")
        api_call("learn", args[3], address)
    else:
        raise ValueError("Invalid OpenVPN hook invocation")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"VPN authorization hook failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
