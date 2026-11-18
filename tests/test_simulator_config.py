"""The two labs must keep state and transport configuration independent."""

import json
from types import SimpleNamespace

from dotenv import dotenv_values
import pytest

from scripts import local_lab


@pytest.mark.parametrize("simulator", [False, True])
def test_fresh_lab_configuration_is_isolated(tmp_path, monkeypatch, simulator):
    root = tmp_path / "lab"
    monkeypatch.setattr(local_lab, "port", lambda: 54321)
    monkeypatch.setenv("SMS_PROVIDER_URL", "https://external.example.test/send")
    monkeypatch.setenv("SMS_PROVIDER_API_KEY", "external-key")

    def compose_config(args, *, env, **kwargs):
        # The real Compose interpolation receives only local provider settings.
        assert env["SMS_PROVIDER_URL"] == ("http://test-sms:8080/send" if simulator else "")
        assert env["SMS_PROVIDER_API_KEY"] != "external-key"
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "services": {
                        "nginx": {},
                        "openvpn": {},
                        "api": {
                            "environment": {
                                "JWT_SECRET": env["JWT_SECRET"],
                                "WORKER_API_KEY": env["WORKER_API_KEY"],
                                "VPN_SESSION_MINUTES": "180",
                                "DATABASE_URL": "postgresql+asyncpg://isolated-db",
                            },
                            "volumes": [
                                {"source": "old", "target": "/backups"},
                                {"source": "old", "target": "/backup-control"},
                            ]
                        },
                    }
                }
            )
        )

    monkeypatch.setattr(local_lab.subprocess, "run", compose_config)
    lab = local_lab.initialize_lab(root, simulator=simulator)
    config = json.loads((root / "docker-compose.yml").read_text())["services"]
    assert lab.root == root
    assert lab.activation_mode(1) == ("sms" if simulator else "wifi")
    assert lab.activation_mode(2) == "wifi"
    assert config["peers"]["environment"] == {"FACTORYVPN_LOCAL_LAB": "1"}
    assert str(root / "peers") + ":/lab" in config["peers"]["volumes"]
    assert config["openvpn"]["ports"] == []
    assert config["nginx"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert all(v["source"].startswith(str(root)) for v in config["api"]["volumes"])
    assert config["api"]["environment"] == {
        "JWT_SECRET": "${JWT_SECRET}",
        "WORKER_API_KEY": "${WORKER_API_KEY}",
        "VPN_SESSION_MINUTES": "${VPN_SESSION_MINUTES}",
        "DATABASE_URL": "postgresql+asyncpg://isolated-db",
    }
    assert (root / "migrations/versions/0001_initial.py").read_bytes() == (
        local_lab.ROOT / "migrations/versions/0001_initial.py"
    ).read_bytes()
    assert ("test-sms" in config) is simulator
    if simulator:
        provider = config["test-sms"]
        assert "ports" not in provider
        assert provider["volumes"] == [str(root / "sms") + ":/sms"]
        env = dotenv_values(root / ".env")
        assert (
            provider["environment"]["FACTORYVPN_SIMULATOR_SMS_KEY"] == env["SMS_PROVIDER_API_KEY"]
        )
    # Re-running up must preserve CA and passwords; another mode must not adopt it.
    secret = (root / "credentials.json").read_bytes()
    assert local_lab.initialize_lab(root, simulator=simulator).state == lab.state
    assert (root / "credentials.json").read_bytes() == secret
    with pytest.raises(RuntimeError, match="mode"):
        local_lab.initialize_lab(root, simulator=not simulator)
    (root / ".runtime/pki-public/authority.json").unlink()
    with pytest.raises(RuntimeError, match="fresh lab with --directory"):
        local_lab.initialize_lab(root, simulator=simulator)
    assert (root / "credentials.json").read_bytes() == secret


def test_lab_commands_use_own_updated_environment(tmp_path, monkeypatch):
    (tmp_path / "lab.json").write_text(json.dumps({
        "purpose": "factoryvpn-local-lab", "project": "factoryvpn-lab-0123456789",
    }))
    certificate = tmp_path / ".runtime/https/cert.pem"
    certificate.parent.mkdir(parents=True)
    monkeypatch.setattr(local_lab.ssl, "create_default_context", lambda **kwargs: None)
    monkeypatch.setenv("SMS_PROVIDER_URL", "https://unrelated.example.test/send")
    environment = tmp_path / ".env"
    environment.write_text("SMS_PROVIDER_URL=http://test-sms:8080/send\nJWT_SECRET=before\n")
    seen = []

    def invoke(args, **kwargs):
        seen.append(kwargs["env"])
        return b""

    monkeypatch.setattr(local_lab, "command", invoke)
    lab = local_lab.Lab(tmp_path)
    lab.docker("config")
    environment.write_text("SMS_PROVIDER_URL=http://test-sms:8080/send\nJWT_SECRET=after\n")
    lab.docker("config")
    assert all(env["SMS_PROVIDER_URL"] == "http://test-sms:8080/send" for env in seen)
    assert [env["JWT_SECRET"] for env in seen] == ["before", "after"]


def test_unmarked_directory_is_never_overwritten(tmp_path):
    with pytest.raises(RuntimeError, match="without its marker"):
        local_lab.initialize_lab(tmp_path, simulator=True)


@pytest.mark.parametrize("lease_seconds", [30, 300, 900])
def test_worker_loss_waits_for_configured_lease(monkeypatch, lease_seconds):
    clock = [0.0]
    monkeypatch.setattr(local_lab.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        local_lab.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    assert (
        local_lab.wait_for_worker_lease(lambda: clock[0] >= lease_seconds + 2, lease_seconds)
        == lease_seconds + 2
    )


@pytest.mark.parametrize("blocked_after", [303, None])
def test_worker_loss_rejects_late_or_missing_expiry(monkeypatch, blocked_after):
    clock = [0.0]
    monkeypatch.setattr(local_lab.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        local_lab.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    with pytest.raises(RuntimeError, match="exceeded|Timed out"):
        local_lab.wait_for_worker_lease(
            lambda: blocked_after is not None and clock[0] >= blocked_after, 300
        )
