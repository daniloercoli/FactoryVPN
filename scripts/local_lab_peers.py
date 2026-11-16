"""Linux-only peers for the disposable FactoryVPN local lab.

Runs the real gateway agent and OpenVPN clients in separate network namespaces.
LAN endpoints and optional SMS transport are simulated. Never run on a Linux host.
"""

import argparse
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import select
import shlex
import signal
import socket
from socketserver import TCPServer
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values
from scripts.simulator_sms import Inbox, Modem

ROOT = Path("/lab")


class LabHTTPServer(HTTPServer):
    def server_bind(self):
        # I namespace del lab non richiedono DNS per servire i bersagli locali.
        TCPServer.server_bind(self)
        self.server_name = "factoryvpn-lab"
        self.server_port = self.server_address[1]


def run(command, **kwargs):
    return subprocess.run(command, check=kwargs.pop("check", True), text=True, timeout=15, **kwargs)


def inside(name, command, **kwargs):
    return run(["ip", "netns", "exec", name, *command], **kwargs)


def external_hostname(value):
    # Accettiamo solo nomi/IPv4: il laboratorio e la VPN sono IPv4.
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        labels = value.rstrip(".").split(".")
        if len(value) > 253 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("Invalid external endpoint hostname") from None
    else:
        if address.version != 4:
            raise ValueError("External simulator endpoints must use IPv4")
    return value.lower()


def external_origin(value):
    try:
        origin = urlsplit(value)
        port = origin.port if origin.port is not None else 443
        valid = (
            origin.scheme == "https"
            and origin.hostname
            and origin.username is None
            and origin.password is None
            and origin.path in ("", "/")
            and not origin.query
            and not origin.fragment
            and "?" not in value
            and "#" not in value
            and not any(character.isspace() for character in value)
            and 1 <= port <= 65535
        )
        if valid:
            return external_hostname(origin.hostname)
    except (TypeError, ValueError):
        pass
    # Non includere l'URL ricevuto: una configurazione errata può contenere segreti.
    raise ValueError("gateway.env requires an HTTPS origin without credentials or a path")


def profile_endpoints(path):
    endpoints = set()
    inline = False
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("<"):
                inline = not line.startswith("</")
                continue
            if inline or not line or line.startswith(("#", ";")):
                continue
            fields = shlex.split(line, comments=True)
            if (
                fields
                and fields[0] == "proto"
                and fields != ["proto", "udp"]
                and fields != ["proto", "udp4"]
            ):
                raise ValueError("Invalid transport")
            if not fields or fields[0] != "remote":
                continue
            if (
                len(fields) not in (3, 4)
                or not 1 <= int(fields[2]) <= 65535
                or (len(fields) == 4 and fields[3] not in ("udp", "udp4"))
            ):
                raise ValueError("Invalid remote")
            endpoints.add(external_hostname(fields[1]))
    except (OSError, ValueError, UnicodeError):
        raise ValueError(
            f"Invalid or unreadable endpoint in {path.parent.name}/{path.name}"
        ) from None
    if not endpoints:
        raise ValueError(f"Missing remote endpoint in {path.parent.name}/{path.name}")
    return endpoints


def external_configuration():
    configurations, endpoints = {}, set()
    for number in (1, 2):
        folder = ROOT / f"site{number}"
        config = dotenv_values(folder / "gateway.env")
        endpoints.add(external_origin(config.get("SERVER_URL")))
        configurations[number] = config
        for profile in ("gateway.ovpn", "technician.ovpn"):
            endpoints.update(profile_endpoints(folder / profile))
    hosts = ["127.0.0.1 localhost", "::1 localhost ip6-localhost"]
    reserved = tuple(
        ipaddress.ip_network(network)
        for network in ("192.168.1.0/24", "198.18.0.0/24", "10.100.0.0/16")
    )
    for hostname in sorted(endpoints):
        try:
            addresses = {
                result[4][0]
                for result in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
            }
        except OSError:
            raise ValueError("Cannot resolve an external API/VPN endpoint to IPv4") from None
        if not addresses:
            raise ValueError("Cannot resolve an external API/VPN endpoint to IPv4")
        for value in sorted(addresses):
            address = ipaddress.IPv4Address(value)
            if (
                address.is_loopback
                or address.is_unspecified
                or address.is_multicast
                or address.is_link_local
            ):
                raise ValueError(
                    "External API/VPN endpoint must be reachable outside the container"
                )
            if any(address in network for network in reserved):
                raise ValueError(
                    "External API/VPN endpoint overlaps simulator LAN/WAN or VPN networks"
                )
            hosts.append(f"{address} {hostname}")
    ca = ROOT / "https-ca.pem"
    if ca.exists():
        try:
            ssl.create_default_context(cafile=str(ca))
        except (OSError, ssl.SSLError):
            raise ValueError("Invalid or unreadable /lab/https-ca.pem") from None
    return configurations, "\n".join(hosts) + "\n"


