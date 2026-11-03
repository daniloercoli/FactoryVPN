"""Persistent, isolated Docker lab with real gateways, VPN clients and simulated LANs.

State stays in .factoryvpn-lab, or .factoryvpn-simulator for the Alpine/SMS demo.
Uses production services and provisioning APIs; email, LAN hosts and optional SMS
transport are local fixtures. Both state directories are ignored by Git/builds.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import time
from uuid import uuid4
import zipfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from dotenv import dotenv_values, set_key
import httpx

from scripts.bootstrap import initialize, load_ca, write_private
from scripts.simulator_sms import PHONE
from scripts.backup_coordinator import Coordinator

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / ".factoryvpn-lab"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def command(args, *, data=None, timeout=180):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        # Do not echo command arguments or environment, which may contain secrets.
        raise RuntimeError(
            f"Lab command failed ({result.returncode}): "
            + result.stderr.decode(errors="replace")[-3000:]
        )
    return result.stdout


def wait_for(description, probe, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(1)
    raise RuntimeError("Timed out: " + description)


class Lab:
    def __init__(self, root=LAB):
        self.root = Path(root)
        self.state_path = self.root / "lab.json"
        self.state = json.loads(self.state_path.read_text())
        if self.state.get("purpose") != "factoryvpn-local-lab" or not re.fullmatch(
            r"factoryvpn-lab-[a-f0-9]{10}", self.state.get("project", "")
        ):
            raise RuntimeError("Not a FactoryVPN lab; refusing to operate on this project.")
        self.compose = [
            "docker",
            "compose",
            "-p",
            self.state["project"],
            "--project-directory",
            str(self.root),
            "-f",
            str(self.root / "docker-compose.yml"),
        ]
        self.context = ssl.create_default_context(cafile=str(self.root / ".runtime/ca/ca.crt"))

    def activation_mode(self, number):
        return "sms" if self.state.get("simulator") and number == 1 else "wifi"

    def persist(self):
        save(self.state_path, self.state)

    def docker(self, *args, **kwargs):
        return command([*self.compose, *args], **kwargs)

    def peer(self, action, number, protocol="tcp"):
        return (
            self.docker(
                "exec",
                "-T",
                "peers",
                "python",
                "-m",
                "scripts.local_lab_peers",
                action,
                str(number),
                "--protocol",
                protocol,
            )
            .decode()
            .strip()
        )

    def client(self):
        return httpx.Client(
            base_url=self.state["origin"],
            verify=self.context,
            trust_env=False,
            timeout=15,
            headers={"Origin": self.state["origin"], "X-FactoryVPN-Portal": "1"},
        )

    def call(self, client, method, path, expected=200, **kwargs):
        response = client.request(method, "/api/v1" + path, **kwargs)
        if response.status_code != expected:
            raise RuntimeError(
                f"{method} {path}: expected {expected}, got {response.status_code}; "
                + response.text[:500]
            )
        return response

    def login(self, account):
        client = self.client()
        result = self.call(
            client,
            "POST",
            "/web/login",
            json={"username": account["username"], "password": account["password"]},
        ).json()
        if result.get("challenge_required"):
            with httpx.Client(trust_env=False) as mail:
                code = mail.get(self.state["mail_origin"] + "/messages/" + account["email"]).json()[
                    "code"
                ]
            result = self.call(
                client,
                "POST",
                "/web/login/verify",
                json={"challenge_token": result["challenge_token"], "code": code},
            ).json()
        client.headers["X-CSRF-Token"] = result["csrf_token"]
        return client

    def deliver(self, client, response, number, kind):
        job = response.json()["id"]

        def ready():
            state = self.call(client, "GET", "/provisioning/jobs/" + job).json()
            if state["state"] in ("failed", "expired", "cancelled"):
                raise RuntimeError(f'Provisioning {kind} failed: {state["error_code"]}')
            return state["state"] == "ready"

        wait_for("certificate issuance", ready)
        payload = self.call(client, "POST", f"/provisioning/jobs/{job}/download").content
        folder = self.root / "peers" / f"site{number}"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if kind == "gateway":
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                for name in ("gateway.env", "gateway.ovpn"):
                    write_private(
                        folder / name, archive.read(name), replace=(folder / name).exists()
                    )
        else:
            write_private(
                folder / "technician.ovpn", payload, replace=(folder / "technician.ovpn").exists()
            )
        self.call(client, "POST", f"/provisioning/jobs/{job}/activate")

    def provision(self):
        credentials = json.loads((self.root / "credentials.json").read_text())
        if not self.state.get("admin_created"):
            account = credentials["admin"]
            self.docker(
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "scripts.create_admin",
                account["username"],
                "--email",
                account["email"],
                "--password-stdin",
                data=(account["password"] + "\n").encode(),
            )
            self.state["admin_created"] = True
            self.persist()
        with closing(self.login(credentials["admin"])) as admin:
            for number in (1, 2):
                site = self.state["sites"].setdefault(str(number), {})
                account = credentials[f"tech{number}"]
                if "group_id" not in site:
                    site["group_id"] = self.call(
                        admin,
                        "POST",
                        "/admin/groups",
                        201,
                        json={"name": f"FactoryVPN laboratorio {number}"},
                    ).json()["id"]
                    self.persist()
                if "technician_id" not in site:
                    site["technician_id"] = self.call(
                        admin,
                        "POST",
                        "/admin/users",
                        201,
                        json={
                            **account,
                            "full_name": f"Tecnico laboratorio {number}",
                            "group_ids": [site["group_id"]],
                            "permissions": ["connect"],
                        },
                    ).json()["id"]
                    self.persist()
                if "gateway_id" not in site:
                    identifier = str(uuid4())
                    response = self.call(
                        admin,
                        "POST",
                        "/provisioning/gateways",
                        202,
                        headers={"Idempotency-Key": str(uuid4())},
                        json={
                            "gateway_id": identifier,
                            "group_id": site["group_id"],
                            "display_name": f"Gateway laboratorio {number}",
                            "activation_mode": self.activation_mode(number),
                            "phone_number": (
                                PHONE if self.activation_mode(number) == "sms" else None
                            ),
                            "mac_address": (
                                f"02:00:00:00:00:{number:02x}"
                                if self.activation_mode(number) == "wifi"
                                else None
                            ),
                            "lan_interface": "eth0",
                            "lan_address": "192.168.1.1",
                            "lan_cidr": "192.168.1.0/24",
                        },
                    )
                    self.deliver(admin, response, number, "gateway")
                    site["gateway_id"] = identifier
                    self.persist()
                if not site.get("technician_provisioned"):
                    with closing(self.login(account)) as technician:
                        response = self.call(
                            technician,
                            "POST",
                            f'/provisioning/peers/technician/{site["technician_id"]}/prepare',
                            202,
                            headers={"Idempotency-Key": str(uuid4())},
                            json={"expected_certificate_id": None},
                        )
                        self.deliver(technician, response, number, "technician")
                    site["technician_provisioned"] = True
                    self.persist()

    def connect(self, client, number):
        site = self.state["sites"][str(number)]
        result = self.call(
            client,
            "POST",
            "/sessions/",
            headers={"Idempotency-Key": str(uuid4())},
            json={"gateway_id": site["gateway_id"]},
        ).json()
        identifier = result["session_id"]
        site["session_id"] = identifier
        self.persist()

        def detail():
            value = self.call(client, "GET", "/sessions/" + identifier).json()
            if value["status"] in ("FAILED", "CLOSED", "EXPIRED"):
                raise RuntimeError(
                    "Session ended before connecting: " + str(value.get("error_reason"))
                )
            return value

        if self.activation_mode(number) == "sms":
            self.receive_pending_sms(client, number, identifier)
        wait_for("gateway ready", lambda: detail().get("gateway_ready_at"))
        if self.activation_mode(number) == "sms":
            wait_for(
                "SMS acknowledged by agent",
                lambda: json.loads(self.peer("sms-status", number))["delivered"] == 0,
            )
        self.peer("client-start", number)
        wait_for("session ACTIVE", lambda: detail()["status"] == "ACTIVE")
        return identifier

    def receive_pending_sms(self, client, number, identifier):
        """Prove provider acceptance alone cannot start a tunnel, then deliver."""
        wait_for(
            "SMS accepted by local provider",
            lambda: json.loads(self.peer("sms-status", number))["pending"] > 0,
        )
        time.sleep(6)  # Longer than one normal agent polling interval.
        detail = self.call(client, "GET", "/sessions/" + identifier).json()
        if detail.get("gateway_ready_at") or detail.get("activation_received_at"):
            raise RuntimeError("SMS gateway activated before simulated delivery")
        interfaces = json.loads(
            self.docker(
                "exec",
                "-T",
                "peers",
                "ip",
                "-n",
                f"gw{number}",
                "-j",
                "addr",
                "show",
            )
        )
        if any(interface["ifname"] == "tun0" for interface in interfaces):
            raise RuntimeError("Gateway tunnel exists before simulated SMS delivery")
        if self.peer("query", number) != "blocked":
            raise RuntimeError("LAN reachable before simulated SMS delivery")
        print("PASS: provider accepted SMS; gateway still asleep and LAN blocked", flush=True)
        self.peer("sms", number)

    def disconnect(self, client, number):
        identifier = self.state["sites"][str(number)].get("session_id")
        if identifier:
            self.call(client, "DELETE", "/sessions/" + identifier)
            wait_for(
                "session cleanup",
                lambda: self.call(client, "GET", "/sessions/" + identifier)
                .json()
                .get("cleaned_at"),
            )
        self.peer("client-stop", number)

    def verify(self):
        credentials = json.loads((self.root / "credentials.json").read_text())
        clients = [self.login(credentials[f"tech{number}"]) for number in (1, 2)]
        checks = []
        worker_paused = False

        def record(label):
            checks.append(label)
            print("PASS: " + label, flush=True)

        def traffic(number, expected):
            for protocol in ("tcp", "udp"):
                observed = self.peer("query", number, protocol)
                if observed != expected:
                    raise RuntimeError(
                        f"{protocol} site {number}: expected {expected}, got {observed}"
                    )

        try:
            for number, client in enumerate(clients, 1):
                other = self.state["sites"][str(3 - number)]["gateway_id"]
                self.call(client, "POST", "/sessions/", 404, json={"gateway_id": other})
                self.disconnect(client, number)
                traffic(number, "blocked")
            record("No LAN traffic before a session; other group gateway denied")
            for number, client in enumerate(clients, 1):
                self.connect(client, number)
                traffic(number, f"site{number}")
            record("Two real OpenVPN sessions reach distinct TCP/UDP hosts at 192.168.1.10")
            if self.state.get("simulator"):
                record("SMS waits for manual delivery and modem ACK; Wi-Fi starts without SMS")
            self.disconnect(clients[0], 1)
            traffic(1, "blocked")
            traffic(2, "site2")
            record("Closing site 1 blocks its traffic and preserves site 2")
            self.connect(clients[0], 1)
            traffic(1, "site1")
            record("A new session reuses cleaned resources successfully")
            self.call(clients[0], "POST", "/web/logout")
            wait_for("logout stops site 1 traffic", lambda: self.peer("query", 1) == "blocked", 15)
            traffic(1, "blocked")
            traffic(2, "site2")
            record("Portal logout stops its VPN and preserves the other technician")
            # Freeze the worker but retain its mount/network namespaces. Stopping
            # the container would destroy them and would not prove lease expiry.
            self.docker("pause", "session-worker")
            worker_paused = True
            started = time.monotonic()
            wait_for(
                "worker-loss kernel lease expiry", lambda: self.peer("query", 2) == "blocked", 35
            )
            elapsed = round(time.monotonic() - started, 2)
            if elapsed > 32.5:  # 30s lease plus the 2s probe timeout and scheduling.
                raise RuntimeError(f"Worker lease exceeded the expected bound: {elapsed}s")
            traffic(2, "blocked")
            record(
                f"Worker loss blocks traffic through expiring kernel policy ({elapsed}s observed)"
            )
            self.docker("unpause", "session-worker")
            worker_paused = False
            wait_for(
                "worker restart restores authorized traffic",
                lambda: self.peer("query", 2) == "site2",
            )
            traffic(2, "site2")
            record("Worker resume restores the still-authorized session")
            self.docker("restart", "-t", "0", "session-worker")
            wait_for(
                "worker restart rebuilds session network",
                lambda: self.peer("query", 2) == "site2",
            )
            traffic(2, "site2")
            record("Worker container restart rebuilds the session network")
            save(
                self.root / "report.json",
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "checks": checks,
                    "worker_loss_seconds": elapsed,
                    "git_head": command(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
                    .decode()
                    .strip(),
                    "working_tree_changes": bool(
                        command(["git", "-C", str(ROOT), "status", "--porcelain"]).strip()
                    ),
                },
            )
        finally:
            if worker_paused:
                self.docker("unpause", "session-worker")
            self.docker("start", "session-worker")
            for client in clients:
                client.close()
            # Fresh accesses can close owned sessions even after the opening browser logged out.
            for number in (1, 2):
                with closing(self.login(credentials[f"tech{number}"])) as client:
                    self.disconnect(client, number)


def initialize_lab(root=LAB, *, simulator=False):
    root = Path(root)
    if root.exists():
        if not (root / "lab.json").exists():
            raise RuntimeError(
                "Lab directory already exists without its marker; refusing to overwrite."
            )
        lab = Lab(root)
        if bool(lab.state.get("simulator")) != simulator:
            raise RuntimeError("Lab mode does not match the requested environment")
        return lab
    root.mkdir(mode=0o700)
    origin, mail_port = f"https://localhost:{port()}", port()
    name = "factoryvpn-lab-" + uuid4().hex[:10]
    initialize(root / ".runtime", "localhost", root / ".env")
    # Add the internal proxy name to the lab certificate; every client verifies this CA.
    cert = x509.load_pem_x509_certificate((root / ".runtime/https/cert.pem").read_bytes())
    ca_key, _ = load_ca(root / ".runtime")
    builder = (
        x509.CertificateBuilder()
        .subject_name(cert.subject)
        .issuer_name(cert.issuer)
        .public_key(cert.public_key())
        .serial_number(cert.serial_number)
        .not_valid_before(cert.not_valid_before_utc)
        .not_valid_after(cert.not_valid_after_utc)
    )
    for extension in cert.extensions:
        value = (
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.DNSName("nginx")])
            if isinstance(extension.value, x509.SubjectAlternativeName)
            else extension.value
        )
        builder = builder.add_extension(value, extension.critical)
    write_private(
        root / ".runtime/https/cert.pem",
        builder.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM),
        replace=True,
    )
    sms_key = secrets.token_urlsafe(32) if simulator else ""
    for key, value in {
        "COMPOSE_PROJECT_NAME": name,
        "PORTAL_PUBLIC_URL": origin,
        "OPENVPN_SERVER_HOST": "openvpn",
        "OPENVPN_SERVER_PORT": "1194",
        "SMTP_HOST": "test-mail",
        "SMTP_PORT": "2525",
        "SMTP_SECURITY": "plain",
        "SMTP_FROM": "factoryvpn@example.test",
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "SMTP_CA_FILE": "",
        "SSO_PROVIDERS": "{}",
        "SSO_PUBLIC_URL": "",
        "SSO_TOKEN_KEY": "",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_FROM": "",
        "SMS_PROVIDER": "generic",
        "SMS_PROVIDER_URL": "http://test-sms:8080/send" if simulator else "",
        "SMS_PROVIDER_API_KEY": sms_key,
    }.items():
        set_key(str(root / ".env"), key, value)
    Coordinator(root).initialize()
    env = {**os.environ, **dotenv_values(root / ".env")}
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            name,
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(root / ".env"),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        check=True,
        timeout=30,
    )
    config = json.loads(result.stdout)
    services = config["services"]
    for service in services.values():
        service["restart"] = "no"
    services["nginx"]["ports"] = [
        {"target": 443, "published": origin.rsplit(":", 1)[1], "host_ip": "127.0.0.1"}
    ]
    services["openvpn"]["ports"] = []  # Peers are inside the isolated Docker network.
    for volume in services["api"]["volumes"]:
        if volume["target"] in ("/backup-control", "/backups"):
            volume["source"] = str(
                root / (".backup-control" if volume["target"] == "/backup-control" else ".backups")
            )
    services["api"]["image"] = name + "-api"
    services["peers"] = {
        "build": {
            "context": str(ROOT),
            "dockerfile": "docker/Dockerfile.network",
            "target": "test",
        },
        "command": ["python", "-u", "-m", "scripts.local_lab_peers", "serve"],
        # This disposable network simulator owns nested gateways/LANs. Production
        # server containers retain their original, narrower capabilities.
        "privileged": True,
        "devices": ["/dev/net/tun:/dev/net/tun"],
        # Namespace bind-mount files must disappear with the container's
        # namespaces; otherwise a restart leaves unusable /run/netns entries.
        "tmpfs": ["/run/netns:mode=755", "/etc/netns:mode=755"],
        "environment": {"FACTORYVPN_LOCAL_LAB": "1"},
        "volumes": [str(root / "peers") + ":/lab"],
        "networks": ["backend"],
        "healthcheck": {
            "test": ["CMD", "test", "-f", "/lab/ready"],
            "interval": "2s",
            "retries": 20,
        },
    }
    services["test-mail"] = {
        "image": name + "-api",
        "entrypoint": [
            "python",
            "-m",
            "uvicorn",
            "portal_mail:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
            "--no-access-log",
        ],
        "volumes": [str(ROOT / "tests/portal_mail.py") + ":/tmp/portal_mail.py:ro"],
        "environment": {
            "PYTHONPATH": "/tmp",
            "SMTP_PORT": "2525",
            "FACTORYVPN_TEST_DATABASE_URL": "isolated-lab",
            "FACTORYVPN_TEST_SMTP_BIND": "0.0.0.0",
        },
        "ports": [f"127.0.0.1:{mail_port}:8001"],
        "networks": ["backend"],
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')",
            ],
            "interval": "2s",
            "retries": 20,
        },
    }
    if simulator:
        # Both the transport and gateway run on Alpine; no public SMS endpoint.
        services["peers"]["build"] = {
            "context": str(ROOT),
            "dockerfile": "docker/Dockerfile.simulator",
        }
        services["peers"]["image"] = name + "-peers"
        (root / "sms").mkdir(mode=0o700)
        sms_volume = str(root / "sms") + ":/sms"
        services["peers"]["volumes"].append(sms_volume)
        services["test-sms"] = {
            "image": name + "-peers",
            "command": ["python", "-m", "scripts.simulator_sms"],
            "environment": {
                "FACTORYVPN_LOCAL_LAB": "1",
                "FACTORYVPN_SIMULATOR_SMS_KEY": sms_key,
            },
            "volumes": [sms_volume],
            "networks": ["backend"],
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')",
                ],
                "interval": "2s",
                "retries": 20,
            },
        }
    save(root / "docker-compose.yml", config)
    save(
        root / "credentials.json",
        {
            role: {
                "username": "lab-" + role,
                "email": f"lab-{role}@example.test",
                "password": secrets.token_urlsafe(24),
            }
            for role in ("admin", "tech1", "tech2")
        },
    )
    (root / "peers").mkdir(mode=0o700)
    write_private(root / "peers/ca.crt", (root / ".runtime/ca/ca.crt").read_bytes())
    save(
        root / "lab.json",
        {
            "purpose": "factoryvpn-local-lab",
            "project": name,
            "origin": origin,
            "mail_origin": f"http://127.0.0.1:{mail_port}",
            "sites": {},
            "simulator": simulator,
        },
    )
    return Lab(root)


def main(root=LAB, *, simulator=False):
    root = Path(root)
    parser = argparse.ArgumentParser(
        description=(
            "FactoryVPN Alpine simulator: site 1 SMS/IMEI, site 2 Wi-Fi/MAC. "
            "State: .factoryvpn-simulator. Use sms to deliver queued portal messages."
            if simulator
            else __doc__
        )
    )
    parser.add_argument(
        "action",
        choices=[
            "up",
            "verify",
            "connect",
            "disconnect",
            "client-start",
            "client-stop",
            "query",
            "status",
            "down",
            "sms",
            "versions",
        ],
    )
    parser.add_argument("--site", type=int, choices=[1, 2], default=1)
    args = parser.parse_args()
    lab = initialize_lab(root, simulator=simulator) if args.action == "up" else Lab(root)
    if bool(lab.state.get("simulator")) != simulator:
        parser.error("Lab mode does not match the requested environment")
    if args.action == "up":
        print("Building isolated FactoryVPN lab…", flush=True)
        lab.docker("build", timeout=600)
        lab.docker(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "120",
            "db",
            "api",
            "nginx",
            "openvpn",
            "session-worker",
            "provisioning-worker",
            "test-mail",
            *(["test-sms"] if simulator else []),
        )
        lab.provision()
        lab.docker("up", "-d", "--wait", "--wait-timeout", "60", "peers")
        credentials = json.loads((root / "credentials.json").read_text())
        with closing(lab.login(credentials["admin"])) as admin:
            for site in lab.state["sites"].values():
                wait_for(
                    "gateway registration",
                    lambda: lab.call(
                        admin, "GET", "/provisioning/peers/gateway/" + site["gateway_id"]
                    ).json()["registered_at"],
                )
        print("Ready: " + lab.state["origin"] + "/app/")
        print("Local credentials: " + str(root / "credentials.json"))
        print("Test email inbox: " + lab.state["mail_origin"])
    elif args.action == "sms":
        if lab.activation_mode(args.site) != "sms":
            parser.error("This gateway uses Wi-Fi and has no SMS modem (use --site 1).")
        print("SMS delivered to modem: " + lab.peer("sms", args.site))
    elif args.action == "versions":
        result = json.loads(lab.peer("versions", args.site))
        save(root / "versions.json", result)
        print(json.dumps(result, indent=2))
    elif args.action == "verify":
        lab.verify()
        print("Report: " + str(root / "report.json"))
    elif args.action in ("client-start", "client-stop"):
        lab.peer(args.action, args.site)
        print("Linux OpenVPN client: " + args.action.removeprefix("client-"))
    elif args.action == "query":
        for protocol in ("tcp", "udp"):
            print(protocol.upper() + ": " + lab.peer("query", args.site, protocol))
    elif args.action in ("connect", "disconnect"):
        credentials = json.loads((root / "credentials.json").read_text())
        with closing(lab.login(credentials[f"tech{args.site}"])) as client:
            if args.action == "connect":
                identifier = lab.connect(client, args.site)
                print("ACTIVE: " + identifier + "; target replied " + lab.peer("query", args.site))
            else:
                lab.disconnect(client, args.site)
                print("Session closed and resources released.")
    elif args.action == "down":
        lab.docker("down", "--remove-orphans")
        print("Lab stopped; local state and its database volume are preserved.")
    else:
        print(lab.docker("ps").decode())
        print("Portal: " + lab.state["origin"] + "/app/")
        if simulator:
            print("Site 1 SMS: " + lab.peer("sms-status", 1))
            print("Site 2: Wi-Fi / MAC 02:00:00:00:00:02")


if __name__ == "__main__":
    main()
