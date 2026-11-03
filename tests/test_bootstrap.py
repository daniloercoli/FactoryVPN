"""Generated credentials/certificates, safe reruns and certificate revocation."""

import stat
from uuid import uuid4
import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from scripts.bootstrap import initialize, issue, update_crl
from api.config import Settings


def test_bootstrap_issues_certificate_bound_profiles_and_rotates_without_replacing_ca(tmp_path):
    root = tmp_path / "runtime"
    env = tmp_path / ".env"
    initialize(root, "vpn.example.test", env)
    ca_before = (root / "ca/ca.key").read_bytes()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert (root / "server/ca.key").exists() is False
    assert "JWT_SECRET=" in env.read_text() and "change-this" not in env.read_text()
    server = x509.load_pem_x509_certificate((root / "server/server.crt").read_bytes())
    assert (
        ExtendedKeyUsageOID.SERVER_AUTH
        in server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    )
    assert server.extensions.get_extension_for_class(x509.KeyUsage).value.key_encipherment
    identifier = uuid4()
    profile = issue(root, "technician", identifier, "vpn.example.test")
    cert_path = root / "ca/issued" / f"technician-{identifier}.crt"
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == f"technician-{identifier}"
    )
    assert "<tls-crypt>" in profile.read_text()
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        initialize(root, "vpn.example.test", env)
    with pytest.raises(FileExistsError):
        issue(root, "technician", identifier, "vpn.example.test")
    issue(root, "technician", identifier, "vpn.example.test", replace=True)
    crl = x509.load_pem_x509_crl((root / "server/crl.pem").read_bytes())
    assert crl.get_revoked_certificate_by_serial_number(cert.serial_number)
    assert (
        x509.load_pem_x509_certificate(cert_path.read_bytes()).serial_number != cert.serial_number
    )
    update_crl(root, cert.serial_number)
    assert len(x509.load_pem_x509_crl((root / "server/crl.pem").read_bytes())) == 1
    assert (root / "ca/ca.key").read_bytes() == ca_before


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
    assert settings.ENVIRONMENT == "production"
    assert len(settings.JWT_SECRET) >= 32
    assert len(settings.WORKER_API_KEY) >= 32
