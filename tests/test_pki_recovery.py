"""Multiple generations, archived signing state and offline-root-free recovery."""

import base64
import os
import shutil
from datetime import timedelta
from uuid import uuid4

import pytest
from factoryvpn_common.certificates import load_authority, now, fingerprint, validate_crls
from factoryvpn_common.pki import publish_crl, issue_profile
from factoryvpn_common.backup_archive import pack, unpack, private_write, pki_files
from scripts.pki import prepare, sign_issuer, import_issuer, root_init
from scripts.bootstrap import initialize, renew_server, recovery_initialize
from scripts.backup_coordinator import Coordinator
from api.services.server_readiness import pki_status

PASSWORD = b"offline-test-passphrase"


@pytest.fixture
def installation(tmp_path):
    offline, online = tmp_path / "offline", tmp_path / "source/runtime"
    root = root_init(offline, PASSWORD)
    request = prepare(online)
    import_issuer(online, sign_issuer(offline, PASSWORD, request.read_bytes()), fingerprint(root))
    initialize(online, "vpn.example.test")
    return offline, online, root


def test_multiple_issuers_server_rotation_and_status(installation):
    offline, online, root = installation
    old_cert, _ = issue_profile(online, "gateway", uuid4(), "vpn.example.test", 1194)
    old_issuer = load_authority(online)[2][load_authority(online)[0]["active"]]
    request = prepare(online)
    import_issuer(online, sign_issuer(offline, PASSWORD, request.read_bytes()), fingerprint(root))
    server = renew_server(online, replace=True)
    publish_crl(online, [])
    assert len(validate_crls(online)) == 3
    old_cert.verify_directly_issued_by(old_issuer)
    server.verify_directly_issued_by(load_authority(online)[2][load_authority(online)[0]["active"]])
    status = pki_status(online, now())
    assert status.state == "ok" and len(status.authorities) == 3
    assert all(item.crl_next_update is not None for item in status.authorities)
    issuing = next(item for item in status.authorities if item.role == "issuing")
    assert 1822 <= (issuing.issuance_deadline - now()).days <= 1824
    root_status = next(item for item in status.authorities if item.role == "root")
    assert 3647 <= (root_status.issuance_deadline - now()).days <= 3649
    assert pki_status(online, now() + timedelta(days=8)).state == "error"


def test_archive_and_recovery_never_contain_private_root(installation, tmp_path):
    offline, online, root = installation
    request = prepare(online)
    import_issuer(online, sign_issuer(offline, PASSWORD, request.read_bytes()), fingerprint(root))
    publish_crl(online, [])
    source = online.parent
    private_write(source / "database.dump", b"synthetic dump")
    private_write(source / "configuration.json", b"{}")
    recovery_key = os.urandom(32)
    key_file = tmp_path / "recovery.key"
    private_write(key_file, base64.b64encode(recovery_key))
    archive = tmp_path / "server.fvbackup"
    manifest = pack(
        source,
        archive,
        recovery_key,
        dict(postgres_major=15, installation_id=str(uuid4()), created_at=now().isoformat()),
    )
    assert manifest["format"] == 2
    assert len([path for path in manifest["files"] if path.startswith("runtime/issuers/")]) == 2
    assert not any("root.key" in path or "ca/ca.key" in path for path in manifest["files"])
    restored = tmp_path / "restored"
    recovery_initialize(restored, "vpn.example.test", tmp_path / "new.env", archive, key_file)
    assert load_authority(restored)[0] == load_authority(online)[0]
    assert not (restored / "root.key").exists()
    assert len(validate_crls(restored)) == 3
    checked = tmp_path / "checked"
    checked.mkdir()
    unpack(archive, checked, recovery_key, 1024 * 1024)
    Coordinator.validate_runtime(None, checked)
    assert set(pki_files(restored)) == set(pki_files(online))


def test_restore_preserves_newer_issuer_keys_and_crl_counters(installation, tmp_path):
    offline, online, root = installation
    archived = tmp_path / "archived"
    shutil.copytree(online, archived)
    request = prepare(online)
    new = import_issuer(
        online, sign_issuer(offline, PASSWORD, request.read_bytes()), fingerprint(root)
    )
    publish_crl(online, [])
    original = (online / "crls/bundle.pem").read_bytes()
    coordinator = object.__new__(Coordinator)
    coordinator.runtime = online
    coordinator.restore_pki(archived)
    assert load_authority(online)[0]["active"] == new
    assert (online / "crls/bundle.pem").read_bytes() == original


def test_integrity_accepts_expired_online_crl_for_regeneration(installation, monkeypatch):
    _, online, _ = installation
    import factoryvpn_common.certificates as certificates

    observed = now()
    monkeypatch.setattr(certificates, "now", lambda: observed + timedelta(days=8))
    with pytest.raises(ValueError, match="expired"):
        validate_crls(online)
    Coordinator.validate_runtime(None, online.parent)
    publish_crl(online, [])
    assert validate_crls(online)


def test_bundle_rejects_appended_garbage(installation):
    _, online, _ = installation
    path = online / "crls/bundle.pem"
    path.write_bytes(path.read_bytes() + b"unexpected content")
    with pytest.raises(ValueError):
        validate_crls(online)
    with pytest.raises(ValueError):
        publish_crl(online, [])
