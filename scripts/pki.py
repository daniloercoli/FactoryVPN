"""Offline root ceremony and online CSR/import tools. Root keys never enter runtime."""

import argparse
import getpass
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from factoryvpn_common.certificates import (
    PEM,
    ROOT_DAYS,
    ISSUER_DAYS,
    certificate,
    fingerprint,
    key_bytes,
    load_authority,
    make_crl,
    name,
    new_key,
    pki_lock,
    public_bytes,
    read_bounded,
    require_ca,
    require_valid,
    verify_crl,
    write_private,
)


def root_init(folder, password, days=ROOT_DAYS):
    folder = Path(folder)
    if len(password) < 12:
        raise ValueError("Root passphrase must contain at least 12 bytes")
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError("Offline root directory must be empty")
    key = new_key()
    cn = "FactoryVPN Root " + str(uuid4())
    cert = certificate(cn, key.public_key(), key, name(cn), days=days, ca=True, path_length=1)
    write_private(folder / "root.key", key_bytes(key, password))
    write_private(folder / "root.crt", cert.public_bytes(PEM))
    write_private(folder / "state.json", json.dumps({"issued": [], "crl_number": 0}))
    root_crl(folder, password)
    return cert


def root_material(folder, password):
    folder = Path(folder)
    key = serialization.load_pem_private_key(read_bounded(folder / "root.key"), password)
    cert = x509.load_pem_x509_certificate(read_bounded(folder / "root.crt"))
    require_ca(cert, 1)
    require_valid(cert)
    cert.verify_directly_issued_by(cert)
    if public_bytes(key.public_key()) != public_bytes(cert.public_key()):
        raise ValueError("Root key mismatch")
    return key, cert


def root_crl(folder, password):
    folder = Path(folder)
    with pki_lock(folder):
        key, cert = root_material(folder, password)
        state = json.loads(read_bounded(folder / "state.json"))
        existing = folder / "root.crl"
        previous = x509.load_pem_x509_crl(read_bounded(existing)) if existing.exists() else None
        number = (
            max(
                state["crl_number"],
                verify_crl(previous, cert, fresh=False) if previous is not None else 0,
            )
            + 1
        )
        crl = make_crl(cert, key, list(previous) if previous else [], days=365, number=number)
        write_private(existing, crl.public_bytes(PEM), replace=existing.exists())
        state["crl_number"] = number
        write_private(folder / "state.json", json.dumps(state), replace=True)
    return crl


def prepare(root):
    root = Path(root)
    with pki_lock(root):
        key = new_key()
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(name("FactoryVPN Issuing " + str(uuid4())))
            .sign(key, hashes.SHA256())
        )
        pending = hashlib.sha256(public_bytes(key.public_key())).hexdigest()
        write_private(root / "pending" / (pending + ".key"), key_bytes(key))
        target = root / "pending" / (pending + ".csr")
        write_private(target, csr.public_bytes(PEM))
    return target


def sign_issuer(folder, password, csr_data, days=ISSUER_DAYS):
    folder = Path(folder)
    csr = x509.load_pem_x509_csr(csr_data)
    from cryptography.hazmat.primitives.asymmetric import ec

    if (
        not csr.is_signature_valid
        or not isinstance(csr.public_key(), ec.EllipticCurvePublicKey)
        or csr.public_key().curve.name != "secp256r1"
    ):
        raise ValueError("A signed P-256 CSR is required")
    attributes = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(attributes) != 1 or not attributes[0].value.startswith("FactoryVPN Issuing "):
        raise ValueError("Invalid issuer CSR identity")
    with pki_lock(folder):
        key, root_cert = root_material(folder, password)
        require_valid(root_cert, days=days + 1)
        cert = certificate(
            attributes[0].value,
            csr.public_key(),
            key,
            root_cert.subject,
            days=days,
            ca=True,
            path_length=0,
        )
        state = json.loads(read_bounded(folder / "state.json"))
        state["issued"].append(cert.public_bytes(PEM).decode())
        write_private(folder / "state.json", json.dumps(state), replace=True)
        crl = x509.load_pem_x509_crl(read_bounded(folder / "root.crl"))
        verify_crl(crl, root_cert)
        return {
            "root": root_cert.public_bytes(PEM).decode(),
            "root_crl": crl.public_bytes(PEM).decode(),
            "issuer": cert.public_bytes(PEM).decode(),
        }


