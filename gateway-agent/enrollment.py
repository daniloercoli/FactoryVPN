"""Arruolamento HTTPS iniziale, con installazione riprendibile prima della VPN."""

import asyncio
from importlib import import_module
import io
import json
import logging
import os
from pathlib import Path
import random
import ssl
from urllib.parse import urlsplit
from uuid import UUID
import zipfile

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from dotenv import dotenv_values
import httpx

from factoryvpn_common.certificates import fingerprint, public_bytes, read_bounded, write_private
from factoryvpn_common.protocol import peer_name

logger = logging.getLogger(__name__)
renewal = import_module("gateway-agent.certificate_renewal")
MAX_PACKAGE_BYTES = 256 * 1024
# Solo parametri locali: mai identità, server, chiavi o configurazione concordata.
LOCAL_SETTINGS = {
    "MODEM_PATH",
    "VPN_INTERFACE",
    "GATEWAY_WIFI_INTERFACE",
    "GATEWAY_HARDWARE_MODEL",
    "GATEWAY_HARDWARE_REVISION",
    "SSL_CERT_FILE",
}
PACKAGE_SETTINGS = {
    "SERVER_URL",
    "GATEWAY_ID",
    "GATEWAY_ACTIVATION_MODE",
    "GATEWAY_PHONE_NUMBER",
    "GATEWAY_API_KEY",
    "GATEWAY_SMS_KEY",
    "MODEM_PATH",
    "MAC_ADDRESS",
    "GATEWAY_WIFI_INTERFACE",
    "GATEWAY_HARDWARE_MODEL",
    "GATEWAY_HARDWARE_REVISION",
    "VPN_INTERFACE",
    "OPENVPN_CONFIG",
    "GATEWAY_POLL_INTERVAL",
    "GATEWAY_WIFI_CHECK_INTERVAL",
    "CONTROL_API_TIMEOUT_SECONDS",
    "GATEWAY_CONNECT_TIMEOUT_SECONDS",
}
PRESENTATION_SETTINGS = {
    "GATEWAY_ACTIVATION_MODE": "activation_mode",
    "GATEWAY_PHONE_NUMBER": "phone_number",
    "MAC_ADDRESS": "mac_address",
    "GATEWAY_IMEI": "imei",
    "GATEWAY_ICCID": "iccid",
}


class EnrollmentBlocked(RuntimeError):
    """Richiede intervento locale/HQ; non rigenerare identità o credenziali."""


def origin(value):
    parsed = urlsplit(value)
    parsed.port  # Valida anche una porta esplicita prima di salvare/usare l'origin.
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Enrollment requires an HTTPS origin without credentials or path")
    return value.rstrip("/")


def read_env(path):
    if not path.exists():
        return {}
    return {
        key: value
        for key, value in dotenv_values(
            stream=io.StringIO(read_bounded(path, MAX_PACKAGE_BYTES).decode()), interpolate=False
        ).items()
        if value is not None
    }


def env_text(values):
    # Compatibile sia con python-dotenv sia con EnvironmentFile di systemd.
    if any("\n" in value or "\r" in value or "\x00" in value for value in values.values()):
        raise ValueError("Invalid multiline gateway setting")
    return "".join(
        f'{key}="{value.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"\n'
        for key, value in values.items()
    )


