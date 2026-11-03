"""Initialize deployment secrets and the VPN CA/server for a fresh installation."""

import argparse
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
import os
from pathlib import Path
import re
import secrets
import textwrap

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from factoryvpn_common.protocol import peer_name


def hostname(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", value):
        raise ValueError("Invalid server hostname or IP address")
    return value


def write_private(path, data, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(data, str):
        data = data.encode()
    if replace:
        temporary = path.with_name(path.name + ".new-" + secrets.token_hex(6))
        write_private(temporary, data)
        os.replace(temporary, path)
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)


def key_bytes(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def certificate(common_name, key, issuer_key, issuer, *, ca=False, server=False, host=None):
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(common_name))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650 if ca else 365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=server,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
    if host:
        try:
            san = x509.IPAddress(ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        builder = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def load_ca(root):
    key = serialization.load_pem_private_key((root / "ca/ca.key").read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate((root / "ca/ca.crt").read_bytes())
    return key, cert


def update_crl(root, serial=None):
    key, ca = load_ca(root)
    path = root / "server/crl.pem"
    revoked = list(x509.load_pem_x509_crl(path.read_bytes())) if path.exists() else []
    if serial and not any(entry.serial_number == serial for entry in revoked):
        revoked.append(
            x509.RevokedCertificateBuilder()
            .serial_number(serial)
            .revocation_date(datetime.now(timezone.utc))
            .build()
        )
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca.subject)
        .last_update(now)
        .next_update(now + timedelta(days=365))
    )
    for entry in revoked:
        builder = builder.add_revoked_certificate(entry)
    write_private(
        path,
        builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM),
        replace=path.exists(),
    )


def initialize(root, host, env_file=None):
    root = Path(root)
    host = hostname(host)
    if (root / "ca/ca.key").exists() or (env_file and Path(env_file).exists()):
        raise FileExistsError(
            "Existing CA or environment file; initialization refuses to overwrite secrets"
        )
    ca_key, server_key = (
        ec.generate_private_key(ec.SECP256R1()),
        ec.generate_private_key(ec.SECP256R1()),
    )
    ca = certificate("FactoryVPN CA", ca_key, ca_key, name("FactoryVPN CA"), ca=True)
    server = certificate("factoryvpn-server", server_key, ca_key, ca.subject, server=True)
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    write_private(root / "ca/ca.key", key_bytes(ca_key))
    write_private(root / "ca/ca.crt", ca_pem)
    write_private(root / "server/ca.crt", ca_pem)
    write_private(root / "server/server.crt", server.public_bytes(serialization.Encoding.PEM))
    write_private(root / "server/server.key", key_bytes(server_key))
    static_key = secrets.token_hex(256)
    write_private(
        root / "server/tls-crypt.key",
        "-----BEGIN OpenVPN Static key V1-----\n"
        + "\n".join(textwrap.wrap(static_key, 32))
        + "\n-----END OpenVPN Static key V1-----\n",
    )
    update_crl(root)
    # Local development HTTPS certificate. Use a publicly trusted certificate in production.
    https_key = ec.generate_private_key(ec.SECP256R1())
    https_cert = certificate(host, https_key, ca_key, ca.subject, server=True, host=host)
    write_private(root / "https/cert.pem", https_cert.public_bytes(serialization.Encoding.PEM))
    write_private(root / "https/key.pem", key_bytes(https_key))
    if env_file:
        write_private(
            env_file,
            f"""POSTGRES_PASSWORD={secrets.token_urlsafe(32)}
JWT_SECRET={secrets.token_urlsafe(48)}
EMAIL_CODE_KEY={secrets.token_urlsafe(48)}
WORKER_API_KEY={secrets.token_urlsafe(48)}
PROVISIONING_SECRET_KEY={Fernet.generate_key().decode()}
PORTAL_PUBLIC_URL=https://{host}
ENVIRONMENT=production
OPENVPN_SERVER_HOST={host}
FACTORYVPN_RUNTIME_DIR={root.resolve()}
SMS_PROVIDER=generic
SMS_PROVIDER_URL=
SMS_PROVIDER_API_KEY=
""",
        )


def issue(root, kind, identifier, host, port=1194, replace=False):
    root = Path(root)
    cn, host = peer_name(kind, identifier), hostname(host)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid VPN port")
    profile, cert_path = root / "clients" / f"{cn}.ovpn", root / "ca/issued" / f"{cn}.crt"
    if (profile.exists() or cert_path.exists()) and not replace:
        raise FileExistsError("Client already exists; use --replace to revoke and rotate it")
    ca_key, ca = load_ca(root)
    if cert_path.exists():
        update_crl(root, x509.load_pem_x509_certificate(cert_path.read_bytes()).serial_number)
    client_key = ec.generate_private_key(ec.SECP256R1())
    cert = certificate(cn, client_key, ca_key, ca.subject)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    contents = f"""client
dev tun0
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
{cert_pem}</cert>
<key>
{key_bytes(client_key).decode()}</key>
<tls-crypt>
{(root/'server/tls-crypt.key').read_text()}</tls-crypt>
"""
    write_private(cert_path, cert_pem, replace=cert_path.exists())
    write_private(profile, contents, replace=profile.exists())
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".runtime"))
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init")
    init.add_argument("--host", required=True)
    init.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    initialize(args.root, args.host, args.env_file)


if __name__ == "__main__":
    main()
