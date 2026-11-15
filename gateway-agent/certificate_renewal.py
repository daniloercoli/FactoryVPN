"""Durable certificate-only renewal over the gateway's independent HTTPS identity."""

import base64
import json
import re
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from factoryvpn_common.certificates import (
    PEM,
    fingerprint,
    key_bytes,
    new_key,
    now,
    public_bytes,
    read_bounded,
    require_ca,
    require_valid,
    write_private,
    name,
)
from factoryvpn_common.protocol import peer_name


def section(profile, tag):
    matches = re.findall(rf"<{tag}>\s*(.*?)\s*</{tag}>", profile, re.S)
    if len(matches) != 1:
        raise ValueError("Expected one inline OpenVPN certificate section")
    return matches[0].encode() + b"\n"


def generation(profile):
    matches = re.findall(r"^# factoryvpn-trust-generation ([0-9]+)$", profile, re.M)
    if len(matches) != 1:
        raise ValueError("Missing profile trust generation")
    return int(matches[0])


def signed(key, message):
    return base64.b64encode(key.sign(message, ec.ECDSA(hashes.SHA256()))).decode()


def validated_profile(profile, bundle, key, gateway_id):
    """Only PEM material is accepted; remote data cannot add OpenVPN directives."""
    if (
        not isinstance(bundle["trust_generation"], int)
        or isinstance(bundle["trust_generation"], bool)
        or bundle["trust_generation"] < generation(profile)
    ):
        raise ValueError("Trust rollback refused")
    cert = x509.load_pem_x509_certificate(bundle["certificate_pem"].encode())
    issuer = x509.load_pem_x509_certificate(bundle["issuer_pem"].encode())
    roots = x509.load_pem_x509_certificates(bundle["trust_pem"].encode())
    if not 1 <= len(roots) <= 32 or fingerprint(issuer) != bundle["issuer_id"]:
        raise ValueError("Invalid issuer bundle")
    for root in roots:
        require_ca(root, 1)
        root.verify_directly_issued_by(root)
    require_ca(issuer, 0)
    matches = [root for root in roots if root.subject == issuer.issuer]
    if len(matches) != 1:
        raise ValueError("Unknown root")
    root = matches[0]
    issuer.verify_directly_issued_by(root)
    cert.verify_directly_issued_by(issuer)
    for item in (root, issuer, cert):
        require_valid(item)
    if not cert.not_valid_after_utc <= issuer.not_valid_after_utc <= root.not_valid_after_utc:
        raise ValueError("Certificate outlives its authority")
    names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if (
        fingerprint(cert) != bundle["certificate_sha256"]
        or public_bytes(cert.public_key()) != public_bytes(key.public_key())
        or len(names) != 1
        or names[0].value != peer_name("gateway", gateway_id)
        or cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or not cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature
        or list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        != [ExtendedKeyUsageOID.CLIENT_AUTH]
    ):
        raise ValueError("Certificate does not match this gateway and key")
    # Re-serialize parsed objects; embedded tags or trailing directives never reach disk.
    material = {
        "ca": b"".join(item.public_bytes(PEM) for item in roots),
        "extra-certs": issuer.public_bytes(PEM),
        "cert": cert.public_bytes(PEM),
        "key": key_bytes(key),
    }
    for tag, value in material.items():
        section(profile, tag)
        profile = re.sub(
            rf"<{tag}>.*?</{tag}>",
            lambda _: f"<{tag}>\n{value.decode()}</{tag}>",
            profile,
            flags=re.S,
        )
    return re.sub(
        r"^# factoryvpn-trust-generation [0-9]+$",
        f"# factoryvpn-trust-generation {bundle['trust_generation']}",
        profile,
        flags=re.M,
    )


