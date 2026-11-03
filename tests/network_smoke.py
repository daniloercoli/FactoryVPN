"""Real Linux namespace/NAT regression; run only in the disposable test container."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parents[1]))
from factoryvpn_common.network import SessionNetwork, run  # noqa: E402


def load(folder, name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[1] / folder / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inside(namespace):
    return lambda args, **kwargs: run(["ip", "netns", "exec", namespace, *args], **kwargs)


def vpn_peer(name, address):
    run(["ip", "netns", "add", name])
    run(["ip", "link", "add", "p" + name, "type", "veth", "peer", "name", "tun0", "netns", name])
    run(["ip", "link", "set", "p" + name, "master", "tun0"])
    run(["ip", "link", "set", "p" + name, "up"])
    peer = inside(name)
    peer(["ip", "addr", "add", address + "/32", "dev", "tun0"])
    peer(["ip", "link", "set", "tun0", "up"])
    peer(["ip", "link", "set", "lo", "up"])
    peer(["ip", "route", "add", "10.100.0.1/32", "dev", "tun0"])
    peer(["ip", "route", "add", "default", "via", "10.100.0.1"])


def lan(gateway, host):
    run(["ip", "netns", "add", host])
    inside(gateway)(
        ["ip", "link", "add", "eth0", "type", "veth", "peer", "name", "eth0", "netns", host]
    )
    for namespace, address in ((gateway, "192.168.1.1/24"), (host, "192.168.1.10/24")):
        execute = inside(namespace)
        execute(["ip", "addr", "add", address, "dev", "eth0"])
        execute(["ip", "link", "set", "eth0", "up"])
        execute(["ip", "link", "set", "lo", "up"])
    inside(host)(["ip", "route", "add", "default", "via", "192.168.1.1"])


def query(technician):
    code = """import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(0.7)
s.bind(("0.0.0.0",39000))
s.sendto(b"hello",("192.168.1.10",9000))
try: print(s.recv(1024).decode())
except TimeoutError: print("blocked")
"""
    return inside(technician)([sys.executable, "-c", code]).stdout.strip()


def main():
    if os.environ.get("FACTORYVPN_NETWORK_TEST") != "1":
        raise SystemExit("This test must run in a disposable privileged container")
    workers = load("session-worker", "namespace_manager")
    firewall = load("session-worker", "firewall_manager").FirewallManager()
    gateway = load("gateway-agent", "network").NetworkManager
    run(
        [
            "sysctl",
            "-w",
            "net.ipv4.ip_forward=1",
            "net.ipv4.conf.all.rp_filter=0",
            "net.ipv4.conf.default.rp_filter=0",
        ]
    )
    run(["ip", "link", "add", "tun0", "type", "bridge"])
    run(["ip", "addr", "add", "10.100.0.1/16", "dev", "tun0"])
    run(["ip", "link", "set", "tun0", "up"])
    specs, managers, servers = [], [], []
    ns = workers.NamespaceManager()
    try:
        for number in (1, 2):
            vpn_peer(f"gw{number}", f"10.100.0.{number+1}")
            vpn_peer(f"tech{number}", f"10.100.128.{number+1}")
            lan(f"gw{number}", f"site{number}")
            server = (
                'import socket\ns=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(("0.0.0.0",9000))\nwhile True:\n data,addr=s.recvfrom(1024);s.sendto(b"site'
                + str(number)
                + '",addr)'
            )
            servers.append(
                subprocess.Popen(
                    ["ip", "netns", "exec", f"site{number}", sys.executable, "-c", server]
                )
            )
            spec = SessionNetwork(
                str(UUID(int=number)),
                number,
                f"10.100.128.{number+1}",
                f"10.100.0.{number+1}",
                "192.168.1.0/24",
            )
            specs.append(spec)
            ns.ensure(spec)
            manager = gateway(runner=inside(f"gw{number}"))
            managers.append(manager)
            manager.configure_session(spec)
        firewall.apply(specs)
        assert query("tech1") == "site1", "First overlapping LAN is not reachable"
        assert query("tech2") == "site2", "Second overlapping LAN is not reachable"
        # Reconciliation must be idempotent.
        ns.ensure(specs[1])
        managers[1].configure_session(specs[1])
        firewall.apply(specs)
        assert query("tech2") == "site2"
        managers[0].disable_nat(spec=specs[0])
        ns.delete(specs[0])
        firewall.apply(specs[1:])
        assert query("tech1") == "blocked", "Closed session still forwards traffic"
        assert query("tech2") == "site2", "Closing one session broke the other"
        ns.ensure(specs[1], lease_seconds=1)
        managers[1].configure_session(specs[1], lease_seconds=1)
        time.sleep(1.2)
        assert query("tech2") == "blocked", "Expired kernel lease still forwards traffic"
        print("PASS: overlapping LANs, idempotency, isolated teardown and lease expiry")
    except BaseException:
        print(run(["ip", "rule", "show"]).stdout)
        print(run(["nft", "list", "ruleset"]).stdout)
        for spec in specs:
            print(inside(spec.namespace)(["ip", "route"], check=False).stdout)
        raise
    finally:
        for server in servers:
            server.terminate()
            server.wait(timeout=3)


if __name__ == "__main__":
    main()