def gateway_environment(number, config, *, external=False):
    env = {
        **os.environ,
        **config,
        "OPENVPN_CONFIG": str(ROOT / f"site{number}" / "gateway.ovpn"),
        "GATEWAY_WIFI_CHECK_INTERVAL": "5",
    }
    if external:
        # La fiducia remota proviene solo dal bundle esplicito o dalle CA di sistema.
        # Evitiamo di ereditare il certificato del vecchio lab o variabili dell'host.
        env.pop("SSL_CERT_FILE", None)
        env.pop("SSL_CERT_DIR", None)
        if (ROOT / "https-ca.pem").is_file():
            env["SSL_CERT_FILE"] = str(ROOT / "https-ca.pem")
    else:
        env.update(SERVER_URL="https://nginx", SSL_CERT_FILE="/lab/ca.crt")
    return env


def topology(hosts=None):
    run(
        [
            "sysctl",
            "-qw",
            "net.ipv4.ip_forward=1",
            "net.ipv4.conf.all.rp_filter=0",
            "net.ipv4.conf.default.rp_filter=0",
        ]
    )
    run(["ip", "link", "add", "labwan0", "type", "bridge"])
    run(["ip", "addr", "add", "198.18.0.1/24", "dev", "labwan0"])
    run(["ip", "link", "set", "labwan0", "up"])
    if hosts is None:
        hosts = (
            Path("/etc/hosts").read_text()
            + "\n"
            + "\n".join(f"{socket.gethostbyname(host)} {host}" for host in ("nginx", "openvpn"))
            + "\n"
        )
    for number in (1, 2):
        for role, address in (("gw", 10 + number), ("tech", 20 + number)):
            name = f"{role}{number}"
            run(["ip", "netns", "add", name])
            folder = Path("/etc/netns") / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "hosts").write_text(hosts)
            run(
                [
                    "ip",
                    "link",
                    "add",
                    "p" + name,
                    "type",
                    "veth",
                    "peer",
                    "name",
                    "wan0",
                    "netns",
                    name,
                ]
            )
            run(["ip", "link", "set", "p" + name, "master", "labwan0"])
            run(["ip", "link", "set", "p" + name, "up"])
            inside(name, ["ip", "addr", "add", f"198.18.0.{address}/24", "dev", "wan0"])
            inside(name, ["ip", "link", "set", "wan0", "up"])
            inside(name, ["ip", "link", "set", "lo", "up"])
            inside(name, ["ip", "route", "add", "default", "via", "198.18.0.1"])
        site, gateway = f"site{number}", f"gw{number}"
        run(["ip", "netns", "add", site])
        inside(
            gateway,
            ["ip", "link", "add", "eth0", "type", "veth", "peer", "name", "eth0", "netns", site],
        )
        for name, address in ((gateway, "192.168.1.1/24"), (site, "192.168.1.10/24")):
            inside(name, ["ip", "addr", "add", address, "dev", "eth0"])
            inside(name, ["ip", "link", "set", "eth0", "up"])
            inside(name, ["ip", "link", "set", "lo", "up"])
        inside(site, ["ip", "route", "add", "default", "via", "192.168.1.1"])
    run(
        ["nft", "-f", "-"],
        input="""table ip factoryvpn_lab {
 chain postrouting { type nat hook postrouting priority 100;
  ip saddr 198.18.0.0/24 oifname "eth0" masquerade
 }
}
""",
    )


