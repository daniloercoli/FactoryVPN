"""Linux-only peers for the disposable FactoryVPN local lab.

Runs the real gateway agent and OpenVPN clients in separate network namespaces.
LAN endpoints and optional SMS transport are simulated. Never run on a Linux host.
"""

import argparse
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import platform
import select
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

from dotenv import dotenv_values
from scripts.simulator_sms import Inbox, Modem

ROOT = Path("/lab")


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, timeout=15, **kwargs)


def inside(name, command, **kwargs):
    return run(["ip", "netns", "exec", name, *command], **kwargs)


def topology():
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


def serve():
    (ROOT / "ready").unlink(missing_ok=True)
    topology()
    children, logs, modems = [], [], []
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        for number in (1, 2):
            folder = ROOT / f"site{number}"
            env = {
                **os.environ,
                **dotenv_values(folder / "gateway.env"),
                "SERVER_URL": "https://nginx",
                "SSL_CERT_FILE": "/lab/ca.crt",
                "OPENVPN_CONFIG": str(folder / "gateway.ovpn"),
                "GATEWAY_WIFI_CHECK_INTERVAL": "5",
            }
            if env.get("GATEWAY_ACTIVATION_MODE") == "sms":
                modem = Modem(Inbox(), env["GATEWAY_PHONE_NUMBER"])
                modems.append(modem)
                env["MODEM_PATH"] = modem.path
            for name, command in (
                (
                    f"site{number}",
                    [sys.executable, "-m", "scripts.local_lab_peers", "target", str(number)],
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
        print("FactoryVPN lab: two gateways, technicians and overlapping LANs ready.", flush=True)
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

    server = HTTPServer(("0.0.0.0", 8080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", 9000))
        while True:
            _, address = sock.recvfrom(1024)
            sock.sendto(label, address)


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


def query(protocol):
    try:
        if protocol == "tcp":
            import urllib.request

            with urllib.request.urlopen("http://192.168.1.10:8080/", timeout=2) as response:
                return response.read().decode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(2)
            # Reuse the five-tuple so teardown/lease checks also exercise an
            # already tracked UDP flow, not only new connections.
            sock.bind(("0.0.0.0", 39000))
            sock.sendto(b"factoryvpn-lab", ("192.168.1.10", 9000))
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
    parser.add_argument("--protocol", choices=["tcp", "udp"], default="tcp")
    args = parser.parse_args()
    if args.action == "serve":
        serve()
    elif args.action == "target":
        target(args.site)
    elif args.action == "client-start":
        client_start(args.site)
    elif args.action == "client-stop":
        client_stop(args.site)
    elif args.action == "query-inner":
        print(query(args.protocol))
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
        inside(
            f"tech{args.site}",
            [
                sys.executable,
                "-m",
                "scripts.local_lab_peers",
                "query-inner",
                "--protocol",
                args.protocol,
            ],
        )


if __name__ == "__main__":
    main()
