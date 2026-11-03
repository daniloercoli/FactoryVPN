"""Issuance in memory. The caller commits an encrypted artifact with its public registry."""

from datetime import timedelta
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from factoryvpn_common.protocol import peer_name
from scripts.bootstrap import certificate, hostname, key_bytes, load_ca, write_private
from api.time_utils import utc_now


def issue_profile(root, kind, identifier, host, port):
    host = hostname(host)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid VPN port")
    ca_key, ca = load_ca(root)
    if ca.public_key().public_numbers() != ca_key.public_key().public_numbers():
        raise ValueError("CA certificate and signing key differ")
    if ca.not_valid_before_utc > utc_now():
        raise ValueError("CA is not yet valid")
    if ca.not_valid_after_utc <= utc_now() + timedelta(days=366):
        raise ValueError("CA renewal is required before issuing client certificates")
    key = ec.generate_private_key(ec.SECP256R1())
    cert = certificate(peer_name(kind, identifier), key, ca_key, ca.subject)
    profile = f"""client
dev {'tun0' if kind == 'gateway' else 'tun'}
proto udp
remote {host} {port}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
verify-x509-name factoryvpn-server name
tls-version-min 1.3
data-ciphers AES-256-GCM:AES-128-GCM
verb 3
<ca>
{ca.public_bytes(serialization.Encoding.PEM).decode()}</ca>
<cert>
{cert.public_bytes(serialization.Encoding.PEM).decode()}</cert>
<key>
{key_bytes(key).decode()}</key>
<tls-crypt>
{(root / 'server/tls-crypt.key').read_text()}</tls-crypt>
"""
    return cert, profile.encode()


def publish_crl(root, certificates):
    ca_key, ca = load_ca(root)
    path = root / "server/crl.pem"
    entries = {}
    if path.exists():
        existing = x509.load_pem_x509_crl(path.read_bytes())
        if not existing.is_signature_valid(ca.public_key()):
            raise ValueError("Existing CRL signature differs from the configured CA")
        entries = {entry.serial_number: entry for entry in existing}
    for cert in certificates:
        serial = int(cert.serial)
        entries[serial] = (
            entries.get(serial)
            or x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(cert.revoked_at)
            .build()
        )
    now = utc_now()
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca.subject)
        .last_update(now - timedelta(minutes=1))
        .next_update(now + timedelta(days=7))
    )
    for entry in entries.values():
        builder = builder.add_revoked_certificate(entry)
    crl = builder.sign(ca_key, hashes.SHA256())
    write_private(path, crl.public_bytes(serialization.Encoding.PEM), replace=path.exists())
