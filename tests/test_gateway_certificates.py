"""The device validates signed identity, key, usage, time and monotonic trust."""

from datetime import timedelta
from importlib import import_module
from uuid import uuid4
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from factoryvpn_common.certificates import (
    PEM,
    new_key,
    name,
    certificate,
    load_issuer,
    fingerprint,
    now,
)
from factoryvpn_common.pki import issue_profile, issue_csr
from scripts.lab_pki import initialize

agent = import_module("gateway-agent.certificate_renewal")


@pytest.mark.parametrize(
    "damage", ["hash", "key", "identity", "ca", "eku", "expired", "root", "generation"]
)
def test_device_rejects_invalid_signed_bundle(tmp_path, damage):
    root = tmp_path / "runtime"
    initialize(root, "vpn.example.test")
    gateway = uuid4()
    _, profile = issue_profile(root, "gateway", gateway, "vpn.example.test", 1194)
    key = new_key()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(name("ignored"))
        .sign(key, hashes.SHA256())
    )
    cert, issuer_id, chain, trust, generation = issue_csr(
        root, "gateway", gateway, csr.public_bytes(PEM)
    )
    if damage in ("identity", "ca", "eku", "expired"):
        issuer_key, issuer = load_issuer(root)
        cert = certificate(
            "wrong" if damage == "identity" else "gateway-" + str(gateway),
            key.public_key(),
            issuer_key,
            issuer.subject,
            days=1 if damage == "expired" else 3650,
            ca=damage == "ca",
            path_length=0 if damage == "ca" else None,
            server=damage == "eku",
            at=now() - timedelta(days=2) if damage == "expired" else None,
        )
    bundle = dict(
        certificate_pem=cert.public_bytes(PEM).decode(),
        issuer_id=issuer_id,
        issuer_pem=chain,
        trust_pem=trust,
        trust_generation=generation,
        certificate_sha256=fingerprint(cert),
    )
    if damage == "hash":
        bundle["certificate_sha256"] = "0" * 64
    elif damage == "key":
        key = new_key()
    elif damage == "root":
        bundle["trust_pem"] = chain
    elif damage == "generation":
        bundle["trust_generation"] = generation - 1
    with pytest.raises(ValueError):
        agent.validated_profile(profile.decode(), bundle, key, gateway)


async def test_sms_wakeup_rechecks_remote_request_without_idle_polling(tmp_path):
    from types import SimpleNamespace

    root = tmp_path / "runtime"
    initialize(root, "vpn.example.test")
    gateway = uuid4()
    _, profile = issue_profile(root, "gateway", gateway, "vpn.example.test", 1194)
    path = tmp_path / "gateway.ovpn"
    path.write_bytes(profile)
    renewer = agent.CertificateRenewer(SimpleNamespace(OPENVPN_CONFIG=str(path)))
    calls = []

    async def request(*args, **kwargs):
        calls.append(args)
        return {"renewal_due": False}

    renewer.request = request
    await renewer.tick(idle=True, awake=False)
    assert not calls
    await renewer.tick(idle=True, awake=True)
    await renewer.tick(idle=True, awake=True)
    assert len(calls) == 1
    await renewer.tick(idle=True, awake=False)
    assert len(calls) == 1
    await renewer.tick(idle=True, awake=True)
    assert len(calls) == 2
