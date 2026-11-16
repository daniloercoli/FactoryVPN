"""Contratto Gateway Only: coppie IPv4 e lease, senza filtri di porte o reti cliente."""

from uuid import UUID
from unittest.mock import Mock, AsyncMock
from types import SimpleNamespace
import json
import pytest
from factoryvpn_common.network import SessionNetwork, lease_ttl, interface_name


def spec(number=1):
    return SessionNetwork(str(UUID(int=number)), f"10.100.128.{number+1}", f"10.100.0.{number+1}")


def test_sessions_use_complete_identity_and_no_lan_resources():
    assert spec(1).lease_set != spec(2).lease_set
    assert set(spec().to_dict()) == {"session_id", "technician_ip", "gateway_ip"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("technician_ip", "8.8.8.8"),
        ("gateway_ip", "10.100.0.1"),
        ("gateway_ip", "10.100.0.0"),
        ("gateway_ip", "10.100.128.2"),
    ],
)
def test_invalid_network_plans_are_rejected(field, value):
    data = spec().to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        SessionNetwork(**data)


def test_network_inputs_never_accept_shell_fragments():
    with pytest.raises(ValueError):
        interface_name("eth0;reboot")
    with pytest.raises(ValueError):
        lease_ttl(0)


def test_firewall_snapshot_does_not_renew_existing_leases(load_script):
    a, b = spec(1), spec(2)
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps({"nftables": [{"set": {"name": a.lease_set}}]})
        )
    )
    manager = load_script("session-worker", "firewall_manager").FirewallManager(runner=runner)
    manager.apply([a, b])
    script = runner.call_args.kwargs["input"]
    assert "timeout " not in script
    assert f"add set inet factoryvpn_support {a.lease_set}" not in script
    assert f"add set inet factoryvpn_support {b.lease_set}" in script
    for session in (a, b):
        assert f"ip saddr . ip daddr @{session.lease_set} accept" in script
        assert f"ip daddr . ip saddr @{session.lease_set} accept" in script
    assert 'iifname "tun0" drop' in script and 'oifname "tun0" drop' in script
    assert not any(restriction in script for restriction in ("dport", "sport", "ct state"))
    manager.ensure(b, renew=False)
    assert runner.call_args.kwargs["input"] == script
    manager.ensure(b, 7)
    assert "timeout 7s" in runner.call_args.kwargs["input"]
    manager.apply([b])
    assert f"delete set inet factoryvpn_support {a.lease_set}" in runner.call_args.kwargs["input"]


def test_gateway_only_local_services_and_both_directions_expire(load_script):
    runner = Mock(return_value=SimpleNamespace(returncode=1))
    manager = load_script("gateway-agent", "network").NetworkManager(runner=runner)
    manager.configure_session(spec(), lease_seconds=7)
    policy = runner.call_args.kwargs["input"]
    assert "timeout 7s" in policy
    assert f"ip saddr @technician ip daddr {spec().gateway_ip} accept" in policy
    assert f"ip saddr {spec().gateway_ip} ip daddr @technician accept" in policy
    assert not any(restriction in policy for restriction in ("dport", "sport", "ct state"))
    forward = policy.split("chain forward")[1]
    assert "accept\n" not in forward
    assert "masquerade" not in policy and "eth0" not in policy
    manager.disable()
    assert "accept\n" not in runner.call_args.kwargs["input"]
    assert all(call.args[0][0] == "nft" for call in runner.call_args_list)


@pytest.mark.parametrize("kind", ["technician", "gateway"])
async def test_openvpn_pushes_peer_host_route_without_lan_or_internet_changes(monkeypatch, kind):
    from api.routers import internal

    network = spec()
    address = network.technician_ip if kind == "technician" else network.gateway_ip
    other = network.gateway_ip if kind == "technician" else network.technician_ip
    monkeypatch.setattr(
        internal, "authorized_peer", AsyncMock(return_value=(kind, None, network, address))
    )
    result = await internal.authorize_vpn(
        SimpleNamespace(common_name="test", certificate_serial="1"),
        SimpleNamespace(commit=AsyncMock()),
    )
    assert result["config"].splitlines() == [
        f"ifconfig-push {address} 255.255.0.0",
        f'push "route {other} 255.255.255.255"',
    ]


def test_registration_no_longer_accepts_lan_configuration():
    from api.schemas.gateway import GatewayRegisterRequest

    request = {
        "gateway_id": str(UUID(int=1)),
        "activation_mode": "wifi",
        "mac_address": "02:00:00:00:00:01",
    }
    assert GatewayRegisterRequest(**request).gateway_id == UUID(int=1)
    for key, value in (
        ("lan_cidr", "192.168.1.0/24"),
        ("lan_address", "192.168.1.1"),
        ("lan_interface", "eth0"),
    ):
        with pytest.raises(ValueError):
            GatewayRegisterRequest(**request, **{key: value})