class EnrollmentInstaller:
    def __init__(self, identity_path, *, client=None, sleep=asyncio.sleep, jitter=random.uniform):
        self.path = Path(identity_path)
        self.env_path = self.path.with_name("gateway.env")
        self.profile_path = self.path.with_name("gateway.ovpn")
        self.local_path = self.path.with_name("gateway.local.env")
        self.journal_path = self.path.with_name("enrollment-install.json")
        self.client, self.sleep, self.jitter = client, sleep, jitter
        self.identity = json.loads(read_bounded(self.path))
        if self.identity.get("format") != 1:
            raise ValueError("Unsupported enrollment identity format")
        self.gateway_id = str(UUID(self.identity["gateway_id"]))
        self.server_url = origin(self.identity["server_url"])
        if not self.identity.get("completed") and len(self.identity.get("enrollment_key", "")) < 32:
            raise ValueError("Missing individual enrollment credential")
        self.suffix = "/api/v1/gateway-enrollment/" + self.gateway_id

    def save_identity(self):
        write_private(self.path, json.dumps(self.identity), replace=True)

    def local_settings(self):
        return {**os.environ, **read_env(self.env_path), **read_env(self.local_path)}

    async def request(self, method, suffix="", **kwargs):
        response = await self.client.request(
            method,
            self.server_url + self.suffix + suffix,
            headers={"X-Enrollment-Key": self.identity.get("enrollment_key", "")},
            **kwargs,
        )
        response.raise_for_status()
        return response

    def candidate(self, payload, state):
        if len(payload) > MAX_PACKAGE_BYTES:
            raise ValueError("Enrollment package too large")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if (
                len(entries) != 3
                or {entry.filename for entry in entries}
                != {"gateway.env", "gateway.ovpn", "LEGGIMI.txt"}
                or any(entry.file_size > MAX_PACKAGE_BYTES for entry in entries)
            ):
                raise ValueError("Invalid enrollment package files")
            values = dict(
                dotenv_values(
                    stream=io.StringIO(archive.read("gateway.env").decode()), interpolate=False
                )
            )
            profile = archive.read("gateway.ovpn").decode()
        if (
            not set(values) <= PACKAGE_SETTINGS
            or any(value is None for value in values.values())
            or values.get("GATEWAY_ID") != self.gateway_id
            or origin(values.get("SERVER_URL", "")) != self.server_url
        ):
            raise ValueError("Package identity or server differs from the factory identity")
        for key, field in PRESENTATION_SETTINGS.items():
            if field in ("imei", "iccid"):
                continue
            if values.get(key, "") != (state.get(field) or ""):
                raise ValueError("Package configuration differs from the approved candidate")
        if (
            len(values.get("GATEWAY_API_KEY", "")) < 32
            or len(values.get("GATEWAY_SMS_KEY", "")) < 32
        ):
            raise ValueError("Invalid operational credentials")
        if (
            state["job"]["configuration_version"] != state["configuration_version"]
            or not isinstance(state["configuration_version"], int)
            or isinstance(state["configuration_version"], bool)
            or state["configuration_version"] < 1
        ):
            raise ValueError("Package belongs to a stale configuration version")
        self.validate_profile(profile)
        local = self.local_settings()
        values.update({key: local[key] for key in LOCAL_SETTINGS if key in local})
        values["OPENVPN_CONFIG"] = str(self.profile_path)
        return {
            "format": 1,
            "gateway_id": self.gateway_id,
            "job_id": str(UUID(state["job"]["id"])),
            "configuration_version": state["job"]["configuration_version"],
            "env": env_text(values),
            "profile": profile,
        }

    def validate_profile(self, profile):
        cert = x509.load_pem_x509_certificate(renewal.section(profile, "cert"))
        issuer = x509.load_pem_x509_certificate(renewal.section(profile, "extra-certs"))
        key = serialization.load_pem_private_key(renewal.section(profile, "key"), password=None)
        renewal.validated_profile(
            profile,
            {
                "certificate_pem": renewal.section(profile, "cert").decode(),
                "issuer_pem": renewal.section(profile, "extra-certs").decode(),
                "issuer_id": fingerprint(issuer),
                "certificate_sha256": fingerprint(cert),
                "trust_pem": renewal.section(profile, "ca").decode(),
                "trust_generation": renewal.generation(profile),
            },
            key,
            self.gateway_id,
        )

    def install(self, journal):
        if journal.get("format") != 1 or journal.get("gateway_id") != self.gateway_id:
            raise EnrollmentBlocked("Installation journal belongs to another gateway")
        # Il journal è già fsync: un'interruzione fra queste scritture ripete entrambe.
        write_private(self.env_path, journal["env"], replace=self.env_path.exists())
        write_private(self.profile_path, journal["profile"], replace=self.profile_path.exists())

    async def registered(self):
        values = read_env(self.env_path)
        if not values.get("GATEWAY_API_KEY") or not self.profile_path.exists():
            return False
        self.validate_profile(read_bounded(self.profile_path).decode())
        if (
            values.get("GATEWAY_ID") != self.gateway_id
            or origin(values.get("SERVER_URL", "")) != self.server_url
        ):
            raise EnrollmentBlocked("Operational files belong to another gateway or server")
        response = await self.client.post(
            self.server_url + "/api/v1/gateways/register",
            headers={"X-Gateway-Key": values["GATEWAY_API_KEY"]},
            json={
                "gateway_id": self.gateway_id,
                "activation_mode": values.get("GATEWAY_ACTIVATION_MODE", "sms"),
                "phone_number": values.get("GATEWAY_PHONE_NUMBER") or None,
                "mac_address": values.get("MAC_ADDRESS") or None,
            },
        )
        if response.status_code == 401:
            return False
        if response.status_code == 409:
            raise EnrollmentBlocked("Operational configuration rejected; HQ intervention required")
        response.raise_for_status()
        result = response.json()
        if result.get("id") != self.gateway_id:
            raise EnrollmentBlocked("Registration returned a different gateway")
        return True

    def complete(self):
        # Prima il marcatore durabile: al riavvio non si riusa il segreto consumato.
        self.identity = {
            "format": 1,
            "gateway_id": self.gateway_id,
            "server_url": self.server_url,
            "completed": True,
        }
        self.save_identity()
        self.journal_path.unlink(missing_ok=True)
        self.load_installed()

    def load_installed(self):
        if not self.env_path.is_file() or not self.profile_path.is_file():
            raise EnrollmentBlocked("Completed enrollment files are missing; use HQ recovery")
        values = read_env(self.env_path)
        try:
            if values.get("GATEWAY_ID") != self.gateway_id:
                raise ValueError("Different gateway identity")
            # Dopo completamento HQ può consegnare un nuovo origin HTTPS; il
            # vecchio segreto iniziale non viene più inviato a nessun server.
            origin(values.get("SERVER_URL", ""))
            profile = read_bounded(self.profile_path).decode()
            cert = x509.load_pem_x509_certificate(renewal.section(profile, "cert"))
            key = serialization.load_pem_private_key(renewal.section(profile, "key"), password=None)
            names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if (
                len(names) != 1
                or names[0].value != peer_name("gateway", self.gateway_id)
                or public_bytes(cert.public_key()) != public_bytes(key.public_key())
            ):
                raise ValueError("Profile identity or key mismatch")
            # Non rifiutare qui le date scadute: il rinnovo HTTPS deve poterle
            # recuperare. Registrazione/rinnovo verificano lo stato operativo.
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise EnrollmentBlocked(
                "Installed files do not match the completed gateway identity"
            ) from None
        # Il processo VPN deve aprire il profilo che abbiamo appena verificato.
        values["OPENVPN_CONFIG"] = str(self.profile_path)
        os.environ.update(values)
        # Solo impostazioni hardware dopo il completamento, mai vecchi dati operativi.
        os.environ.update(
            {k: v for k, v in read_env(self.local_path).items() if k in LOCAL_SETTINGS}
        )

    async def presentation(self):
        # Il pacchetto candidato può essere scaduto o superato da HQ. Non usare
        # gateway.env (nemmeno ereditato da systemd) come nuova presentazione.
        values = read_env(self.local_path)
        result = {
            field: values[key] for key, field in PRESENTATION_SETTINGS.items() if values.get(key)
        }
        mode = result.get("activation_mode")
        if mode != "wifi" and Path(values.get("MODEM_PATH", "/dev/ttyUSB0")).exists():

            def inventory():
                modem = import_module("gateway-agent.sms_handler").SMSHandler(
                    modem_path=values.get("MODEM_PATH", "/dev/ttyUSB0")
                )
                try:
                    return modem.inventory()
                finally:
                    modem.close()

            try:
                detected = await asyncio.to_thread(inventory)
                result.update(
                    {key: detected[key] for key in ("imei", "iccid") if detected.get(key)}
                )
            except Exception:
                logger.info("Enrollment modem inventory unavailable; optional fields omitted")
        return result

    async def step(self):
        if self.identity.get("completed"):
            self.load_installed()
            self.journal_path.unlink(missing_ok=True)
            return True
        journal = (
            json.loads(read_bounded(self.journal_path)) if self.journal_path.exists() else None
        )
        if journal:
            # HQ può avere copiato entrambi i file di un altro ZIP già attivato.
            # Una coppia parziale invece deve essere riparata dal journal.
            replaced = (
                self.env_path.exists()
                and self.profile_path.exists()
                and read_bounded(self.env_path).decode() != journal["env"]
                and read_bounded(self.profile_path).decode() != journal["profile"]
            )
            if replaced and await self.registered():
                self.complete()
                return True
            self.install(journal)
        # Copre conferma persa e presa in carico manuale con file già attivati da HQ.
        if await self.registered():
            self.complete()
            return True
        if journal:
            try:
                response = await self.request(
                    "POST",
                    f"/package/{journal['job_id']}/confirm",
                    json={"configuration_version": journal["configuration_version"]},
                )
                result = response.json()
                if (
                    not result.get("activated")
                    or result.get("gateway_id") != self.gateway_id
                    or result.get("configuration_version") != journal["configuration_version"]
                ):
                    raise EnrollmentBlocked("Invalid enrollment confirmation")
                if await self.registered():
                    self.complete()
                    return True
                raise EnrollmentBlocked("Activated package cannot authenticate")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
                # Candidato scaduto/superato: mai avviare i file rimasti sul disco.
                self.journal_path.unlink(missing_ok=True)
        state = (await self.request("GET")).json()
        if state.get("id") != self.gateway_id:
            raise EnrollmentBlocked("Enrollment returned a different identity")
        if state["state"] in ("blocked", "completed"):
            raise EnrollmentBlocked("Enrollment is blocked or completed without usable local files")
        if state["delivery_mode"] == "manual" or state["state"] == "manual":
            return False
        presented = await self.presentation()
        previous = self.identity.get("presented", {})
        changes = {key: value for key, value in presented.items() if previous.get(key) != value}
        if changes:
            state = (await self.request("PUT", json=changes)).json()
            self.identity["presented"] = {**previous, **changes}
            self.save_identity()
        if state["state"] not in ("approved", "preparing", "ready"):
            return False
        state = (
            await self.request(
                "POST", "/package", json={"configuration_version": state["configuration_version"]}
            )
        ).json()
        if not state.get("job") or not state["job"]["can_download"]:
            return False
        job = state["job"]
        response = await self.request("POST", f"/package/{job['id']}/download")
        journal = self.candidate(response.content, state)
        write_private(self.journal_path, json.dumps(journal), replace=self.journal_path.exists())
        self.install(journal)
        # Conferma nel ciclo successivo: la coppia è già durabile anche in caso di crash.
        return False

    async def run(self):
        failures = 0
        while True:
            try:
                if await self.step():
                    return
                failures = 0
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in (401, 403, 404):
                    raise EnrollmentBlocked(
                        "Enrollment credential rejected; HQ intervention required"
                    ) from None
                if code not in (409, 408, 429) and not 500 <= code <= 599:
                    raise EnrollmentBlocked("Enrollment request rejected") from None
                failures = min(failures + 1, 5)
                logger.warning("Enrollment waiting after HTTP %s", code)
            except httpx.TransportError:
                failures = min(failures + 1, 5)
                logger.warning("Enrollment HTTPS temporarily unavailable")
            except (ValueError, KeyError, zipfile.BadZipFile):
                raise EnrollmentBlocked("Invalid enrollment response or local identity") from None
            await self.sleep(self.jitter(0.8, 1.2) * min(900, 60 * 2**failures))


async def bootstrap(identity_path=None):
    path = Path(
        identity_path or os.getenv("FACTORYVPN_ENROLLMENT_FILE", "/etc/factoryvpn/enrollment.json")
    )
    if not path.exists():
        return False  # Installazione manuale: stesso avvio e stessi file di prima.
    try:
        installer = EnrollmentInstaller(path)
        certificate = installer.local_settings().get("SSL_CERT_FILE")
        verify = ssl.create_default_context(cafile=certificate) if certificate else True
    except (ValueError, KeyError, OSError):
        raise EnrollmentBlocked(
            "Factory identity or HTTPS trust is invalid or unreadable"
        ) from None
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, verify=verify) as client:
        installer.client = client
        await installer.run()
    return True
