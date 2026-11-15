"""Deployment station credentials and trusted-address configuration."""

import hashlib
import stat

import pytest
from dotenv import dotenv_values

from api.config import Settings
from scripts.factory_access import add_station, allow_ips, load_stations, revoke_station


def test_station_add_rotate_revoke_keeps_secrets_out_of_environment(tmp_path):
    root, env = tmp_path / "runtime", tmp_path / ".env"
    env.write_text("JWT_SECRET=preserved\nFACTORY_STATIONS={}\n")
    key = tmp_path / "station.key"
    add_station(root, env, "line-1", key)
    secret = key.read_text().strip()
    assert len(secret) >= 43
    assert secret not in env.read_text()
    assert load_stations(env) == {"line-1": hashlib.sha256(secret.encode()).hexdigest()}
    assert stat.S_IMODE(key.stat().st_mode) == stat.S_IMODE(env.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        add_station(root, env, "line-1", tmp_path / "other.key")
    with pytest.raises(FileExistsError):
        add_station(root, env, "line-1", key, rotate=True)
    replacement = tmp_path / "replacement.key"
    add_station(root, env, "line-1", replacement, rotate=True)
    assert load_stations(env)["line-1"] != hashlib.sha256(secret.encode()).hexdigest()
    add_station(root, env, "line-2", tmp_path / "line2.key")
    revoke_station(root, env, "line-1")
    assert set(load_stations(env)) == {"line-2"}
    assert dotenv_values(env)["JWT_SECRET"] == "preserved"


def test_allowlist_is_explicit_and_does_not_accept_configuration_injection(tmp_path):
    allow_ips(tmp_path, ["192.0.2.7", "2001:db8::/64"])
    path = tmp_path / "factory/allow.conf"
    content = path.read_text()
    assert "allow 192.0.2.7/32;" in content
    assert "allow 2001:db8::/64;" in content
    assert content.endswith("deny all;\n")
    for address in ("all", "0.0.0.0/0", "::/0", "192.0.2.7;\nallow all;"):
        with pytest.raises(ValueError):
            allow_ips(tmp_path, [address])
        assert path.read_text() == content
    allow_ips(tmp_path, [])
    assert "allow " not in path.read_text()


@pytest.mark.parametrize(
    "stations", [{"line": "cleartext"}, {"../line": "a" * 64}, {"a": "a" * 64, "b": "a" * 64}]
)
def test_settings_reject_invalid_or_shared_station_keys(stations):
    with pytest.raises(ValueError):
        Settings(_env_file=None, FACTORY_STATIONS=stations)
