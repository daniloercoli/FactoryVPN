"""Tempi configurabili: nessun limite nascosto a trenta secondi."""

from datetime import timedelta
import io
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from dotenv import dotenv_values

from api.config import Settings, settings
from api.services import lifecycle
from api.time_utils import utc_now
from factoryvpn_common.network import lease_ttl
from scripts.provisioning_worker import gateway_bundle
from test_network_contract import spec


@pytest.mark.parametrize("seconds", [30, 300, 600, 900])
def test_lease_respects_configuration_and_earlier_login_deadline(monkeypatch, seconds):
    now = utc_now()
    monkeypatch.setattr(lifecycle, "utc_now", lambda: now)
    monkeypatch.setattr(settings, "VPN_LEASE_SECONDS", seconds)
    session = SimpleNamespace(expires_at=now + timedelta(hours=3))
    assert lease_ttl(lifecycle.lease_seconds(session)) == seconds
    for field in ("_sso_lease_deadline", "_web_lease_deadline"):
        setattr(session, field, now + timedelta(seconds=15))
        assert lifecycle.lease_seconds(session) == 15
        setattr(session, field, now - timedelta(seconds=1))
        assert lifecycle.lease_seconds(session) == 0
        delattr(session, field)
    session.gateway_ready_at = now - timedelta(seconds=seconds - 1)
    assert lifecycle.gateway_ready(session)
    session.gateway_ready_at = now - timedelta(seconds=seconds)
    assert not lifecycle.gateway_ready(session)


@pytest.mark.parametrize("value", [0, 901, "300"])
def test_invalid_wire_lease_rejected(value):
    with pytest.raises(ValueError):
        lease_ttl(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("VPN_LEASE_SECONDS", 29),
        ("VPN_LEASE_SECONDS", 901),
        ("VPN_SESSION_MINUTES", 0),
        ("VPN_SESSION_MINUTES", 721),
        ("CONTROL_API_TIMEOUT_SECONDS", 0),
        ("CONTROL_API_TIMEOUT_SECONDS", 61),
    ],
)
def test_invalid_server_timing_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_long_lease_reaches_both_firewalls(load_script):
    network = spec()
    for folder, module, manager in (
        ("gateway-agent", "network", "NetworkManager"),
        ("session-worker", "firewall_manager", "FirewallManager"),
    ):
        runner = Mock(
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="FIB table does not exist")
        )
        instance = getattr(load_script(folder, module), manager)(runner=runner)
        if folder == "gateway-agent":
            instance.configure_session(network, lease_seconds=300)
        else:
            instance.ensure(network, 300)
        scripts = [c.kwargs["input"] for c in runner.call_args_list if "input" in c.kwargs]
        assert any("timeout 300s" in script for script in scripts)


def test_gateway_bundle_carries_configured_timing(monkeypatch):
    monkeypatch.setattr(settings, "PORTAL_PUBLIC_URL", "https://vpn.example.test")
    values = {
        "GATEWAY_POLL_INTERVAL": 7,
        "GATEWAY_WIFI_CHECK_INTERVAL": 45,
        "CONTROL_API_TIMEOUT_SECONDS": 20,
        "GATEWAY_CONNECT_TIMEOUT_SECONDS": 90,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    job = SimpleNamespace(
        peer_id=spec().session_id,
        parameters={
            "activation_mode": "wifi",
            "phone_number": "",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
    )
    with zipfile.ZipFile(
        io.BytesIO(gateway_bundle(job, b"profile", "api-key", "sms-key"))
    ) as bundle:
        config = dotenv_values(stream=io.StringIO(bundle.read("gateway.env").decode()))
    assert {name: int(config[name]) for name in values} == values


@pytest.mark.parametrize("restart", [300, 600, 901])
def test_openvpn_entrypoint_uses_env_keepalive_before_starting(monkeypatch, restart):
    monkeypatch.setattr("factoryvpn_common.certificates.validate_crls", lambda root: None)
    import os

    monkeypatch.setenv("WORKER_API_KEY", "k" * 40)
    monkeypatch.setenv("OPENVPN_PING_SECONDS", "10")
    monkeypatch.setenv("OPENVPN_RESTART_SECONDS", str(restart))
    monkeypatch.setattr(Path, "mkdir", Mock())
    monkeypatch.setattr(Path, "write_text", Mock())
    monkeypatch.setattr(Path, "chmod", Mock())
    monkeypatch.setattr(os, "chmod", Mock())
    execute = Mock()
    monkeypatch.setattr(os, "execvp", execute)
    import importlib

    monkeypatch.setattr(
        importlib, "import_module", lambda name: SimpleNamespace(FirewallManager=Mock)
    )
    if restart > 900:
        with pytest.raises(ValueError):
            runpy.run_path("docker/openvpn/entrypoint.py")
        execute.assert_not_called()
    else:
        runpy.run_path("docker/openvpn/entrypoint.py")
        args = execute.call_args.args[1]
        assert args[args.index("--keepalive") + 1 : args.index("--keepalive") + 3] == [
            "10",
            str(restart),
        ]
