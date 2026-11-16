"""I peer remoti mantengono identità, fiducia TLS e isolamento del laboratorio."""

import socket
from unittest.mock import Mock

import pytest

from scripts import local_lab_peers as peers
from scripts.bootstrap import initialize_https


@pytest.fixture
def remote_files(tmp_path, monkeypatch):
    monkeypatch.setattr(peers, "ROOT", tmp_path)
    for number in (1, 2):
        folder = tmp_path / f"site{number}"
        folder.mkdir()
        (folder / "gateway.env").write_text(
            f"SERVER_URL=https://api{number}.example.test:8443\n"
            "GATEWAY_API_KEY=private-key-must-not-appear-in-errors\n"
            f"GATEWAY_ACTIVATION_MODE={'sms' if number == 1 else 'wifi'}\n"
            "GATEWAY_PHONE_NUMBER=+15550100001\n"
        )
        for kind in ("gateway", "technician"):
            (folder / f"{kind}.ovpn").write_text(
                "client\nproto udp\n"
                f"remote vpn{number}.example.test 1194\n"
                f"remote fallback{number}.example.test 21194 udp4\n"
                "<key>\nremote NOT-AN-ENDPOINT 999999\n</key>\n"
            )
    return tmp_path


def resolver(hostname, port, family, type):
    assert port is None
    assert family == socket.AF_INET
    assert type == socket.SOCK_STREAM
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.20", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.21", 0)),
    ]


def test_external_uses_every_profile_endpoint_without_rewriting_profiles(remote_files, monkeypatch):
    before = {path: path.read_bytes() for path in remote_files.rglob("*") if path.is_file()}
    resolve = Mock(side_effect=resolver)
    monkeypatch.setattr(peers.socket, "getaddrinfo", resolve)
    configs, hosts = peers.external_configuration()
    assert configs[1]["SERVER_URL"] == "https://api1.example.test:8443"
    assert configs[1]["GATEWAY_ACTIVATION_MODE"] == "sms"
    assert configs[2]["GATEWAY_ACTIVATION_MODE"] == "wifi"
    expected = {
        f"{prefix}{number}.example.test"
        for number in (1, 2)
        for prefix in ("api", "vpn", "fallback")
    }
    assert {call.args[0] for call in resolve.call_args_list} == expected
    for hostname in expected:
        assert f"203.0.113.20 {hostname}\n" in hosts
        assert f"203.0.113.21 {hostname}\n" in hosts
    assert "nginx" not in hosts and "openvpn" not in hosts
    assert "private-key" not in hosts and "NOT-AN-ENDPOINT" not in hosts
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "",
        "http://api.example",
        "https://user:secret@api.example",
        "https://api.example/path",
        "https://api.example?token=secret",
        "https://api.example#secret",
        "https://api.example?",
        "https://api.example:0",
        "https://api.example:65536",
        "https://api.example:bad",
        "https://[::1]",
        "https://api.example\n",
        "https://api.example\\secret",
    ],
)
def test_external_rejects_invalid_origin_without_disclosing_value(origin):
    with pytest.raises(ValueError) as error:
        peers.external_origin(origin)
    assert str(error.value) == "gateway.env requires an HTTPS origin without credentials or a path"


@pytest.mark.parametrize("origin", ["https://192.168.50.10", "https://api.example:8443/"])
def test_external_accepts_ip_and_https_port(origin):
    assert peers.external_origin(origin) in ("192.168.50.10", "api.example")


@pytest.mark.parametrize("address", ["192.168.1.10", "198.18.0.7", "10.100.0.1"])
def test_external_rejects_routes_shadowed_by_simulator_or_tunnel(
    remote_files, monkeypatch, address
):
    monkeypatch.setattr(
        peers.socket,
        "getaddrinfo",
        lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))],
    )
    with pytest.raises(ValueError, match="overlaps simulator LAN/WAN or VPN"):
        peers.external_configuration()


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "169.254.5.1", "224.0.0.1"])
def test_external_rejects_nonreachable_addresses(remote_files, monkeypatch, address):
    monkeypatch.setattr(
        peers.socket,
        "getaddrinfo",
        lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))],
    )
    with pytest.raises(ValueError, match="reachable outside the container"):
        peers.external_configuration()


def test_external_dns_failure_is_sanitized_before_topology(remote_files, monkeypatch):
    monkeypatch.setattr(
        peers.socket,
        "getaddrinfo",
        Mock(side_effect=socket.gaierror("unexpected secret in resolver")),
    )
    topology = Mock()
    monkeypatch.setattr(peers, "topology", topology)
    with pytest.raises(
        ValueError, match="Cannot resolve an external API/VPN endpoint to IPv4"
    ) as error:
        peers.serve(external=True)
    assert "secret" not in str(error.value)
    topology.assert_not_called()


@pytest.mark.parametrize(
    "profile",
    [
        "client\n",
        "remote foo 0",
        "remote foo 65536",
        "remote foo bad",
        "remote foo",
        "remote foo 1194 tcp",
        "remote [::1] 1194",
        "remote 'secret 1194",
        "proto tcp\nremote foo 1194",
    ],
)
def test_external_rejects_unusable_profile_endpoints(remote_files, profile):
    path = remote_files / "site1/gateway.ovpn"
    path.write_text(profile)
    with pytest.raises(ValueError) as error:
        peers.profile_endpoints(path)
    assert "site1/gateway.ovpn" in str(error.value)
    assert "secret" not in str(error.value)


def test_external_uses_only_explicit_ca_or_system_trust(remote_files, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/old-lab/ca.crt")
    monkeypatch.setenv("SSL_CERT_DIR", "/other/trust")
    config = {"SERVER_URL": "https://api.example", "SSL_CERT_FILE": "/download/ca.crt"}
    env = peers.gateway_environment(1, config, external=True)
    assert env["SERVER_URL"] == config["SERVER_URL"]
    assert "SSL_CERT_FILE" not in env and "SSL_CERT_DIR" not in env
    initialize_https(remote_files, "api.example")
    (remote_files / "https-ca.pem").write_bytes((remote_files / "https/cert.pem").read_bytes())
    env = peers.gateway_environment(1, config, external=True)
    assert env["SSL_CERT_FILE"] == str(remote_files / "https-ca.pem")
    assert "SSL_CERT_DIR" not in env
    assert env["OPENVPN_CONFIG"] == str(remote_files / "site1/gateway.ovpn")
    monkeypatch.setattr(peers.socket, "getaddrinfo", resolver)
    peers.external_configuration()  # Real SSL context can load the selected public certificate.
    (remote_files / "https-ca.pem").write_text("not a certificate")
    with pytest.raises(ValueError, match="Invalid or unreadable /lab/https-ca.pem"):
        peers.external_configuration()


def test_default_environment_preserves_local_lab_contract(remote_files):
    env = peers.gateway_environment(2, {"SERVER_URL": "https://remote.example"})
    assert env["SERVER_URL"] == "https://nginx"
    assert env["SSL_CERT_FILE"] == "/lab/ca.crt"
    assert env["GATEWAY_WIFI_CHECK_INTERVAL"] == "5"
