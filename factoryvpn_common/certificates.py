"""Bounded X.509 primitives shared by offline tools, issuers and peer verification."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLIENT_DAYS = 3650
SERVER_DAYS = 1825
ISSUER_DAYS = 5475
ROOT_DAYS = 9125
MAX_AUTHORITIES = 32
PEM = serialization.Encoding.PEM


def now():
    return datetime.now(timezone.utc)


def write_private(path, data, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isinstance(data, str):
        data = data.encode()
    temporary = path.with_name(path.name + ".new-" + secrets.token_hex(6)) if replace else path
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if replace:
            temporary.unlink(missing_ok=True)


@contextmanager
def pki_lock(root):
    # PKI mutation runs on Linux; schema export only needs the X.509 helpers
    # and can import this module from Windows as well.
    import fcntl

    path = Path(root) / "locks/pki.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_bounded(path, maximum=1024 * 1024):
    import stat

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Regular PKI file required")
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("PKI file too large")
    return value


def key_bytes(key, password=None):
    return key.private_bytes(
        PEM,
        serialization.PrivateFormat.PKCS8,
        (
            serialization.BestAvailableEncryption(password)
            if password
            else serialization.NoEncryption()
        ),
    )


def new_key():
    return ec.generate_private_key(ec.SECP256R1())


def public_bytes(key):
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def fingerprint(cert):
    return cert.fingerprint(hashes.SHA256()).hex()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid authority identifier")
    return value


def name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def certificate(
    common_name,
    public_key,
    issuer_key,
    issuer,
    *,
    days,
    ca=False,
    path_length=None,
    server=False,
    host=None,
    at=None,
):
    at = at or now()
    if not isinstance(days, int) or not 1 <= days <= ROOT_DAYS:
        raise ValueError("Invalid certificate duration")
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(common_name))
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(at - timedelta(minutes=5))
        .not_valid_after(at + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), critical=True)
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
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
        from ipaddress import ip_address

        try:
            san = x509.IPAddress(ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        builder = builder.add_extension(x509.SubjectAlternativeName([san]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def require_valid(cert, at=None, days=0):
    at = at or now()
    if cert.not_valid_before_utc > at or cert.not_valid_after_utc <= at + timedelta(days=days):
        raise ValueError("Certificate not valid for the requested lifetime")


def require_ca(cert, depth):
    constraint = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = cert.extensions.get_extension_for_class(x509.KeyUsage)
    if (
        not constraint.critical
        or not constraint.value.ca
        or constraint.value.path_length != depth
        or not usage.critical
        or not usage.value.key_cert_sign
        or not usage.value.crl_sign
    ):
        raise ValueError("Invalid CA constraints")
    key = cert.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("Unsupported CA key")


def parse_csr(data):
    if len(data) > 16384:
        raise ValueError("CSR too large")
    csr = x509.load_pem_x509_csr(data)
    key = csr.public_key()
    if (
        not csr.is_signature_valid
        or not isinstance(key, ec.EllipticCurvePublicKey)
        or key.curve.name != "secp256r1"
    ):
        raise ValueError("A signed P-256 CSR is required")
    # Extension requests never grant CA/identity privileges to a caller.
    for extension in csr.extensions:
        if isinstance(extension.value, x509.BasicConstraints) and extension.value.ca:
            raise ValueError("Client CSR cannot request CA privileges")
    return csr


def make_crl(cert, key, revoked=(), *, days=7, number=1, at=None):
    at = at or now()
    require_valid(cert, at)
    end = min(at + timedelta(days=days), cert.not_valid_after_utc)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(cert.subject)
        .last_update(at - timedelta(minutes=1))
        .next_update(end)
        .add_extension(x509.CRLNumber(number), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
    )
    for entry in sorted(revoked, key=lambda item: item.serial_number):
        builder = builder.add_revoked_certificate(entry)
    return builder.sign(key, hashes.SHA256())


def verify_crl(crl, cert, *, at=None, fresh=True):
    if crl.issuer != cert.subject or not crl.is_signature_valid(cert.public_key()):
        raise ValueError("CRL issuer/signature mismatch")
    aki = crl.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value.key_identifier
    ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
    if aki != ski:
        raise ValueError("CRL authority mismatch")
    number = crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
    at = at or now()
    if fresh and (
        crl.last_update_utc > at or crl.next_update_utc is None or crl.next_update_utc <= at
    ):
        raise ValueError("CRL missing or expired")
    return number


def load_authority(root):
    data = json.loads(read_bounded(Path(root) / "pki-public/authority.json"))
    if set(data) != {"version", "generation", "active", "roots", "issuers"} or data["version"] != 2:
        raise ValueError("Invalid authority registry")
    if (
        not 1 <= len(data["roots"]) <= MAX_AUTHORITIES
        or not 1 <= len(data["issuers"]) <= MAX_AUTHORITIES
    ):
        raise ValueError("Invalid authority count")
    if not isinstance(data["generation"], int) or data["generation"] < 1:
        raise ValueError("Invalid trust generation")
    roots, issuers = {}, {}
    for key, value in data["roots"].items():
        identifier(key)
        cert = x509.load_pem_x509_certificate(value["certificate"].encode())
        require_ca(cert, 1)
        cert.verify_directly_issued_by(cert)
        if fingerprint(cert) != key:
            raise ValueError("Root fingerprint mismatch")
        crl = x509.load_pem_x509_crl(value["crl"].encode())
        verify_crl(crl, cert, fresh=False)
        roots[key] = cert
    for key, value in data["issuers"].items():
        identifier(key)
        cert = x509.load_pem_x509_certificate(value["certificate"].encode())
        require_ca(cert, 0)
        root_cert = roots[value["root_id"]]
        cert.verify_directly_issued_by(root_cert)
        if fingerprint(cert) != key or cert.not_valid_after_utc > root_cert.not_valid_after_utc:
            raise ValueError("Invalid issuer identity or lifetime")
        if value["state"] not in {"issuing", "retiring"}:
            raise ValueError("Invalid issuer state")
        root_crl = x509.load_pem_x509_crl(data["roots"][value["root_id"]]["crl"].encode())
        if root_crl.get_revoked_certificate_by_serial_number(cert.serial_number):
            raise ValueError("Issuer has been revoked")
        issuers[key] = cert
    if sum(value["state"] == "issuing" for value in data["issuers"].values()) != 1:
        raise ValueError("Exactly one issuing authority is required")
    if data["issuers"][identifier(data["active"])]["state"] != "issuing":
        raise ValueError("Active issuer mismatch")
    return data, roots, issuers


def load_issuer(root, issuer_id=None, *, days=0):
    root = Path(root)
    data, roots, issuers = load_authority(root)
    issuer_id = identifier(issuer_id or data["active"])
    cert = issuers[issuer_id]
    root_cert = roots[data["issuers"][issuer_id]["root_id"]]
    require_valid(cert, days=days)
    require_valid(root_cert, days=days)
    key = serialization.load_pem_private_key(
        read_bounded(root / "issuers" / (issuer_id + ".key")), None
    )
    if public_bytes(key.public_key()) != public_bytes(cert.public_key()):
        raise ValueError("Issuer signing key mismatch")
    return key, cert


def trust_pem(root):
    _, roots, _ = load_authority(root)
    return b"".join(cert.public_bytes(PEM) for cert in roots.values())


def parse_crls(value):
    pattern = rb"-----BEGIN X509 CRL-----.*?-----END X509 CRL-----"
    blocks = re.findall(pattern, value, re.S)
    if not blocks or re.sub(pattern, b"", value, flags=re.S).strip():
        raise ValueError("Invalid CRL bundle")
    return [x509.load_pem_x509_crl(block) for block in blocks]


def validate_crls(root, *, at=None):
    root = Path(root)
    at = at or now()
    data, roots, issuers = load_authority(root)
    crls = parse_crls(read_bounded(root / "crls/bundle.pem"))
    expected = [
        cert for cert in [*roots.values(), *issuers.values()] if cert.not_valid_after_utc > at
    ]
    if len(crls) != len(expected):
        raise ValueError("Incomplete CRL bundle")
    for cert in expected:
        require_valid(cert, at)
        matches = [
            crl
            for crl in crls
            if crl.issuer == cert.subject and crl.is_signature_valid(cert.public_key())
        ]
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous CRL")
        verify_crl(matches[0], cert, at=at)
        if fingerprint(cert) in roots:
            authoritative = x509.load_pem_x509_crl(data["roots"][fingerprint(cert)]["crl"].encode())
            if matches[0].public_bytes(PEM) != authoritative.public_bytes(PEM):
                raise ValueError("Root CRL bundle is stale")
    return crls
