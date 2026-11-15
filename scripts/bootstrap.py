"""Finalize an installation after importing its offline-signed issuing authority."""

import argparse
from pathlib import Path
import re
import secrets
import textwrap
from cryptography.fernet import Fernet
from factoryvpn_common.certificates import (
    PEM,
    SERVER_DAYS,
    certificate,
    key_bytes,
    load_issuer,
    new_key,
    pki_lock,
    trust_pem,
    write_private,
)


def hostname(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", value):
        raise ValueError("Invalid server hostname or IP address")
    return value


def load_ca(root):
    return load_issuer(Path(root))


def renew_server(root, days=SERVER_DAYS, replace=False):
    root = Path(root)
    with pki_lock(root):
        ca_key, ca = load_issuer(root, days=days + 1)
        key = new_key()
        cert = certificate(
            "factoryvpn-server", key.public_key(), ca_key, ca.subject, days=days, server=True
        )
        # Called only in maintenance with OpenVPN stopped. Never replace a live pair.
        for filename, value in {
            "server.key": key_bytes(key),
            "server.crt": cert.public_bytes(PEM),
            "chain.pem": ca.public_bytes(PEM),
            "ca.crt": trust_pem(root),
        }.items():
            write_private(root / "server" / filename, value, replace=replace)
    return cert


def initialize(root, host, env_file=None):
    root, host = Path(root), hostname(host)
    if (root / "server/server.key").exists() or (env_file and Path(env_file).exists()):
        raise FileExistsError("Existing server or environment; refusing to overwrite secrets")
    # This fails if the offline ceremony was not completed. No root is generated here.
    load_issuer(root, days=SERVER_DAYS + 1)
    renew_server(root)
    static_key = secrets.token_hex(256)
    write_private(
        root / "transport/tls-crypt.key",
        "-----BEGIN OpenVPN Static key V1-----\n"
        + "\n".join(textwrap.wrap(static_key, 32))
        + "\n-----END OpenVPN Static key V1-----\n",
    )
    from factoryvpn_common.pki import publish_crl

    publish_crl(root, [])
    initialize_https(root, host)
    initialize_environment(root, host, env_file)


def initialize_https(root, host):
    # Temporary self-signed HTTPS identity; production must install a trusted certificate.
    https_key = new_key()
    from factoryvpn_common.certificates import name

    https_cert = certificate(
        host, https_key.public_key(), https_key, name(host), days=365, server=True, host=host
    )
    write_private(root / "https/cert.pem", https_cert.public_bytes(PEM))
    write_private(root / "https/key.pem", key_bytes(https_key))


def initialize_environment(root, host, env_file):
    # L'accesso delle postazioni resta chiuso anche dopo una nuova recovery.
    write_private(Path(root) / "factory/allow.conf", "deny all;\n")
    if env_file:
        # Il modello è unico: anche il .env generato conserva le spiegazioni italiane.
        template = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
        values = {
            "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
            "JWT_SECRET": secrets.token_urlsafe(48),
            "EMAIL_CODE_KEY": secrets.token_urlsafe(48),
            "WORKER_API_KEY": secrets.token_urlsafe(48),
            "PROVISIONING_SECRET_KEY": Fernet.generate_key().decode(),
            "PORTAL_PUBLIC_URL": f"https://{host}",
            "OPENVPN_SERVER_HOST": host,
            "FACTORYVPN_RUNTIME_DIR": str(root.resolve()),
        }
        lines = []
        for line in template.splitlines():
            key = line.partition("=")[0]
            lines.append(f"{key}={values[key]}" if key in values else line)
        write_private(env_file, "\n".join(lines) + "\n")


def recovery_initialize(root, host, env_file, archive, recovery_key):
    """Stage authenticated online PKI without inventing a new root for recovery."""
    import tempfile
    from factoryvpn_common.backup_archive import unpack, load_key, pki_files
    from scripts.backup_coordinator import Coordinator

    root, host = Path(root), hostname(host)
    if root.exists() and any(root.iterdir()) or Path(env_file).exists():
        raise FileExistsError("Recovery requires an empty runtime and a new environment")
    with tempfile.TemporaryDirectory(prefix="factoryvpn-recovery-") as directory:
        work = Path(directory)
        unpack(archive, work, load_key(recovery_key), 4 * 1024**3)
        # Uses no coordinator state, Docker, or database.
        Coordinator.validate_runtime(None, work)
        for filename in pki_files(work / "runtime"):
            write_private(root / filename, (work / "runtime" / filename).read_bytes())
    initialize_environment(root, host, env_file)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".runtime"))
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init")
    init.add_argument("--host", required=True)
    init.add_argument("--env-file", type=Path, default=Path(".env"))
    recovery = actions.add_parser("recovery-init")
    recovery.add_argument("--host", required=True)
    recovery.add_argument("--env-file", type=Path, default=Path(".env"))
    recovery.add_argument("--archive", type=Path, required=True)
    recovery.add_argument("--recovery-key-file", type=Path, required=True)
    trust = actions.add_parser("refresh-server-trust")
    trust.add_argument("--services-stopped", action="store_true", required=True)
    renew = actions.add_parser("renew-server")
    renew.add_argument("--days", type=int, default=SERVER_DAYS)
    renew.add_argument("--services-stopped", action="store_true", required=True)
    args = parser.parse_args()
    if args.action == "init":
        initialize(args.root, args.host, args.env_file)
    elif args.action == "recovery-init":
        recovery_initialize(
            args.root, args.host, args.env_file, args.archive, args.recovery_key_file
        )
    elif args.action == "refresh-server-trust":
        with pki_lock(args.root):
            write_private(args.root / "server/ca.crt", trust_pem(args.root), replace=True)
    else:
        renew_server(args.root, args.days, replace=True)


if __name__ == "__main__":
    main()
