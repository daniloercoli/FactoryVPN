"""Generated credentials/certificates, safe reruns and certificate revocation."""

import stat
from uuid import uuid4
import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from scripts.lab_pki import initialize
from factoryvpn_common.pki import issue_profile, publish_crl
from factoryvpn_common.certificates import load_authority, validate_crls
from types import SimpleNamespace
from api.time_utils import utc_now
from api.config import Settings


def test_bootstrap_issues_certificate_bound_profiles_and_rotates_without_replacing_ca(tmp_path):
    root = tmp_path / "runtime"
    env = tmp_path / ".env"
    initialize(root, "vpn.example.test", env)
    ca_before = (root / "pki-public/authority.json").read_bytes()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert (root / "factory/allow.conf").read_text() == "deny all;\n"
    assert (root / "server/ca.key").exists() is False
    assert "JWT_SECRET=" in env.read_text() and "change-this" not in env.read_text()
    server = x509.load_pem_x509_certificate((root / "server/server.crt").read_bytes())
    assert (
        ExtendedKeyUsageOID.SERVER_AUTH
        in server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )
    assert server.extensions.get_extension_for_class(x509.KeyUsage).value.key_encipherment
    identifier = uuid4()
    cert, profile = issue_profile(root, "technician", identifier, "vpn.example.test", 1194)
    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == f"technician-{identifier}"
    )
    assert b"<extra-certs>" in profile and b"<tls-crypt>" in profile
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days == 3650
    assert (server.not_valid_after_utc - server.not_valid_before_utc).days == 1825
    with pytest.raises(FileExistsError):
        initialize(root, "vpn.example.test", env)
    replacement, _ = issue_profile(root, "technician", identifier, "vpn.example.test", 1194)
    entry = SimpleNamespace(
        serial=str(cert.serial_number),
        revoked_at=utc_now(),
        issuer_id=load_authority(root)[0]["active"],
    )
    publish_crl(root, [entry])
    publish_crl(root, [entry])
    crls = validate_crls(root)
    assert sum(len(crl) for crl in crls) == 1
    assert any(crl.get_revoked_certificate_by_serial_number(cert.serial_number) for crl in crls)
    assert replacement.serial_number != cert.serial_number
    assert (root / "pki-public/authority.json").read_bytes() == ca_before
    assert not (root / "ca/ca.key").exists()


def test_bootstrap_rejects_config_injection_before_writing(tmp_path):
    with pytest.raises(ValueError):
        initialize(tmp_path / "bad", "host\nscript-security 3")
    assert not (tmp_path / "bad").exists()


def test_production_settings_reject_defaults_and_accept_generated_environment(
    tmp_path, monkeypatch
):
    # _env_file=None disables dotenv loading, not process environment variables.
    # Test defaults and the generated file independently of the runner's settings.
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(_env_file=None, ENVIRONMENT="production")
    with pytest.raises(ValueError):
        Settings(_env_file=None, PROVISIONING_SECRET_KEY="invalid-key")
    env = tmp_path / ".env"
    initialize(tmp_path / "runtime", "localhost", env)
    settings = Settings(
        _env_file=env, DATABASE_URL="postgresql+asyncpg://user:test-random-pass@db/database"
    )
    assert "# CONTINUITÀ DELLA VPN" in env.read_text()
    assert settings.VPN_LEASE_SECONDS == 300
    assert settings.VPN_SESSION_MINUTES == 180
    assert settings.PORTAL_SESSION_MINUTES == 720
    assert settings.ENVIRONMENT == "production"
    assert settings.FACTORY_STATIONS == {}
    assert len(settings.JWT_SECRET) >= 32
    assert len(settings.WORKER_API_KEY) >= 32
