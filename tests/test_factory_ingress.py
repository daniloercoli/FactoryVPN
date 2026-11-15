"""Real Nginx ingress checks, opt in with FACTORYVPN_INGRESS_TEST=1 (Docker required)."""

import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from scripts.bootstrap import initialize_https


pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORYVPN_INGRESS_TEST") != "1", reason="isolated Nginx Docker check"
)


def test_factory_allowlist_uses_tcp_peer_and_leaves_gateway_bootstrap_public(tmp_path):
    root = Path(__file__).resolve().parents[1]
    initialize_https(tmp_path, "localhost")
    config = (root / "docker/nginx/nginx.conf").read_text()
    config = config.replace("server api:8000;", "server 127.0.0.1:8000;")
    # Un upstream fittizio prova che la richiesta ha superato il confine Nginx.
    end = config.rfind("}")
    config = config[:end] + "server { listen 8000; return 204; }\n" + config[end:]
    (tmp_path / "nginx.conf").write_text(config)
    allow = tmp_path / "factory-allow.conf"
    allow.write_text("deny all;\n")
    name = "factoryvpn-ingress-test-" + uuid4().hex[:10]

    def docker(*args, check=True):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=check, timeout=30
        )

    def request(path, *headers):
        result = docker(
            "exec",
            name,
            "wget",
            "-S",
            "-O",
            "/dev/null",
            "--no-check-certificate",
            *[part for value in headers for part in ("--header", value)],
            "https://127.0.0.1" + path,
            check=False,
        )
        return result.stderr

    try:
        docker(
            "run",
            "--rm",
            "-d",
            "--network",
            "none",
            "--name",
            name,
            "--mount",
            f"type=bind,src={tmp_path / 'nginx.conf'},dst=/etc/nginx/nginx.conf,readonly",
            "--mount",
            f"type=bind,src={allow},dst=/etc/nginx/factory-allow.conf,readonly",
            "--mount",
            f"type=bind,src={tmp_path / 'https'},dst=/etc/nginx/ssl,readonly",
            "nginx:stable-alpine",
        )
        deadline = time.monotonic() + 10
        while "204" not in request("/api/v1/gateway-enrollment/example"):
            assert time.monotonic() < deadline, "Nginx failed to start"
            time.sleep(0.1)
        for path in (
            "/api/v1/factory/gateways",
            "/api/v1/factory%2fgateways",
            "/api/v1//factory/gateways",
        ):
            response = request(path, "X-Forwarded-For: 192.0.2.9", "X-Real-IP: 192.0.2.9")
            assert "403 Forbidden" in response, response
        # Una allowlist con un indirizzo contraffatto negli header non basta.
        allow.write_text("allow 192.0.2.9;\ndeny all;\n")
        docker("exec", name, "nginx", "-s", "reload")
        assert "403 Forbidden" in request("/api/v1/factory/gateways", "X-Real-IP: 192.0.2.9")
        allow.write_text("allow 127.0.0.1;\ndeny all;\n")
        docker("exec", name, "nginx", "-s", "reload")
        deadline = time.monotonic() + 10
        while "204" not in request("/api/v1/factory/gateways"):
            assert time.monotonic() < deadline, "Trusted TCP peer was not allowed"
            time.sleep(0.1)
    finally:
        docker("rm", "-f", name, check=False)
