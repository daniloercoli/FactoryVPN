"""Failures, recovery and authorization at the agent/worker protocol boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
import io

import httpx
import pytest

from factoryvpn_common.network import SessionNetwork
from factoryvpn_common.protocol import activation_message, verify_activation, peer_name
from factoryvpn_common.openvpn import ManagementClient


def plan():
    spec = SessionNetwork(str(uuid4()), 1, "10.100.128.2", "10.100.0.2", "192.168.1.0/24")
    return {
        "session_id": spec.session_id,
        "network": spec.to_dict(),
        "status": "REQUESTED",
        "technician_cn": peer_name("technician", uuid4()),
        "gateway_cn": peer_name("gateway", uuid4()),
        "gateway_ready": True,
        "activation_confirmed": True,
        "lease_seconds": 20,
    }


def test_activation_is_bound_to_session_expiry_and_gateway_key():
    identifier = uuid4()
    message = activation_message(identifier, 1000, "secret")
    assert len(message) <= 160
    assert verify_activation(message, "secret", now=999) == str(identifier)
    assert verify_activation(message, "secret", now=1000) is None
    assert verify_activation(message, "other", now=999) is None
    assert (
        verify_activation(message.replace(str(identifier), str(uuid4())), "secret", now=999) is None
    )
    assert verify_activation("FACTORYVPN: activate", "secret") is None


def test_management_parses_full_status_and_does_not_accept_greeting_as_kill_reply(monkeypatch):
    class Socket:
        response = ">INFO:OpenVPN Management Interface\nERROR: client not found\n"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            pass

        def connect(self, value):
            pass

        def sendall(self, value):
            pass

        def makefile(self, *args, **kwargs):
            return io.StringIO(self.response)

    monkeypatch.setattr("factoryvpn_common.openvpn.socket.socket", lambda *a: Socket())
    client = ManagementClient()
    assert client.kill(peer_name("gateway", uuid4())) is False
    names = [peer_name("technician", uuid4()) for _ in range(75)]
    Socket.response = (
        ">INFO:hello\n"
        + "\n".join(
            f"CLIENT_LIST,{name},127.0.0.1,10.100.128.{i+2}" for i, name in enumerate(names)
        )
        + "\nEND\n"
    )
    assert len(client.clients()) == 75
    Socket.response = "CLIENT_LIST,broken\nEND\n"
    with pytest.raises(ValueError):
        client.clients()
    with pytest.raises(ValueError):
        client.kill("someone\nkill all")


async def test_worker_activates_only_after_both_peers_are_connected(load_script):
    module = load_script("session-worker", "main")
    item = plan()
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[item])
        posts.append(request.url.path)
        return httpx.Response(200, json={})

    ns, fw, vpn = Mock(), Mock(), Mock()
    vpn.clients.return_value = {item["gateway_cn"]: item["network"]["gateway_ip"]}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        worker = module.SessionWorker(client, ns, fw, vpn)
        await worker.reconcile()
        ns.ensure.assert_not_called()
        assert posts[-1].endswith("/waiting")
        vpn.clients.return_value[item["technician_cn"]] = item["network"]["technician_ip"]
        await worker.reconcile()
        ns.ensure.assert_called_once()
        assert posts[-1].endswith("/ready")


async def test_worker_cleanup_acknowledgement_waits_for_real_disconnect(load_script):
    module = load_script("session-worker", "main")
    item = plan()
    item["status"] = "CLOSED"
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[item])
        posts.append(request.url.path)
        return httpx.Response(200)

    ns, fw, vpn = Mock(), Mock(), Mock()
    vpn.clients.return_value = {item["gateway_cn"]: item["network"]["gateway_ip"]}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        worker = module.SessionWorker(client, ns, fw, vpn)
        await worker.reconcile()
        assert posts == [f"/api/v1/internal/sessions/{item['session_id']}/diagnostics"]
        ns.delete.assert_called_once()
        vpn.clients.return_value = {}
        await worker.reconcile()
        assert posts[-1].endswith("/cleaned")


async def test_worker_api_failure_never_renews_a_lease(load_script):
    module = load_script("session-worker", "main")
    ns, fw, vpn = Mock(), Mock(), Mock()
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await module.SessionWorker(client, ns, fw, vpn).reconcile()
    ns.ensure.assert_not_called()
    ns.delete.assert_not_called()
    fw.apply.assert_not_called()


async def test_gateway_rechecks_session_after_handshake_and_cleans_up(load_script):
    module = load_script("gateway-agent", "main")
    item = plan()
    modem = Mock()
    modem.list_sms.return_value = []
    vpn = Mock()
    vpn.start = AsyncMock()
    vpn.stop = AsyncMock()
    vpn.is_running.return_value = False
    vpn.get_tunnel_ip.return_value = item["network"]["gateway_ip"]
    network, heartbeat = Mock(), Mock()
    heartbeat.send = AsyncMock(
        side_effect=[
            {"connect": True, "network": item["network"], "lease_seconds": 20},
            {"connect": False},
        ]
    )
    config = SimpleNamespace(
        VPN_INTERFACE="tun0", LAN_INTERFACE="eth0", GATEWAY_API_KEY="key", ACTIVATION_MODE="wifi"
    )
    agent = module.GatewayAgent(modem, vpn, network, heartbeat, config)
    await agent.step()
    vpn.start.assert_awaited_once()
    vpn.stop.assert_awaited_once()
    network.configure_session.assert_not_called()
    network.disable_nat.assert_called_once()


async def test_openvpn_hook_reads_real_arguments_and_environment(
    load_script, monkeypatch, tmp_path
):
    hook = load_script("session-worker", "openvpn_hooks")
    monkeypatch.setenv("common_name", "gateway-example")
    monkeypatch.setenv("tls_serial_0", "1234")
    call = Mock(return_value={"config": "ifconfig-push 10.100.0.2 255.255.0.0\n"})
    monkeypatch.setattr(hook, "api_call", call)
    output = tmp_path / "client.conf"
    hook.main(["connect", str(output)])
    call.assert_called_with("authorize", "gateway-example", certificate_serial="1234")
    assert output.read_text().startswith("ifconfig-push")
    hook.main(["learn", "add", "10.100.0.2", "gateway-example"])
    call.assert_called_with("learn", "gateway-example", "10.100.0.2")


async def test_gateway_renewal_keeps_its_installed_session_ready(load_script):
    module = load_script("gateway-agent", "main")
    item = plan()
    spec = SessionNetwork(**item["network"])
    modem = Mock()
    modem.list_sms.return_value = []
    vpn = Mock()
    vpn.start = AsyncMock()
    vpn.stop = AsyncMock()
    vpn.is_running.return_value = True
    vpn.get_tunnel_ip.return_value = spec.gateway_ip
    network, heartbeat = Mock(), Mock()
    heartbeat.send = AsyncMock(
        return_value={"connect": True, "network": item["network"], "lease_seconds": 20}
    )
    config = SimpleNamespace(
        VPN_INTERFACE="tun0", LAN_INTERFACE="eth0", GATEWAY_API_KEY="key", ACTIVATION_MODE="wifi"
    )
    agent = module.GatewayAgent(modem, vpn, network, heartbeat, config)
    agent.spec = spec
    await agent.step()
    assert all(
        call.args == (spec.gateway_ip, spec.session_id, None)
        for call in heartbeat.send.call_args_list
    )
    network.configure_session.assert_called_once()
    vpn.stop.assert_not_called()


async def test_worker_withdraws_network_when_activation_races_with_close(load_script):
    module = load_script("session-worker", "main")
    item = plan()
    ns, fw, vpn = Mock(), Mock(), Mock()
    vpn.clients.return_value = {
        item["gateway_cn"]: item["network"]["gateway_ip"],
        item["technician_cn"]: item["network"]["technician_ip"],
    }

    def handler(request):
        return httpx.Response(200, json=[item]) if request.method == "GET" else httpx.Response(409)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        await module.SessionWorker(client, ns, fw, vpn).reconcile()
    ns.ensure.assert_called_once()
    ns.delete.assert_called_once_with(SessionNetwork(**item["network"]))
