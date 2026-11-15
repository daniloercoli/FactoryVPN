"""Versioned, bounded, authenticated streaming archives. No executable input paths."""

import base64
import hashlib
import json
import os
import re
from pathlib import Path
import struct
import tarfile
from datetime import datetime
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"FVPNBK01"
CHUNK = 1024 * 1024
PKI_FILES = (
    "pki-public/authority.json",
    "server/ca.crt",
    "server/server.crt",
    "server/server.key",
    "server/chain.pem",
    "transport/tls-crypt.key",
    "crls/bundle.pem",
    "https/cert.pem",
    "https/key.pem",
)
PAYLOAD_FILES = ("database.dump", "configuration.json", *["runtime/" + p for p in PKI_FILES])
ARCHIVE_FILES = ("manifest.json", *PAYLOAD_FILES)


def pki_files(root):
    from factoryvpn_common.certificates import load_authority

    data, _, _ = load_authority(root)
    return (*PKI_FILES, *("issuers/" + key + ".key" for key in sorted(data["issuers"])))


def payload_files(directory):
    return (
        "database.dump",
        "configuration.json",
        *("runtime/" + p for p in pki_files(Path(directory) / "runtime")),
    )


def allowed_file(name):
    return (
        name in ARCHIVE_FILES
        or re.fullmatch(r"runtime/issuers/[0-9a-f]{64}\.key", name) is not None
    )


class BackupError(Exception):
    """Stable public error code, never a subprocess message or secret."""


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def load_key(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise BackupError("key_permissions")
    try:
        key = base64.b64decode(path.read_bytes().strip(), validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except ValueError:
        raise BackupError("invalid_key") from None


def file_hash(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            value.update(chunk)
    return value.hexdigest()


def schema_fingerprint():
    # The initial revision stays initial; compatibility tracks its actual DDL.
    import importlib.util

    source = Path(__file__).resolve().parents[1] / "migrations/versions/0001_initial.py"
    spec = importlib.util.spec_from_file_location("factoryvpn_backup_schema", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return hashlib.sha256("\n".join(module.STATEMENTS).encode()).hexdigest()


def cipher(key, salt):
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=MAGIC).derive(key))


def encrypt(source, destination, key):
    """Authenticated frame order and final empty frame reject truncation/append/reorder."""
    header = MAGIC + os.urandom(16) + os.urandom(8)
    aes = cipher(key, header[8:24])
    destination.write(header)
    counter = 0
    while True:
        data = source.read(CHUNK)
        number = struct.pack(">I", counter)
        encoded = aes.encrypt(header[24:] + number, data, header + number)
        destination.write(struct.pack(">I", len(encoded)))
        destination.write(encoded)
        if not data:
            return
        counter += 1


def decrypt(source, destination, key, maximum):
    from cryptography.exceptions import InvalidTag

    header = source.read(32)
    if len(header) != 32 or header[:8] != MAGIC:
        raise BackupError("invalid_archive")
    aes, total, counter = cipher(key, header[8:24]), 0, 0
    try:
        while True:
            size = source.read(4)
            if len(size) != 4:
                raise BackupError("truncated_archive")
            length = struct.unpack(">I", size)[0]
            if not 16 <= length <= CHUNK + 16:
                raise BackupError("invalid_archive")
            encoded = source.read(length)
            if len(encoded) != length:
                raise BackupError("truncated_archive")
            number = struct.pack(">I", counter)
            data = aes.decrypt(header[24:] + number, encoded, header + number)
            if not data:
                if source.read(1):
                    raise BackupError("invalid_archive")
                return
            total += len(data)
            if total > maximum:
                raise BackupError("archive_too_large")
            destination.write(data)
            counter += 1
    except InvalidTag:
        raise BackupError("authentication_failed") from None


def pack(directory, output, key, manifest):
    directory = Path(directory)
    files = payload_files(directory)
    manifest = {
        **manifest,
        "format": 2,
        "schema": schema_fingerprint(),
        "files": {name: file_hash(directory / name) for name in files},
    }
    private_write(directory / "manifest.json", json.dumps(manifest).encode())
    # Temporary plaintext is confined to a private job directory and always removed by caller.
    plain = directory / "payload.tar"
    with plain.open("xb") as stream:
        os.chmod(plain, 0o600)
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name in ("manifest.json", *files):
                path = directory / name
                if path.is_symlink() or not path.is_file():
                    raise BackupError("invalid_file")
                info = tarfile.TarInfo(name)
                info.size, info.mode = path.stat().st_size, 0o600
                with path.open("rb") as data:
                    archive.addfile(info, data)
    with plain.open("rb") as source, Path(output).open("xb") as target:
        os.chmod(output, 0o600)
        encrypt(source, target, key)
        target.flush()
        os.fsync(target.fileno())
    return manifest


def unpack(path, directory, key, maximum):
    directory = Path(directory)
    plain = directory / "payload.tar"
    with open(path, "rb") as source, plain.open("xb") as target:
        os.chmod(plain, 0o600)
        decrypt(source, target, key, maximum)
    seen, total = set(), 0
    try:
        with tarfile.open(plain, "r:") as archive:
            for member in archive:
                if (
                    not allowed_file(member.name)
                    or member.name in seen
                    or not member.isfile()
                    or member.pax_headers
                    or member.size < 0
                ):
                    raise BackupError("invalid_file")
                total += member.size
                if total > maximum or (member.name != "database.dump" and member.size > CHUNK):
                    raise BackupError("archive_too_large")
                seen.add(member.name)
                if len(seen) > len(ARCHIVE_FILES) + 32:
                    raise BackupError("invalid_file")
                target = directory / member.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as out, archive.extractfile(member) as source:
                    os.chmod(target, 0o600)
                    while data := source.read(CHUNK):
                        out.write(data)
        files = payload_files(directory)
        if seen != {"manifest.json", *files}:
            raise BackupError("missing_file")
        manifest = json.loads((directory / "manifest.json").read_bytes())
        UUID(manifest["installation_id"])
        if datetime.fromisoformat(manifest["created_at"]).tzinfo is None:
            raise BackupError("invalid_archive")
        if manifest.get("format") != 2 or manifest.get("schema") != schema_fingerprint():
            raise BackupError("incompatible_schema")
        if manifest.get("postgres_major") != 15:
            raise BackupError("incompatible_database")
        if manifest.get("files") != {name: file_hash(directory / name) for name in files}:
            raise BackupError("checksum_failed")
        return manifest
    except (tarfile.TarError, ValueError, TypeError, KeyError, FileNotFoundError):
        raise BackupError("invalid_archive") from None


def public_manifest(manifest):
    return {
        key: manifest[key]
        for key in ("format", "schema", "postgres_major", "created_at", "installation_id")
    }
