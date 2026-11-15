"""Exercise the actual offline/online boundary and reject broken trust imports."""

from datetime import timedelta
import stat
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from factoryvpn_common.certificates import (
    CLIENT_DAYS,
    SERVER_DAYS,
    PEM,
    certificate,
    fingerprint,
    load_authority,
    load_issuer,
    now,
    public_bytes,
    require_valid,
)
from scripts.pki import root_init, prepare, sign_issuer, import_issuer, root_crl, import_root_crl

PASSWORD = b"offline-test-passphrase"


@pytest.fixture
def ceremony(tmp_path):
    offline, online = tmp_path / "offline", tmp_path / "online"
    root_cert = root_init(offline, PASSWORD)
    request = prepare(online)
    bundle = sign_issuer(offline, PASSWORD, request.read_bytes())
    return offline, online, root_cert, bundle


def test_root_never_enters_online_and_lifetimes(ceremony):
    offline, online, root_cert, bundle = ceremony
    issuer_id = import_issuer(online, bundle, fingerprint(root_cert))
    key, issuer = load_issuer(online, days=CLIENT_DAYS + 1)
    assert stat.S_IMODE((offline / "root.key").stat().st_mode) == 0o600
    with pytest.raises(TypeError):
        serialization.load_pem_private_key((offline / "root.key").read_bytes(), None)
    assert not list(online.rglob("root.key"))
    assert len(list(online.rglob("*.key"))) == 1
    assert issuer_id == fingerprint(issuer)
    leaf = certificate("gateway-test", key.public_key(), key, issuer.subject, days=CLIENT_DAYS)
    server = certificate(
        "factoryvpn-server", key.public_key(), key, issuer.subject, days=SERVER_DAYS, server=True
    )
    assert (leaf.not_valid_after_utc - now()).days == 3649
    assert (server.not_valid_after_utc - now()).days == 1824
    leaf.verify_directly_issued_by(issuer)
    issuer.verify_directly_issued_by(root_cert)
    assert public_bytes(root_cert.public_key()) != public_bytes(issuer.public_key())


@pytest.mark.parametrize("damage", ["fingerprint", "signature", "key", "private"])
def test_import_rejects_invalid_ceremony(ceremony, tmp_path, damage):
    offline, online, cert, bundle = ceremony
    expected = fingerprint(cert)
    if damage == "fingerprint":
        expected = "0" * 64
    elif damage == "signature":
        other = root_init(tmp_path / "other", PASSWORD)
        bundle["root"] = other.public_bytes(PEM).decode()
        expected = fingerprint(other)
    elif damage == "key":
        for path in (online / "pending").glob("*.key"):
            path.unlink()
    else:
        bundle["root"] += (offline / "root.key").read_text()
    with pytest.raises((ValueError, FileNotFoundError)):
        import_issuer(online, bundle, expected)
    assert not (online / "pki-public/authority.json").exists()


def test_issuer_rotation_retains_old_certificates(ceremony):
    offline, online, cert, bundle = ceremony
    old = import_issuer(online, bundle, fingerprint(cert))
    request = prepare(online)
    new = import_issuer(
        online, sign_issuer(offline, PASSWORD, request.read_bytes()), fingerprint(cert)
    )
    data, _, issuers = load_authority(online)
    assert data["active"] == new
    assert data["issuers"][old]["state"] == "retiring"
    assert set(issuers) == {old, new}
    load_issuer(online, old)
    with pytest.raises(FileExistsError):
        import_issuer(online, bundle, fingerprint(cert))


def test_root_crl_refresh_rejects_rollback(ceremony):
    offline, online, cert, bundle = ceremony
    import_issuer(online, bundle, fingerprint(cert))
    root_crl(offline, PASSWORD)
    import_root_crl(online, (offline / "root.crl").read_bytes())
    with pytest.raises(ValueError, match="advance"):
        import_root_crl(online, bundle["root_crl"].encode())
    data, _, _ = load_authority(online)
    crl = x509.load_pem_x509_crl(data["roots"][fingerprint(cert)]["crl"].encode())
    assert crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number == 2


def test_requested_lifetime_must_fit_issuer(ceremony):
    _, online, cert, bundle = ceremony
    import_issuer(online, bundle, fingerprint(cert))
    _, issuer = load_issuer(online)
    with pytest.raises(ValueError, match="lifetime"):
        require_valid(
            issuer, at=issuer.not_valid_after_utc - timedelta(days=3650), days=CLIENT_DAYS + 1
        )


def test_no_overwrite_offline_root(ceremony):
    offline, _, _, _ = ceremony
    original = (offline / "root.key").read_bytes()
    with pytest.raises(FileExistsError):
        root_init(offline, PASSWORD)
    assert (offline / "root.key").read_bytes() == original


def test_online_crl_bundle_refresh_and_fail_closed(tmp_path):
    from scripts.lab_pki import initialize
    from factoryvpn_common.certificates import validate_crls
    from factoryvpn_common.pki import issue_profile, publish_crl, certificate_issuer
    from types import SimpleNamespace
    from uuid import uuid4

    root = tmp_path / "runtime"
    initialize(root, "vpn.example.test")
    cert, _ = issue_profile(root, "gateway", uuid4(), "vpn.example.test", 1194)
    before = validate_crls(root)
    assert len(before) == 2 and not any(len(crl) for crl in before)
    publish_crl(root, [])
    empty = validate_crls(root)
    issuer_id = certificate_issuer(root, cert)
    issuer = load_authority(root)[2][issuer_id]

    def number(crl):
        return crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number

    assert (
        number(next(crl for crl in empty if crl.issuer == issuer.subject))
        == number(next(crl for crl in before if crl.issuer == issuer.subject)) + 1
    )
    publish_crl(
        root,
        [
            SimpleNamespace(
                serial=str(cert.serial_number),
                revoked_at=now(),
                issuer_id=certificate_issuer(root, cert),
            )
        ],
    )
    after = validate_crls(root)
    assert any(crl.get_revoked_certificate_by_serial_number(cert.serial_number) for crl in after)
    path = root / "crls/bundle.pem"
    good = path.read_bytes()
    path.write_bytes(after[0].public_bytes(PEM))
    with pytest.raises(ValueError, match="Incomplete"):
        validate_crls(root)
    path.write_bytes(good)
    with pytest.raises(ValueError, match="expired"):
        validate_crls(root, at=now() + timedelta(days=8))
    path.unlink()
    with pytest.raises(FileNotFoundError):
        validate_crls(root)