def serve(*, external=False):
    (ROOT / "ready").unlink(missing_ok=True)
    # Validare prima di creare namespace o avviare processi del laboratorio.
    configurations, hosts = external_configuration() if external else ({}, None)
    topology(hosts)
    children, logs, modems = [], [], []
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        for number in (1, 2):
            folder = ROOT / f"site{number}"
            config = configurations[number] if external else dotenv_values(folder / "gateway.env")
            env = gateway_environment(number, config, external=external)
            if env.get("GATEWAY_ACTIVATION_MODE") == "sms":
                modem = Modem(Inbox(), env["GATEWAY_PHONE_NUMBER"])
                modems.append(modem)
                env["MODEM_PATH"] = modem.path
            for name, command in (
                (
                    f"site{number}",
                    [sys.executable, "-m", "scripts.local_lab_peers", "target", str(number)],
                ),
                (
                    f"gw{number}",
                    [sys.executable, "-m", "scripts.local_lab_peers", "web", str(number)],
                ),
                (f"gw{number}", [sys.executable, "-m", "gateway-agent.main"]),
            ):
                log = (folder / f"{name}.log").open("a")
                logs.append(log)
                children.append(
                    subprocess.Popen(
                        ["ip", "netns", "exec", name, *command], env=env, stdout=log, stderr=log
                    )
                )
        (ROOT / "ready").touch()
        print("FactoryVPN lab: two Gateway Only peers and local LANs ready.", flush=True)
        while not stop.wait(1):
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A lab peer exited; inspect the per-peer logs.")
            if any(not modem.thread.is_alive() for modem in modems):
                raise RuntimeError("A simulated modem stopped unexpectedly.")
    finally:
        (ROOT / "ready").unlink(missing_ok=True)
        for number in (1, 2):
            client_stop(number)
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()
        for modem in modems:
            modem.close()


