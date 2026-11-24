"""Online issuance and per-issuer CRLs. Private root material is never loaded."""

from datetime import timedelta
from pathlib import Path
from cryptography import x509
from factoryvpn_common.certificates import (
    PEM,
    CLIENT_DAYS,
    certificate,
    key_bytes,
    load_authority,
    load_issuer,
    make_crl,
    new_key,
    now,
    pki_lock,
    read_bounded,
    trust_pem,
    verify_crl,
    write_private,
    parse_csr,
    parse_crls,
)
from factoryvpn_common.protocol import peer_name


def issue_certificate(root, kind, identifier, public_key, days=CLIENT_DAYS):
    ca_key, ca = load_issuer(root, days=days + 1)
    return certificate(peer_name(kind, identifier), public_key, ca_key, ca.subject, days=days)


def certificate_issuer(root, cert):
    data, _, issuers = load_authority(root)
    for issuer_id, issuer in issuers.items():
        if cert.issuer == issuer.subject:
            cert.verify_directly_issued_by(issuer)
            return issuer_id
    raise ValueError("Unknown certificate issuer")


def issue_csr(root, kind, identifier, csr_data, days=CLIENT_DAYS):
    csr = parse_csr(csr_data)
    with pki_lock(root):
        cert = issue_certificate(root, kind, identifier, csr.public_key(), days)
        data, _, issuers = load_authority(root)
        issuer_id = certificate_issuer(root, cert)
        return (
            cert,
            issuer_id,
            issuers[issuer_id].public_bytes(PEM).decode(),
            trust_pem(root).decode(),
            data["generation"],
        )


def issue_profile(root, kind, identifier, host, port, days=CLIENT_DAYS):
    from scripts.bootstrap import hostname

    root, host = Path(root), hostname(host)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid VPN port")
    with pki_lock(root):
        key = new_key()
        cert = issue_certificate(root, kind, identifier, key.public_key(), days)
        _, _, issuers = load_authority(root)
        issuer = issuers[certificate_issuer(root, cert)]
        # Let the server observe an orderly technician disconnect without the keepalive timeout.
        exit_notify = "explicit-exit-notify 1\n" if kind == "technician" else ""
        profile = f"""# factoryvpn-trust-generation {load_authority(root)[0]["generation"]}
client
dev {'tun0' if kind == 'gateway' else 'tun'}
proto udp
remote {host} {port}
{exit_notify}resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
verify-x509-name factoryvpn-server name
tls-version-min 1.3
data-ciphers AES-256-GCM:AES-128-GCM
verb 3
<ca>
{trust_pem(root).decode()}</ca>
<extra-certs>
{issuer.public_bytes(PEM).decode()}</extra-certs>
<cert>
{cert.public_bytes(PEM).decode()}</cert>
<key>
{key_bytes(key).decode()}</key>
<tls-crypt>
{(root / 'transport/tls-crypt.key').read_text()}</tls-crypt>
"""
        return cert, profile.encode()


def publish_crl(root, certificates):
    root = Path(root)
    with pki_lock(root):
        data, roots, issuers = load_authority(root)
        path = root / "crls/bundle.pem"
        previous = []
        if path.exists():
            previous = parse_crls(read_bounded(path))
            for crl in previous:
                matches = [
                    ca
                    for ca in [*roots.values(), *issuers.values()]
                    if crl.issuer == ca.subject and crl.is_signature_valid(ca.public_key())
                ]
                if len(matches) != 1:
                    raise ValueError("Existing CRL has an unknown issuer")
                verify_crl(crl, matches[0], fresh=False)
        output = []
        for root_id, ca in roots.items():
            if ca.not_valid_after_utc <= now():
                continue
            crl = x509.load_pem_x509_crl(data["roots"][root_id]["crl"].encode())
            verify_crl(crl, ca)
            output.append(crl.public_bytes(PEM))
        for issuer_id, ca in issuers.items():
            if ca.not_valid_after_utc <= now():
                continue
            key, _ = load_issuer(root, issuer_id)
            matches = [
                crl
                for crl in previous
                if crl.issuer == ca.subject and crl.is_signature_valid(ca.public_key())
            ]
            if len(matches) > 1:
                raise ValueError("Duplicate issuer CRL")
            old = matches[0] if matches else None
            entries = {entry.serial_number: entry for entry in old} if old else {}
            for item in certificates:
                item_issuer = getattr(item, "issuer_id", None)
                if item_issuer is None:
                    # Only isolated smoke fixtures omit the persisted issuer reference.
                    item_issuer = data["active"]
                if item_issuer == issuer_id:
                    serial = int(item.serial)
                    entries.setdefault(
                        serial,
                        x509.RevokedCertificateBuilder()
                        .serial_number(serial)
                        .revocation_date(item.revoked_at)
                        .build(),
                    )
            crl = make_crl(
                ca,
                key,
                entries.values(),
                number=verify_crl(old, ca, fresh=False) + 1 if old is not None else 1,
            )
            output.append(crl.public_bytes(PEM))
        write_private(path, b"".join(output), replace=path.exists())


def crl_refresh_required(root):
    from factoryvpn_common.certificates import validate_crls

    try:
        crls = validate_crls(root)
        return any(crl.next_update_utc < now() + timedelta(days=1) for crl in crls)
    except (OSError, ValueError, x509.ExtensionNotFound):
        return True
