"""Guasti API temporanei, scadenza non prorogata e rifiuti espliciti."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import time

import httpx
import pytest

from factoryvpn_common.network import SessionNetwork
from factoryvpn_common.protocol import activation_message
from test_gateway_modes import agent_fixture, KEY
from test_lifecycle import plan


def api_error(kind):
    request = httpx.Request("POST", "https://api.example.test/control")
    if kind == "timeout":
        return httpx.ReadTimeout("timeout", request=request)
    if kind == "connection":
        return httpx.ConnectError("offline", request=request)
    if kind == "invalid":
        return ValueError("Invalid API response")
    return httpx.HTTPStatusError(
        "API error", request=request, response=httpx.Response(kind, request=request)
    )


def authorization(item):
    return {"connect": True, "network": item["network"], "lease_seconds": item["lease_seconds"]}


@pytest.mark.parametrize("failure", ["timeout", "connection", 408, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("heartbeat", [0, 1])
async def test_gateway_outage_preserves_network_without_renewing(load_script, failure, heartbeat):
    agent = agent_fixture(load_script, "wifi")
    item = plan()
    state = authorization(item)
    agent.heartbeat.send.return_value = state
    agent.heartbeat.report = AsyncMock()
    agent.vpn.is_running.return_value = True
    await agent.step()
    installed = agent.spec
    agent.network.reset_mock()

    # Due errori consecutivi, anche dopo una prima risposta valida nello stesso
    # ciclo, non devono riscrivere il firewall né riavviare OpenVPN.
    for _ in range(2):
        agent.heartbeat.send.side_effect = [state] * heartbeat + [api_error(failure)]
        with pytest.raises(httpx.HTTPError) as error:
            await agent.step()
        await agent.handle_failure(error.value)
        assert agent.spec == installed and agent.interval == 5
        agent.network.configure_session.assert_not_called()
        agent.network.disable.assert_not_called()
        agent.vpn.stop.assert_not_awaited()

    agent.heartbeat.send.side_effect = None
    await agent.step()
    agent.network.configure_session.assert_called_once()
    agent.vpn.stop.assert_not_awaited()


@pytest.mark.parametrize("failure", [400, 401, 403, 404, 409, 422, "invalid"])
async def test_gateway_explicit_rejection_closes_existing_network(load_script, failure):
    agent = agent_fixture(load_script, "wifi")
    agent.spec = SessionNetwork(**plan()["network"])
    agent.vpn.is_running.return_value = True
    await agent.handle_failure(api_error(failure))
    agent.network.disable.assert_called_once()
    agent.vpn.stop.assert_awaited_once()
    assert agent.spec is None


async def test_gateway_outage_without_installed_lease_does_not_keep_tunnel(load_script):
    agent = agent_fixture(load_script, "wifi")
    agent.vpn.is_running.return_value = True
    await agent.handle_failure(api_error(503))
    agent.vpn.stop.assert_awaited_once()
    agent.network.configure_session.assert_not_called()


async def test_gateway_logout_after_outage_still_withdraws_network(load_script):
    agent = agent_fixture(load_script, "wifi")
    agent.spec = SessionNetwork(**plan()["network"])
    agent.vpn.is_running.return_value = True
    await agent.handle_failure(api_error(503))
    agent.heartbeat.send.return_value = {"connect": False}
    await agent.step()
    agent.network.disable.assert_called_once()
    agent.vpn.stop.assert_awaited_once()
    assert agent.spec is None


async def test_sms_confirmation_outage_preserves_lease_and_recovers_pending_receipt(load_script):
    item = plan()
    sms = SimpleNamespace(id=3, body=activation_message(item["session_id"], time.time() + 300, KEY))
    agent = agent_fixture(load_script, "sms", sms)
    agent.vpn.is_running.return_value = True
    agent.heartbeat.report = AsyncMock()
    state = authorization(item)
    agent.heartbeat.send.side_effect = [state, state, api_error(503)]
    with pytest.raises(httpx.HTTPError) as error:
        await agent.step()
    await agent.handle_failure(error.value)
    # Il lease proviene dalla risposta valida ricevuta prima dell'installazione:
    # la conferma persa non aggiunge un secondo rinnovo e conserva l'SMS pendente.
    agent.network.configure_session.assert_called_once()
    agent.network.disable.assert_not_called()
    agent.vpn.stop.assert_not_awaited()
    agent.modem.delete_sms.assert_not_called()
    agent.heartbeat.send.side_effect = None
    agent.heartbeat.send.return_value = state
    await agent.step()
    agent.modem.delete_sms.assert_called_once_with(3)
    assert agent.activation_sms_id is None


@pytest.fixture
def worker_state(load_script, monkeypatch):
    module = load_script("session-worker", "main")
    item = plan()
    clock = [100.0]
    # L'orologio sostituito appartiene al worker, non al loop asyncio del test.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    firewall, vpn = Mock(), Mock()
    vpn.clients.return_value = {
        item["gateway_cn"]: item["network"]["gateway_ip"],
        item["technician_cn"]: item["network"]["technician_ip"],
    }
    deadline = [None]

    def ensure(spec, lease_seconds=30, *, renew=True):
        if renew:
            deadline[0] = clock[0] + lease_seconds

    firewall.ensure.side_effect = ensure
    firewall.delete.side_effect = lambda spec: deadline.__setitem__(0, None)
    return SimpleNamespace(
        module=module,
        item=item,
        clock=clock,
        firewall=firewall,
        vpn=vpn,
        deadline=deadline,
    )


@pytest.mark.parametrize("failure", ["timeout", "connection", 408, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("ttl", [20, 300, 600])
async def test_worker_ready_outage_never_extends_previous_deadline(worker_state, failure, ttl):
    s = worker_state
    s.item["lease_seconds"] = ttl
    response_error = [None]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[s.item])
        if response_error[0] is not None:
            raise api_error(response_error[0])
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        worker = s.module.SessionWorker(client, s.firewall, s.vpn)
        await worker.reconcile()
        assert s.deadline[0] == 100 + ttl
        response_error[0] = failure
        # Anche GET validi ripetuti non possono rinnovare senza conferma ready.
        for now in (105, 100 + ttl / 2, 101 + ttl):
            s.clock[0] = now
            await worker.reconcile()
            assert s.deadline[0] == 100 + ttl
            s.firewall.delete.assert_not_called()
        response_error[0] = None
        await worker.reconcile()
        assert s.deadline[0] == 101 + ttl * 2


@pytest.mark.parametrize("failure", [401, 403, 404, 409, "invalid"])
async def test_worker_ready_rejection_withdraws_previous_lease(worker_state, failure):
    s = worker_state
    s.deadline[0] = 120

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[s.item])
        raise api_error(failure)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        await s.module.SessionWorker(client, s.firewall, s.vpn).reconcile()
    assert s.deadline[0] is None
    s.firewall.ensure.assert_called_once_with(SessionNetwork(**s.item["network"]), renew=False)


@pytest.mark.parametrize("failure", [503, "timeout", 401, 403, "invalid"])
async def test_worker_snapshot_error_preserves_only_for_temporary_failures(worker_state, failure):
    s = worker_state

    def handler(request):
        raise api_error(failure)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises((httpx.HTTPError, ValueError)):
            await s.module.SessionWorker(client, s.firewall, s.vpn).reconcile()
    s.firewall.ensure.assert_not_called()
    if failure in (503, "timeout"):
        s.firewall.apply.assert_not_called()
    else:
        s.firewall.apply.assert_called_once_with([])


@pytest.mark.parametrize("failure", [503, "timeout"])
async def test_worker_first_ready_failure_does_not_authorize_new_network(worker_state, failure):
    s = worker_state

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[s.item])
        raise api_error(failure)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        await s.module.SessionWorker(client, s.firewall, s.vpn).reconcile()
    assert s.deadline[0] is None
    s.firewall.ensure.assert_called_once_with(SessionNetwork(**s.item["network"]), renew=False)


@pytest.mark.parametrize("delay", [7, 21])
async def test_worker_api_wait_consumes_lease_instead_of_extending_it(worker_state, delay):
    s = worker_state

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[s.item])
        if request.url.path.endswith("/ready"):
            s.clock[0] += delay
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        await s.module.SessionWorker(client, s.firewall, s.vpn).reconcile()
    assert s.deadline[0] == (120 if delay < 20 else None)


@pytest.mark.parametrize("status", ["CLOSED", "FAILED", "EXPIRED"])
async def test_worker_terminal_session_stays_blocked_when_cleanup_ack_fails(worker_state, status):
    s = worker_state
    s.item["status"] = status
    s.deadline[0] = 120
    s.vpn.clients.side_effect = [s.vpn.clients.return_value, {}]

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[s.item])
        return httpx.Response(503)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        await s.module.SessionWorker(client, s.firewall, s.vpn).reconcile()
    assert s.deadline[0] is None
    s.firewall.ensure.assert_not_called()
    s.firewall.apply.assert_called_once_with([])
    assert s.vpn.kill.call_count == 2


async def test_worker_local_network_failure_remains_blocking(worker_state):
    s = worker_state
    s.deadline[0] = 120
    s.firewall.ensure.side_effect = RuntimeError("Network setup failed")

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[s.item])
        assert not request.url.path.endswith("/ready")
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        await s.module.SessionWorker(client, s.firewall, s.vpn).reconcile()
    assert s.deadline[0] is None
    s.firewall.delete.assert_called_once()


@pytest.mark.parametrize("delay", [15, 301])
async def test_gateway_delayed_authorization_consumes_five_minute_lease(
    load_script, monkeypatch, delay
):
    import sys

    agent = agent_fixture(load_script, "wifi")
    module = sys.modules[type(agent).__module__]
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    item = plan()
    item["lease_seconds"] = 300
    agent.spec = SessionNetwork(**item["network"])
    agent.vpn.is_running.return_value = True
    agent.heartbeat.report = AsyncMock()
    calls = 0

    async def heartbeat(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            clock[0] += delay
        return authorization(item)

    agent.heartbeat.send.side_effect = heartbeat
    if delay > 300:
        with pytest.raises(RuntimeError) as error:
            await agent.step()
        await agent.handle_failure(error.value)
        agent.network.configure_session.assert_not_called()
        agent.vpn.stop.assert_awaited_once()
    else:
        await agent.step()
        assert agent.network.configure_session.call_args.args[-1] == 300 - delay
