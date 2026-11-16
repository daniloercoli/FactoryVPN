"""Le sonde del lab distinguono l'IP del gateway dalla LAN non autorizzata."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import local_lab_peers as peers


def test_lab_listener_starts_without_reverse_dns(monkeypatch):
    def no_dns(*_):
        raise AssertionError("The isolated lab cannot depend on reverse DNS")

    monkeypatch.setattr(peers.socket, "getfqdn", no_dns)
    with peers.LabHTTPServer(("127.0.0.1", 0), peers.BaseHTTPRequestHandler) as server:
        assert server.server_name == "factoryvpn-lab"
        assert server.server_port == server.server_address[1] > 0


@pytest.mark.parametrize(
    ("protocol", "destination"),
    [("gateway-udp", "10.100.0.2"), ("lan-udp", "192.168.1.10"), ("udp", "192.168.1.10")],
)
def test_udp_probes_use_distinct_targets_and_reuse_the_flow(monkeypatch, protocol, destination):
    sock = MagicMock()
    sock.__enter__.return_value = sock
    sock.recv.return_value = b"site1"
    monkeypatch.setattr(peers.socket, "socket", lambda *_: sock)

    assert peers.query(protocol, "10.100.0.2") == "site1"
    sock.bind.assert_called_once_with(("0.0.0.0", 39000))
    sock.sendto.assert_called_once_with(b"factoryvpn-lab", (destination, 9000))


def test_nonstandard_tcp_probe_uses_gateway_port_8080(monkeypatch):
    import urllib.request

    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b"site1"
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert peers.query("tcp-alt", "10.100.0.2") == "site1"
    assert urlopen.call_args.args[0] == "http://10.100.0.2:8080/"


@pytest.mark.parametrize(("returncode", "expected"), [(0, "reachable"), (1, "blocked")])
def test_icmp_probe_reports_reachability(monkeypatch, returncode, expected):
    execute = MagicMock(return_value=SimpleNamespace(returncode=returncode))
    monkeypatch.setattr(peers, "run", execute)

    assert peers.query("icmp", "10.100.0.2") == expected
    assert execute.call_args.args[0][-1] == "10.100.0.2"
    assert execute.call_args.kwargs["check"] is False
