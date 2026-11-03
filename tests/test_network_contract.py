from uuid import UUID
from unittest.mock import Mock
from types import SimpleNamespace

import pytest
from factoryvpn_common.network import SessionNetwork, lease_ttl, interface_name


def spec(number=1):
    return SessionNetwork(
        str(UUID(int=number)),
        number,
        f"10.100.128.{number+1}",
        f"10.100.0.{number+1}",
        "192.168.1.0/24",
    )


def test_network_allocations_do_not_overlap_or_truncate_session_ids():
    a, b = spec(1), spec(2)
    assert a.namespace != b.namespace  # both UUIDs have the same first eight characters
    assert len({a.host_ip, a.peer_ip, b.host_ip, b.peer_ip}) == 4
    assert a.table != b.table
    assert len(a.host_interface) <= 15


@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", 0),
        ("slot", 16384),
        ("technician_ip", "8.8.8.8"),
        ("gateway_ip", "10.100.0.1"),
        ("lan_cidr", "10.100.0.0/24"),
        ("lan_cidr", "10.250.0.0/24"),
    ],
)
def test_invalid_network_plans_are_rejected(field, value):
    data = spec().to_dict()
    data[field] = value
    with pytest.raises(ValueError):
        SessionNetwork(**data)


def test_firewall_replacement_keeps_other_session_rules(load_script):
    module = load_script("session-worker", "firewall_manager")
    runner = Mock(return_value=SimpleNamespace(returncode=0))
    manager = module.FirewallManager(runner=runner)
    a, b = spec(1), spec(2)
    manager.apply([a, b])
    script = runner.call_args.kwargs["input"]
    assert a.technician_ip in script and b.technician_ip in script
    manager.apply([b])
    script = runner.call_args.kwargs["input"]
    assert a.technician_ip not in script and b.technician_ip in script
    assert 'iifname "tun0" drop' in script


def test_network_inputs_never_accept_shell_fragments():
    with pytest.raises(ValueError):
        interface_name("eth0;reboot")
    with pytest.raises(ValueError):
        lease_ttl(0)


@pytest.mark.parametrize("fail_policy", [False, True])
def test_new_namespace_cannot_forward_before_timed_policy_exists(load_script, fail_policy):
    import subprocess

    module = load_script("session-worker", "namespace_manager")
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        if command[-3:] == ["nft", "-f", "-"] and fail_policy:
            raise subprocess.CalledProcessError(1, command)
        if command[:3] == ["ip", "link", "show"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    manager = module.NamespaceManager(runner=runner)
    if fail_policy:
        with pytest.raises(subprocess.CalledProcessError):
            manager.ensure(spec())
    else:
        manager.ensure(spec())
    policy = next(i for i, c in enumerate(commands) if c[-3:] == ["nft", "-f", "-"])
    disable = next(i for i, c in enumerate(commands) if "net.ipv4.ip_forward=0" in c)
    enable = [i for i, c in enumerate(commands) if "net.ipv4.ip_forward=1" in c]
    assert disable < policy
    assert enable == ([] if fail_policy else [len(commands) - 1])
    assert not enable or enable[0] > policy


def test_gateway_route_commands_are_executable_and_use_server_as_next_hop(load_script):
    import subprocess

    module = load_script("gateway-agent", "network")
    commands = []

    def runner(command, **kwargs):
        # Exercise subprocess argument conversion without changing any host network.
        subprocess.run(["/usr/bin/true", *command], check=True)
        commands.append(command)
        missing = command[:4] == ["ip", "-j", "-d", "link"]
        return subprocess.CompletedProcess(command, 1 if missing else 0, stdout="", stderr="")

    module.NetworkManager(runner=runner).configure_session(spec())
    route = next(c for c in commands if c[:3] == ["ip", "route", "replace"] and "via" in c)
    assert route[3] == spec().peer_ip + "/32"
    assert route[route.index("via") + 1] == "10.100.0.1"
    assert route[route.index("dev") + 1] == "tun0"
