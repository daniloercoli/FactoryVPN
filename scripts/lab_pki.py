"""Disposable test-only ceremony. Never use these roots for a real installation."""

from pathlib import Path
import tempfile
from factoryvpn_common.certificates import fingerprint
from scripts.pki import root_init, prepare, sign_issuer, import_issuer
from scripts.bootstrap import initialize as finalize, hostname


def initialize(root, host, env_file=None):
    root, host = Path(root), hostname(host)
    if (root / "server/server.key").exists() or (env_file and Path(env_file).exists()):
        raise FileExistsError("Existing laboratory installation")
    with tempfile.TemporaryDirectory(prefix="factoryvpn-offline-test-") as folder:
        password = b"disposable-laboratory-passphrase"
        ca = root_init(Path(folder), password)
        csr = prepare(root)
        bundle = sign_issuer(Path(folder), password, csr.read_bytes())
        import_issuer(root, bundle, fingerprint(ca))
    finalize(root, host, env_file)