def import_issuer(root, bundle, expected_root):
    root = Path(root)
    if set(bundle) != {"root", "root_crl", "issuer"}:
        raise ValueError("Public root, CRL and issuer only")
    if any(not isinstance(value, str) or "PRIVATE KEY" in value for value in bundle.values()):
        raise ValueError("Private keys are forbidden in public bundles")
    root_cert = x509.load_pem_x509_certificate(bundle["root"].encode())
    cert = x509.load_pem_x509_certificate(bundle["issuer"].encode())
    crl = x509.load_pem_x509_crl(bundle["root_crl"].encode())
    require_ca(root_cert, 1)
    require_ca(cert, 0)
    root_cert.verify_directly_issued_by(root_cert)
    cert.verify_directly_issued_by(root_cert)
    require_valid(root_cert)
    require_valid(cert)
    verify_crl(crl, root_cert)
    if (
        fingerprint(root_cert) != expected_root
        or cert.not_valid_after_utc > root_cert.not_valid_after_utc
    ):
        raise ValueError("Unexpected root or issuer validity")
    if crl.get_revoked_certificate_by_serial_number(cert.serial_number):
        raise ValueError("Issuer is revoked")
    issuer_id, root_id = fingerprint(cert), fingerprint(root_cert)
    pending = hashlib.sha256(public_bytes(cert.public_key())).hexdigest()
    with pki_lock(root):
        target = root / "pki-public/authority.json"
        data = (
            load_authority(root)[0]
            if target.exists()
            else {"version": 2, "generation": 0, "active": issuer_id, "roots": {}, "issuers": {}}
        )
        if issuer_id in data["issuers"]:
            raise FileExistsError("Issuer already imported")
        key_path = root / "pending" / (pending + ".key")
        key = serialization.load_pem_private_key(read_bounded(key_path), None)
        if public_bytes(key.public_key()) != public_bytes(cert.public_key()):
            raise ValueError("CSR key mismatch")
        if root_id in data["roots"]:
            old = x509.load_pem_x509_crl(data["roots"][root_id]["crl"].encode())
            if verify_crl(crl, root_cert) < verify_crl(old, root_cert, fresh=False):
                raise ValueError("Root CRL rollback")
        for item in data["issuers"].values():
            item["state"] = "retiring"
        data["roots"][root_id] = {"certificate": bundle["root"], "crl": bundle["root_crl"]}
        data["issuers"][issuer_id] = {
            "certificate": bundle["issuer"],
            "root_id": root_id,
            "state": "issuing",
        }
        from factoryvpn_common.certificates import MAX_AUTHORITIES

        if len(data["roots"]) > MAX_AUTHORITIES or len(data["issuers"]) > MAX_AUTHORITIES:
            raise ValueError("Authority registry is full")
        data["active"] = issuer_id
        data["generation"] += 1
        destination = root / "issuers" / (issuer_id + ".key")
        if not destination.exists():
            write_private(destination, key_bytes(key))
        write_private(target, json.dumps(data, sort_keys=True), replace=target.exists())
        key_path.unlink()
        (root / "pending" / (pending + ".csr")).unlink(missing_ok=True)
    return issuer_id


def import_root_crl(root, value):
    root = Path(root)
    crl = x509.load_pem_x509_crl(value)
    with pki_lock(root):
        data, roots, _ = load_authority(root)
        matches = [
            (key, cert)
            for key, cert in roots.items()
            if crl.issuer == cert.subject and crl.is_signature_valid(cert.public_key())
        ]
        if len(matches) != 1:
            raise ValueError("Unknown CRL root")
        root_id, cert = matches[0]
        previous = x509.load_pem_x509_crl(data["roots"][root_id]["crl"].encode())
        if verify_crl(crl, cert) <= verify_crl(previous, cert, fresh=False):
            raise ValueError("Root CRL must advance its number")
        data["roots"][root_id]["crl"] = crl.public_bytes(PEM).decode()
        data["generation"] += 1
        write_private(
            root / "pki-public/authority.json", json.dumps(data, sort_keys=True), replace=True
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("root-init")
    actions.add_parser("root-crl")
    actions.add_parser("prepare")
    sign = actions.add_parser("sign")
    sign.add_argument("--csr", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    sign.add_argument("--days", type=int, default=ISSUER_DAYS)
    imp = actions.add_parser("import")
    imp.add_argument("--bundle", type=Path, required=True)
    imp.add_argument("--root-fingerprint", required=True)
    crl = actions.add_parser("import-crl")
    crl.add_argument("--crl", type=Path, required=True)
    args = parser.parse_args()
    if args.action in {"root-init", "root-crl", "sign"}:
        password = getpass.getpass("Passphrase root offline: ").encode()
        if args.action == "root-init":
            if password != getpass.getpass("Conferma passphrase: ").encode():
                raise ValueError("Passphrases differ")
            print("Root fingerprint:", fingerprint(root_init(args.directory, password)))
        elif args.action == "root-crl":
            root_crl(args.directory, password)
        else:
            bundle = sign_issuer(args.directory, password, read_bounded(args.csr, 16384), args.days)
            write_private(args.output, json.dumps(bundle))
    elif args.action == "prepare":
        print(prepare(args.directory))
    elif args.action == "import":
        print(
            import_issuer(
                args.directory, json.loads(read_bounded(args.bundle)), args.root_fingerprint
            )
        )
    else:
        import_root_crl(args.directory, read_bounded(args.crl))


if __name__ == "__main__":
    main()