def target(number):
    label = f"site{number}".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(label)))
            self.end_headers()
            self.wfile.write(label)

        def log_message(self, *_):
            pass

    server = LabHTTPServer(("0.0.0.0", 8080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", 9000))
        while True:
            _, address = sock.recvfrom(1024)
            sock.sendto(label, address)


def gateway_web(number):
    # Servizi fittizi del gateway: il firmware reale deve predisporre i propri.
    folder = ROOT / f"site{number}"
    cert, key = folder / "web.crt", folder / "web.key"
    if not cert.exists():
        run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=factoryvpn-lab",
                "-days",
                "2",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            capture_output=True,
        )
    label = f"site{number}".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(label)))
            self.end_headers()
            self.wfile.write(label)

        def log_message(self, *_):
            pass

    http = LabHTTPServer(("0.0.0.0", 80), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    # Lo stesso bersaglio del sito fornisce sul gateway HTTP 8080 e UDP 9000:
    # la rete deve limitarne l'identità, non le porte o il protocollo.
    threading.Thread(target=target, args=(number,), daemon=True).start()
    https = LabHTTPServer(("0.0.0.0", 443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    https.socket = context.wrap_socket(https.socket, server_side=True)
    https.serve_forever()


@contextmanager
def client_control(number):
    # Separate CLI invocations share the same PID file. Serialize replacement
    # so that a second start cannot race with teardown of the previous tunnel.
    with (ROOT / f"site{number}" / "technician.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def owns_client(pid, number):
    proc = Path(f"/proc/{pid}")
    args = (proc / "cmdline").read_bytes().split(b"\0")
    config = str(ROOT / f"site{number}" / "technician.ovpn").encode()
    return (
        any(args[index : index + 2] == [b"--config", config] for index in range(len(args)))
        and (proc / "exe").resolve(strict=True).name == "openvpn"
        and os.path.samestat((proc / "ns/net").stat(), Path(f"/run/netns/tech{number}").stat())
    )


def stop_client_locked(number):
    pidfile = ROOT / f"site{number}" / "technician.pid"
    try:
        pid = int(pidfile.read_text())
        if pid <= 1:
            raise ValueError("Invalid client PID")
    except (FileNotFoundError, ValueError):
        pidfile.unlink(missing_ok=True)
        return
    descriptor = None
    try:
        # Pin the process before checking its identity: PID reuse must never
        # redirect a signal to another gateway or technician.
        descriptor = os.pidfd_open(pid)
        if owns_client(pid, number):
            for sig, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
                signal.pidfd_send_signal(descriptor, sig)
                if select.select([descriptor], [], [], timeout)[0]:
                    break
            else:
                raise TimeoutError("Technician OpenVPN process did not stop")
    except (ProcessLookupError, FileNotFoundError):
        pass  # The old process has already exited.
    finally:
        if descriptor is not None:
            os.close(descriptor)
    pidfile.unlink(missing_ok=True)


def client_stop(number):
    with client_control(number):
        stop_client_locked(number)


def client_start(number):
    with client_control(number):
        stop_client_locked(number)
        folder = ROOT / f"site{number}"
        try:
            inside(
                f"tech{number}",
                [
                    "openvpn",
                    "--config",
                    str(folder / "technician.ovpn"),
                    "--daemon",
                    "--writepid",
                    str(folder / "technician.pid"),
                    "--log",
                    str(folder / "technician.log"),
                ],
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    pid = int((folder / "technician.pid").read_text())
                    if owns_client(pid, number):
                        return  # Process exists; the portal confirms VPN readiness separately.
                except (FileNotFoundError, ProcessLookupError, ValueError):
                    pass
                time.sleep(0.05)
            raise RuntimeError("Technician OpenVPN process did not publish a valid PID")
        except BaseException:
            stop_client_locked(number)
            raise


def query(protocol, address):
    try:
        if protocol in ("tcp", "tcp-alt", "https"):
            import urllib.request

            scheme = "https" if protocol == "https" else "http"
            # Certificato autofirmato limitato al laboratorio.
            context = ssl._create_unverified_context() if protocol == "https" else None
            port = ":8080" if protocol == "tcp-alt" else ""
            with urllib.request.urlopen(
                f"{scheme}://{address}{port}/", timeout=2, context=context
            ) as response:
                return response.read().decode()
        if protocol == "icmp":
            result = run(
                ["ping", "-n", "-c", "1", "-W", "1", "-w", "2", address],
                check=False,
                capture_output=True,
            )
            return "reachable" if result.returncode == 0 else "blocked"
        if protocol not in ("gateway-udp", "lan-udp", "udp"):
            raise ValueError("Unknown lab protocol")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            # Reuse the five-tuple so teardown/lease checks also exercise an
            # already tracked UDP flow, not only new connections.
            sock.bind(("0.0.0.0", 39000))
            # udp resta l'alias storico della sonda LAN; i nuovi caller sono espliciti.
            destination = address if protocol == "gateway-udp" else "192.168.1.10"
            sock.sendto(b"factoryvpn-lab", (destination, 9000))
            return sock.recv(1024).decode()
    except OSError:
        return "blocked"


def main():
    if os.getenv("FACTORYVPN_LOCAL_LAB") != "1" or not Path("/.dockerenv").exists():
        raise SystemExit("Run only inside the FactoryVPN local lab peers container.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "serve",
            "target",
            "web",
            "client-start",
            "client-stop",
            "query",
            "query-inner",
            "sms",
            "sms-status",
            "versions",
        ],
    )
    parser.add_argument("site", nargs="?", type=int, choices=[1, 2], default=1)
    parser.add_argument(
        "--external",
        action="store_true",
        help="Serve only peers using the downloaded server endpoints",
    )
    parser.add_argument(
        "--protocol",
        choices=["tcp", "tcp-alt", "https", "gateway-udp", "icmp", "lan-udp", "udp"],
        default="tcp",
    )
    parser.add_argument("--address", default="10.100.0.2")
    args = parser.parse_args()
    if args.external and args.action != "serve":
        parser.error("--external is only valid with serve")
    if args.action == "serve":
        try:
            serve(external=args.external)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    elif args.action == "web":
        gateway_web(args.site)
    elif args.action == "target":
        target(args.site)
    elif args.action == "client-start":
        client_start(args.site)
    elif args.action == "client-stop":
        client_stop(args.site)
    elif args.action == "query-inner":
        print(query(args.protocol, args.address))
    elif args.action in ("sms", "sms-status"):
        config = dotenv_values(ROOT / f"site{args.site}" / "gateway.env")
        if config.get("GATEWAY_ACTIVATION_MODE") != "sms":
            raise SystemExit("This gateway has no SMS modem.")
        inbox = Inbox()
        phone = config["GATEWAY_PHONE_NUMBER"]
        print(
            json.dumps(inbox.counts(phone)) if args.action == "sms-status" else inbox.deliver(phone)
        )
    elif args.action == "versions":
        print(
            json.dumps(
                {
                    "os": platform.freedesktop_os_release().get("PRETTY_NAME"),
                    "python": platform.python_version(),
                    "openssl": ssl.OPENSSL_VERSION,
                    "openvpn": run(
                        ["openvpn", "--version"], capture_output=True
                    ).stdout.splitlines()[0],
                }
            )
        )
    else:
        result = inside(
            f"gw{args.site}",
            ["ip", "-j", "-4", "addr", "show", "dev", "tun0"],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            print("blocked")
            return
        addresses = [a["local"] for link in json.loads(result.stdout) for a in link["addr_info"]]
        if not addresses:
            print("blocked")
            return
        inside(
            f"tech{args.site}",
            [
                sys.executable,
                "-m",
                "scripts.local_lab_peers",
                "query-inner",
                "--protocol",
                args.protocol,
                "--address",
                addresses[0],
            ],
        )


if __name__ == "__main__":
    main()