class CertificateRenewer:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.OPENVPN_CONFIG)
        self.journal = self.path.with_name(self.path.name + ".renewal.json")
        self.staged = self.path.with_name(self.path.name + ".renewal.ovpn")
        self.next_check = 0
        self.was_awake = False

    async def request(self, method, suffix="", **kwargs):
        async with httpx.AsyncClient(
            timeout=self.config.API_TIMEOUT, follow_redirects=False
        ) as client:
            result = await client.request(
                method,
                self.config.SERVER_URL.rstrip("/") + "/api/v1/gateways/certificates" + suffix,
                headers={"X-Gateway-Key": self.config.GATEWAY_API_KEY, **kwargs.pop("headers", {})},
                **kwargs,
            )
            result.raise_for_status()
            return result.json()

    def save(self, journal):
        write_private(self.journal, json.dumps(journal), replace=self.journal.exists())

    def clear(self):
        # A crash while deleting is harmless: a confirmed job is replayable.
        self.staged.unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)

    def activation_pending(self):
        try:
            return self.journal.exists() and bool(
                json.loads(read_bounded(self.journal)).get("activation_attempted")
            )
        except (OSError, ValueError):
            return True

    async def tick(self, *, idle, awake):
        """Return True while activation must prevent starting a local VPN."""
        if awake and not self.was_awake:
            self.next_check = 0
        self.was_awake = awake
        journal = json.loads(read_bounded(self.journal)) if self.journal.exists() else None
        blocked = bool(journal and journal.get("activation_attempted"))
        if time.monotonic() < self.next_check:
            return blocked
        self.next_check = time.monotonic() + 60
        profile = read_bounded(self.path).decode()
        cert = x509.load_pem_x509_certificate(section(profile, "cert"))
        due = cert.not_valid_after_utc <= now() + timedelta(days=365)
        if not journal and not due and not awake:
            # SMS idle: local clock check only, no periodic network request.
            return False
        try:
            if not journal:
                status = await self.request("GET")
                if not status["renewal_due"] and not due:
                    self.next_check = time.monotonic() + 86400
                    return False
                if status["renewal"]:
                    raise ValueError("A renewal exists without its local private key")
                key = new_key()
                csr = (
                    x509.CertificateSigningRequestBuilder()
                    .subject_name(name(peer_name("gateway", self.config.GATEWAY_ID)))
                    .sign(key, hashes.SHA256())
                )
                journal = dict(
                    request_key=str(uuid4()),
                    previous_certificate_id=status["certificate_id"],
                    key=key_bytes(key).decode(),
                    csr_pem=csr.public_bytes(PEM).decode(),
                )
                self.save(journal)
            key = serialization.load_pem_private_key(journal["key"].encode(), None)
            if not journal.get("id"):
                job = await self.request(
                    "POST",
                    "/renewals",
                    headers={"Idempotency-Key": journal["request_key"]},
                    json={
                        "previous_certificate_id": journal["previous_certificate_id"],
                        "csr_pem": journal["csr_pem"],
                    },
                )
                journal["id"] = job["id"]
                self.save(journal)
            else:
                job = await self.request("GET", "/renewals/" + journal["id"])
            base = "/renewals/" + job["id"]
            if job["state"] in ("cancelled", "failed"):
                if blocked:
                    raise ValueError(
                        "Activated certificate state changed; re-provisioning required"
                    )
                self.clear()
                self.next_check = time.monotonic() + 3600
                return False
            if job["state"] == "challenge":
                old = serialization.load_pem_private_key(section(profile, "key"), None)
                await self.request(
                    "POST",
                    base + "/submit",
                    json={
                        "signature": signed(old, base64.b64decode(job["challenge"], validate=True))
                    },
                )
                return False
            if job["state"] == "queued":
                return False
            updated = validated_profile(profile, job["bundle"], key, self.config.GATEWAY_ID)
            # The complete new profile must be durable BEFORE server activation.
            write_private(self.staged, updated, replace=self.staged.exists())
            message = f"factoryvpn-certificate-activate-v1\n{job['id']}\n{job['bundle']['certificate_sha256']}\n".encode()
            proof = signed(key, message)
            if job["state"] == "ready":
                if not idle:
                    return False
                journal["activation_attempted"] = True
                self.save(journal)
                blocked = True
                try:
                    job = await self.request(
                        "POST",
                        base + "/activate",
                        json={
                            "signature": proof,
                            "certificate_sha256": job["bundle"]["certificate_sha256"],
                        },
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        journal["activation_attempted"] = False
                        self.save(journal)
                        return False
                    raise
            if job["state"] not in ("active", "confirmed") or not idle:
                return True
            write_private(self.path, read_bounded(self.staged), replace=True)
            await self.request("POST", base + "/confirm", json={"signature": proof})
            self.clear()
            self.next_check = time.monotonic() + 86400
            return False
        except httpx.HTTPError:
            # Keep journal/key on all transport failures and retry the same operation.
            return blocked
