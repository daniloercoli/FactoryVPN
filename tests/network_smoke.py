"""Gateway Only: traffico IPv4 della coppia, LAN negata e lease reali su Linux."""

import asyncio
import importlib.util
import os
from pathlib import Path
import select
import subprocess
import shlex
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import UUID

import httpx

sys.path.insert(0, str(Path(__file__).parents[1]))
from factoryvpn_common.network import SessionNetwork, run  # noqa: E402
from factoryvpn_common.protocol import peer_name  # noqa: E402
from ssh_helpers import SSHLab  # noqa: E402

WEB_PROBE_ERRORS = []


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


def extra_networks(gateway, host, number):
    # Una rete dietro un router sulla LAN e una dietro una seconda scheda.
    inside(host)(["ip", "addr", "add", "10.30.0.10/32", "dev", "lo"])
    inside(gateway)(["ip", "route", "add", "10.30.0.0/24", "via", "192.168.1.10"])
    router, remote = f"router{number}", f"remote{number}"
    for name in (router, remote):
        run(["ip", "netns", "add", name])
        inside(name)(["ip", "link", "set", "lo", "up"])
    inside(gateway)(
        ["ip", "link", "add", "eth1", "type", "veth", "peer", "name", "eth0", "netns", router]
    )
    inside(router)(
        ["ip", "link", "add", "eth1", "type", "veth", "peer", "name", "eth0", "netns", remote]
    )
    for name, interface, address in (
        (gateway, "eth1", "172.20.0.1/30"),
        (router, "eth0", "172.20.0.2/30"),
        (router, "eth1", "10.0.0.1/24"),
        (remote, "eth0", "10.0.0.5/24"),
    ):
        inside(name)(["ip", "addr", "add", address, "dev", interface])
        inside(name)(["ip", "link", "set", interface, "up"])
    inside(router)(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    inside(gateway)(["ip", "route", "add", "10.0.0.0/24", "via", "172.20.0.2"])
    # Destinazione pubblica simulata: verifica anche l'uscita tramite la default route.
    inside(remote)(["ip", "addr", "add", "203.0.113.10/32", "dev", "lo"])
    inside(router)(["ip", "route", "add", "203.0.113.10/32", "via", "10.0.0.5"])
    inside(gateway)(["ip", "route", "replace", "default", "via", "172.20.0.2"])
    # Le reti locali rispondono alla shell del gateway senza conoscere i pool VPN.
    inside(remote)(["ip", "route", "add", "172.20.0.0/30", "via", "10.0.0.1"])


def echo_server(namespace, label, address="0.0.0.0"):
    code = f"""import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(({address!r},9000))
while True:
 data,addr=s.recvfrom(1024);s.sendto({label!r}.encode(),addr)
"""
    return subprocess.Popen(["ip", "netns", "exec", namespace, sys.executable, "-c", code])


def raw_server(namespace):
    # Protocollo sperimentale: evita che una lista TCP/UDP/ICMP passi il collaudo.
    # Solo le richieste hanno risposta, quindi i due server non creano un loop.
    code = f"""import socket
s=socket.socket(socket.AF_INET,socket.SOCK_RAW,253)
while True:
 data,addr=s.recvfrom(65535)
 if data[(data[0]&15)*4:]==b'factoryvpn-probe': s.sendto(b'reply:'+{namespace!r}.encode(),(addr[0],0))
"""
    return subprocess.Popen(["ip", "netns", "exec", namespace, sys.executable, "-c", code])


def raw_query(namespace, address):
    code = f"""import socket
s=socket.socket(socket.AF_INET,socket.SOCK_RAW,253);s.settimeout(0.7)
try:
 s.sendto(b'factoryvpn-probe',({address!r},0))
 while True:
  data,source=s.recvfrom(65535); payload=data[(data[0]&15)*4:]
  if source[0]=={address!r} and payload.startswith(b'reply:'):
   print(payload[6:].decode()); break
except (TimeoutError,PermissionError): print('blocked')
"""
    return inside(namespace)([sys.executable, "-c", code]).stdout.strip()


def local_internet(technician, number):
    # La connessione abituale del tecnico è indipendente dalla VPN cliente.
    internet = f"internet{number}"
    run(["ip", "netns", "add", internet])
    inside(technician)(
        ["ip", "link", "add", "wan0", "type", "veth", "peer", "name", "eth0", "netns", internet]
    )
    for name, interface, address in (
        (technician, "wan0", f"198.18.{number}.2/24"),
        (internet, "eth0", f"198.18.{number}.1/24"),
    ):
        inside(name)(["ip", "addr", "add", address, "dev", interface])
        inside(name)(["ip", "link", "set", interface, "up"])
    inside(internet)(["ip", "addr", "add", "203.0.113.10/32", "dev", "lo"])
    inside(internet)(["ip", "link", "set", "lo", "up"])
    inside(technician)(["ip", "route", "replace", "default", "via", f"198.18.{number}.1"])
    inside(technician)(["ip", "route", "add", "192.168.1.0/24", "via", "10.100.0.1"])
    inside(technician)(["ip", "route", "add", "10.100.0.0/16", "via", "10.100.0.1"])


def query_code(address):
    return f"""import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(0.7)
s.bind(("0.0.0.0",39000))
try:
 s.sendto(b"hello",({address!r},9000)); print(s.recv(1024).decode())
except (TimeoutError,PermissionError): print("blocked")
"""


def query(technician, address="192.168.1.10"):
    return inside(technician)([sys.executable, "-c", query_code(address)]).stdout.strip()


def ping(namespace, address):
    # Lo stesso identificatore mantiene il flusso ICMP riconoscibile fra le sonde.
    return (
        inside(namespace)(
            ["ping", "-n", "-c", "1", "-W", "1", "-w", "1", "-e", "39001", address],
            check=False,
        ).returncode
        == 0
    )


def pair_traffic(technician, gateway, spec, *, allowed):
    # UDP riutilizza anche la stessa porta sorgente, non solo la destinazione.
    for source, address, label in (
        (technician, spec.gateway_ip, gateway),
        (gateway, spec.technician_ip, technician),
    ):
        expected = label if allowed else "blocked"
        assert web_query(source, address, 8080) == expected, (source, "TCP8080", expected)
        assert query(source, address) == expected, (source, "UDP9000", expected)
        assert ping(source, address) is allowed, (source, "ICMP", allowed)
        assert raw_query(source, address) == expected, (source, "IPv4 protocol253", expected)


def check_ssh_output_expiry(ssh, technician, renew, label):
    # I namespace condividono il filesystem del container: il file sblocca il
    # comando remoto senza inviare altri pacchetti SSH dopo la scadenza.
    release = ssh.root / f"release-output-{label}"
    written = ssh.root / f"output-written-{label}"
    remote = (
        f"echo ready; while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done; "
        f"echo after-expiry; touch {shlex.quote(str(written))}; sleep 10"
    )
    # Il client deve restare aperto: una sua disconnessione nasconderebbe il difetto.
    process = subprocess.Popen(
        ssh.command(technician, remote, keepalive_seconds=0),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    ssh.processes.append(process)
    assert select.select([process.stdout], [], [], 5)[0], "SSH output test did not start"
    assert process.stdout.readline().strip() == "ready"
    renew(1)
    time.sleep(1.2)
    release.touch()
    deadline = time.monotonic() + 5
    while not written.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert written.exists(), "Remote command did not produce output"
    assert process.poll() is None, "SSH client exited before the firewall check"
    assert not select.select([process.stdout], [], [], 1)[
        0
    ], f"SSH output passed an expired {label} lease"
    # Rinnovando solo il lato in prova, lo stesso TCP deve consegnare l'output sospeso:
    # proviamo così che il blocco dipendeva dal lease, non dal server o dal client.
    renew(300)
    assert select.select([process.stdout], [], [], 5)[0], "SSH output did not resume after renewal"
    assert process.stdout.readline().strip() == "after-expiry"


async def worker_api_outage(spec, firewall):
    # Il trasporto OpenVPN è simulato; namespace, firewall e SSH restano reali.
    technician = peer_name("technician", UUID(int=1))
    gateway = peer_name("gateway", UUID(int=2))
    item = {
        "session_id": spec.session_id,
        "network": spec.to_dict(),
        "status": "ACTIVE",
        "technician_cn": technician,
        "gateway_cn": gateway,
        "gateway_ready": True,
        "activation_confirmed": True,
        "lease_seconds": 30,
    }
    vpn = SimpleNamespace(
        clients=lambda: {technician: spec.technician_ip, gateway: spec.gateway_ip}
    )

    def handler(request):
        # Anche un elenco valido non rinnova il lease se la conferma fallisce.
        return httpx.Response(200, json=[item]) if request.method == "GET" else httpx.Response(503)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        worker = load("session-worker", "main").SessionWorker(client, firewall, vpn)
        await worker.reconcile()


async def gateway_api_outage(spec, network):
    async def stop():
        raise AssertionError("A temporary API failure stopped the VPN")

    vpn = SimpleNamespace(is_running=lambda: True, stop=stop)
    config = SimpleNamespace(ACTIVATION_MODE="wifi", VPN_INTERFACE="tun0")
    agent = load("gateway-agent", "main").GatewayAgent(None, vpn, network, None, config)
    agent.spec = spec
    await agent.handle_failure(httpx.ReadTimeout("Temporary API outage"))


def web_server(namespace, port, root):
    # I namespace non hanno DNS: il reverse lookup di HTTPServer.server_bind
    # ritarderebbe i listener e falserebbe le prove negative del firewall.
    code = """import http.server, socketserver, ssl, sys
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  data=sys.argv[1].encode(); self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
 def log_message(self,*args): pass
class Server(http.server.ThreadingHTTPServer):
 def server_bind(self):
  socketserver.TCPServer.server_bind(self)
  self.server_name='factoryvpn-test'; self.server_port=self.server_address[1]
server=Server(('0.0.0.0',int(sys.argv[2])),Handler)
if sys.argv[2]=='443':
 context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(sys.argv[3]+'/web.crt',sys.argv[3]+'/web.key'); server.socket=context.wrap_socket(server.socket,server_side=True)
server.serve_forever()
"""
    return subprocess.Popen(
        [
            "ip",
            "netns",
            "exec",
            namespace,
            sys.executable,
            "-c",
            code,
            namespace,
            str(port),
            str(root),
        ],
        stdout=subprocess.DEVNULL,
    )


def web_query(namespace, address, port=80):
    # Certificato HTTPS del solo laboratorio: il trasporto TLS viene eseguito davvero.
    code = f"""import urllib.request, ssl, sys
try:
 with urllib.request.urlopen('{"https" if port==443 else "http"}://{address}:{port}',timeout=0.8,context=ssl._create_unverified_context()) as r: print(r.read().decode())
except Exception as error: print(repr(error),file=sys.stderr); print('blocked')
"""
    result = inside(namespace)([sys.executable, "-c", code])
    if result.stderr:
        WEB_PROBE_ERRORS.append(f"{namespace} -> {address}:{port}: {result.stderr.strip()}")
    return result.stdout.strip()


def ready(probe, expected):
    # Le prove negative sono significative solo con i servizi già funzionanti.
    deadline = time.monotonic() + 5
    while probe() != expected:
        if time.monotonic() >= deadline:
            raise AssertionError(f"Local test service did not start: {expected}")
        time.sleep(0.05)


def main():
    if os.environ.get("FACTORYVPN_NETWORK_TEST") != "1":
        raise SystemExit("Run only in the disposable privileged container")
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
    internet_routes = {}
    temporary = tempfile.TemporaryDirectory(prefix="factoryvpn-ssh-", dir="/root")
    ssh = SSHLab(temporary.name)
    root = Path(temporary.name)
    run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=factoryvpn-test",
            "-days",
            "1",
            "-keyout",
            str(root / "web.key"),
            "-out",
            str(root / "web.crt"),
        ]
    )
    try:
        firewall.create_table()
        for number in (1, 2):
            spec = SessionNetwork(
                str(UUID(int=number)), f"10.100.128.{number+1}", f"10.100.0.{number+1}"
            )
            specs.append(spec)
            vpn_peer(f"gw{number}", spec.gateway_ip)
            vpn_peer(f"tech{number}", spec.technician_ip)
            lan(f"gw{number}", f"site{number}")
            extra_networks(f"gw{number}", f"site{number}", number)
            inside(f"gw{number}")(
                ["ip", "route", "add", spec.technician_ip + "/32", "via", "10.100.0.1"]
            )
            # Rotte aggiuntive ostili: il firewall deve negare gli altri peer
            # anche quando il sistema sa come raggiungerli.
            inside(f"gw{number}")(["ip", "route", "add", "10.100.0.0/16", "via", "10.100.0.1"])
            inside(f"gw{number}")(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            local_internet(f"tech{number}", number)
            internet_routes[number] = inside(f"tech{number}")(
                ["ip", "route", "get", "203.0.113.10"]
            ).stdout
            servers.append(echo_server(f"internet{number}", f"internet{number}", "203.0.113.10"))
            for name, address in (
                (f"site{number}", "192.168.1.10"),
                (f"site{number}", "10.30.0.10"),
                (f"remote{number}", "10.0.0.5"),
                (f"remote{number}", "203.0.113.10"),
            ):
                servers.append(echo_server(name, name, address))
            # Un servizio web LAN realmente raggiungibile dalla shell, ma non dal PC.
            servers.append(web_server(f"site{number}", 80, root))
            for port in (80, 443, 8080):
                servers.append(web_server(f"gw{number}", port, root))
            servers.append(web_server(f"tech{number}", 8080, root))
            for name in (f"gw{number}", f"tech{number}"):
                servers.append(echo_server(name, name))
                servers.append(raw_server(name))
            managers.append(gateway(runner=inside(f"gw{number}")))
            managers[-1].configure_session(spec, lease_seconds=300)
            ssh.start_server(f"gw{number}")
        firewall.apply(specs)
        # La LAN sarebbe realmente instradabile senza i filtri, in entrambe le
        # direzioni. La prova negativa non dipende da una semplice rotta mancante.
        run(["ip", "route", "add", "192.168.1.0/24", "via", specs[0].gateway_ip, "dev", "tun0"])
        for number in (1, 2):
            for namespace, ports in (
                (f"gw{number}", (80, 443, 8080)),
                (f"tech{number}", (8080,)),
                (f"site{number}", (80,)),
            ):
                for port in ports:
                    ready(lambda: web_query(namespace, "127.0.0.1", port), namespace)
            for namespace in (f"gw{number}", f"tech{number}"):
                ready(lambda: query(namespace, "127.0.0.1"), namespace)
                ready(lambda: raw_query(namespace, "127.0.0.1"), namespace)
        assert (
            web_query("tech1", specs[0].gateway_ip) == "blocked"
        ), "Preparation authorized traffic"
        pair_traffic("tech1", "gw1", specs[0], allowed=False)
        for spec in specs:
            firewall.ensure(spec, 300)
        for number, spec in enumerate(specs, 1):
            tech = f"tech{number}"
            assert web_query(tech, spec.gateway_ip) == f"gw{number}"
            assert web_query(tech, spec.gateway_ip, 443) == f"gw{number}"
            pair_traffic(tech, f"gw{number}", spec, allowed=True)
            assert web_query(tech, "192.168.1.10") == "blocked", "Direct LAN access is possible"
            assert web_query(tech, "192.168.1.1") == "blocked"
            assert (
                web_query(tech, specs[2 - number].gateway_ip) == "blocked"
            ), "Cross-gateway access"
            for source, address in (
                (tech, specs[2 - number].gateway_ip),
                (tech, specs[2 - number].technician_ip),
                (f"gw{number}", specs[2 - number].technician_ip),
                (f"gw{number}", specs[2 - number].gateway_ip),
            ):
                assert web_query(source, address, 8080) == "blocked", "Cross-pair TCP access"
                assert query(source, address) == "blocked", "Cross-pair UDP access"
                assert not ping(source, address), "Cross-pair ICMP access"
                assert raw_query(source, address) == "blocked", "Cross-pair raw IPv4 access"
            assert query(tech, "192.168.1.10") == "blocked"
            assert not ping(tech, "192.168.1.10")
            assert query(tech, "203.0.113.10") == f"internet{number}"
            for address, expected in [
                ("192.168.1.10", f"site{number}"),
                ("10.30.0.10", f"site{number}"),
                ("10.0.0.5", f"remote{number}"),
                ("203.0.113.10", f"remote{number}"),
            ]:
                command = shlex.join([sys.executable, "-c", query_code(address)])
                assert ssh.execute(tech, command).stdout.strip() == expected
            # Il cambio della LAN non modifica identità, IP VPN o configurazione VPN.
            inside(f"site{number}")(["ip", "addr", "add", "192.168.10.10/24", "dev", "eth0"])
            ssh.execute(tech, "ip address add 192.168.10.1/24 dev eth0")
            servers.append(echo_server(f"site{number}", f"site{number}", "192.168.10.10"))
            command = shlex.join([sys.executable, "-c", query_code("192.168.10.10")])
            assert ssh.execute(tech, command).stdout.strip() == f"site{number}"
        # Verifica indipendente del gateway: anche senza filtro centrale non
        # espone la LAN o il suo IP LAN ai pacchetti arrivati dalla VPN.
        run(["nft", "delete", "table", "inet", "factoryvpn_support"])
        for address in ("192.168.1.1", "192.168.1.10"):
            assert web_query("tech1", address) == "blocked", "Gateway allowed direct LAN HTTP"
            assert query("tech1", address) == "blocked", "Gateway allowed direct LAN UDP"
            assert not ping("tech1", address), "Gateway allowed direct LAN ICMP"
        firewall.create_table()
        firewall.apply(specs)
        for spec in specs:
            firewall.ensure(spec, 300)
        shells = [ssh.open_shell("tech1"), ssh.open_shell("tech2")]
        assert all(ssh.probe(shell) == "reply:check" for shell in shells)
        managers[0].disable()
        firewall.apply(specs[1:])
        assert ssh.probe(shells[0]) == "blocked"
        assert web_query("tech1", specs[0].gateway_ip) == "blocked"
        pair_traffic("tech1", "gw1", specs[0], allowed=False)
        pair_traffic("tech2", "gw2", specs[1], allowed=True)
        assert ssh.probe(shells[1]) == "reply:check"
        firewall.ensure(specs[1], 2)
        asyncio.run(worker_api_outage(specs[1], firewall))
        assert ssh.probe(shells[1]) == "reply:check"
        time.sleep(2.2)
        assert ssh.probe(shells[1]) == "blocked", "Expired server lease still carries SSH"
        assert web_query("tech2", specs[1].gateway_ip, 443) == "blocked"
        pair_traffic("tech2", "gw2", specs[1], allowed=False)
        firewall.ensure(specs[1], 300)
        pair_traffic("tech2", "gw2", specs[1], allowed=True)
        shell = ssh.open_shell("tech2")
        assert ssh.probe(shell) == "reply:check"
        managers[1].configure_session(specs[1], lease_seconds=2)
        asyncio.run(gateway_api_outage(specs[1], managers[1]))
        assert ssh.probe(shell) == "reply:check"
        time.sleep(2.2)
        assert ssh.probe(shell) == "blocked", "Expired gateway lease still carries SSH"
        assert web_query("tech2", specs[1].gateway_ip) == "blocked"
        pair_traffic("tech2", "gw2", specs[1], allowed=False)
        managers[1].configure_session(specs[1], lease_seconds=300)
        pair_traffic("tech2", "gw2", specs[1], allowed=True)
        check_ssh_output_expiry(ssh, "tech2", lambda ttl: firewall.ensure(specs[1], ttl), "server")
        check_ssh_output_expiry(
            ssh,
            "tech2",
            lambda ttl: managers[1].configure_session(specs[1], lease_seconds=ttl),
            "gateway",
        )
        # Un riavvio del worker non deve adottare i vecchi lease.
        firewall.create_table()
        firewall.apply(specs[1:])
        assert web_query("tech2", specs[1].gateway_ip) == "blocked"
        pair_traffic("tech2", "gw2", specs[1], allowed=False)
        firewall.ensure(specs[1], 300)
        assert web_query("tech2", specs[1].gateway_ip) == "gw2"
        pair_traffic("tech2", "gw2", specs[1], allowed=True)
        for number in (1, 2):
            assert (
                inside(f"tech{number}")(["ip", "route", "get", "203.0.113.10"]).stdout
                == internet_routes[number]
            )
        print(
            "PASS: Gateway Only SSH/HTTP/HTTPS/TCP8080/UDP9000/ICMP/raw253, gateway-initiated callbacks, denied LAN/other peers, LAN change via SSH, independent sessions, teardown, API outages, bidirectional server/gateway lease expiry, established SSH output and restart"
        )
    except BaseException as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr)
        print("\n".join(WEB_PROBE_ERRORS))
        for number in (1, 2):
            listeners = inside(f"gw{number}")(["ss", "-lntup"], check=False)
            print(f"gw{number} listeners:\n" + listeners.stdout + listeners.stderr)
        print("Server exits:", [(server.pid, server.poll()) for server in servers])
        print(run(["nft", "list", "ruleset"]).stdout)
        raise
    finally:
        ssh.close()
        for server in servers:
            server.terminate()
            server.wait(timeout=3)
        temporary.cleanup()


if __name__ == "__main__":
    main()
