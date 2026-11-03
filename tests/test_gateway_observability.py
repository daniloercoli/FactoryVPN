import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from api.schemas.gateway import GatewayRegisterRequest
from factoryvpn_common.observability import TechnicalEvent, SystemReport
from factoryvpn_common.technical_logs import LogQueue
from factoryvpn_common.system_info import SystemCollector


async def test_deleted_gateway_does_not_block_worker_logs_for_other_gateways():
    queue = LogQueue("worker")
    deleted, live = str(uuid4()), str(uuid4())
    for _ in range(60):
        queue.emit("worker_api_failed", uuid4(), route=deleted)
    queue.emit("worker_api_failed", uuid4(), route=live)
    attempted = []

    def reply(request):
        attempted.append(request.url.path)
        return httpx.Response(404 if deleted in request.url.path else 200)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(reply)
    ) as client:
        assert await queue.deliver_once(client, "/gateways/{route}/technical-logs") <= 15
        assert len(queue.events) == 1
        assert not queue.frozen
        await queue.deliver_once(client, "/gateways/{route}/technical-logs")
    assert attempted == [f"/gateways/{deleted}/technical-logs", f"/gateways/{live}/technical-logs"]
    assert not queue.events


def test_sms_without_mac_and_wifi_without_modem_identifiers():
    base = dict(
        gateway_id=uuid4(),
        lan_interface="eth0",
        lan_address="192.168.1.1",
        lan_cidr="192.168.1.0/24",
    )
    sms = GatewayRegisterRequest(**base, activation_mode="sms", phone_number="+393331234567")
    assert sms.mac_address is None
    with pytest.raises(ValidationError):
        GatewayRegisterRequest(**base, activation_mode="wifi")
    wifi = GatewayRegisterRequest(**base, activation_mode="wifi", mac_address="aa-bb-cc-dd-ee-ff")
    assert wifi.phone_number is None and wifi.mac_address == "AA:BB:CC:DD:EE:FF"


@pytest.mark.parametrize(
    "change",
    [
        {"code": "secret-token"},
        {"parameters": {"password": "secret"}},
        {"parameters": {"http_status": "secret"}},
        {"message": "secret"},
        {"code": "tunnel_failed"},
        {"parameters": {"exit_code": 1}},
    ],
)
def test_log_contract_rejects_free_text_and_unscoped_session_events(change):
    now = datetime.now(timezone.utc)
    body = dict(event_id=uuid4(), observed_at=now, first_observed_at=now, code="agent_started")
    with pytest.raises(ValidationError):
        TechnicalEvent(**(body | change))


def test_inventory_uses_real_wifi_interface_and_never_ethernet(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "factoryvpn_common.system_info.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout="OpenVPN 2.6.14 test"),
    )
    monkeypatch.setattr(
        "factoryvpn_common.system_info.platform.freedesktop_os_release",
        lambda: {"ID": "debian", "VERSION_ID": "12"},
    )
    interface = tmp_path / "class/net/wlan0"
    interface.mkdir(parents=True)
    (interface / "address").write_text("aa:bb:cc:dd:ee:ff\n")
    collector = SystemCollector(wifi_interface="wlan0", sys_root=tmp_path)
    report = collector.collect()
    assert report.components["wifi"].state == "unsupported"
    assert report.components["modem"].state == "not_applicable"
    (interface / "wireless").mkdir()
    report = collector.collect()
    assert report.components["wifi"].mac_address == "AA:BB:CC:DD:EE:FF"
    assert report.components["openvpn"].version == "2.6.14"
    assert report.components["system"].model == "debian"
    assert report.components["agent"].version


def test_modem_identity_cached_and_probe_deferred_during_activation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "factoryvpn_common.system_info.subprocess.run", Mock(side_effect=FileNotFoundError)
    )
    modem = Mock()
    modem.inventory.return_value = {"imei": "123456789012345", "model": "test modem"}
    collector = SystemCollector(modem=modem, can_probe_modem=lambda: False, sys_root=tmp_path)
    assert collector.collect().components["modem"].state == "collection_failed"
    modem.inventory.assert_not_called()
    collector.can_probe_modem = lambda: True
    assert collector.collect().components["modem"].imei == "123456789012345"
    collector.collect()
    assert modem.inventory.call_count == 1


async def test_stuck_collector_keeps_only_one_probe(monkeypatch):
    collector = SystemCollector()
    gate = asyncio.Event()

    async def blocked(*args):
        await gate.wait()
        return None

    task = asyncio.create_task(blocked())
    collector._task = task
    for _ in range(3):
        with pytest.raises(TimeoutError):
            await collector.collect_async()
    assert collector._task is task
    gate.set()
    await task


def test_system_contract_does_not_accept_modem_identity_in_other_components():
    with pytest.raises(ValidationError):
        SystemReport(
            report_id=uuid4(),
            observed_at=datetime.now(timezone.utc),
            components={"agent": {"state": "reported", "imei": "123456789012345"}},
        )


async def test_retry_freezes_events_and_does_not_lose_occurrences():
    queue = LogQueue("agent")
    session = uuid4()
    queue.emit("tunnel_timeout", session)
    queue.emit("tunnel_timeout", session)
    _, batch = queue.batch()
    assert batch[0].occurrences == 2
    queue.emit("tunnel_timeout", session)
    assert len(queue.events) == 2
    # Lost HTTP response: original UUID and payload remain immutable for retry.
    assert queue.batch()[1][0] == batch[0]
    queue.acknowledge(batch)
    assert queue.batch()[1][0].occurrences == 1
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="https://example.test",
    ) as client:
        await queue.deliver_once(client, "/api/v1/gateways/technical-logs")
    assert not queue.events


def test_queue_bound_and_repetition_does_not_cross_sessions():
    queue = LogQueue("agent", max_events=3)
    for _ in range(10):
        queue.emit("tunnel_timeout", uuid4())
    assert len(queue.events) == 3 and queue.dropped == 7
    _, batch = queue.batch()
    queue.acknowledge(batch)
    assert len(queue.events) <= 3
    assert all(event.session_id for _, event, _ in queue.events.values())


def test_all_expired_events_still_deliver_a_scoped_drop_summary():
    queue = LogQueue("agent")
    session = uuid4()
    queue.emit("tunnel_timeout", session)
    identifier, (key, event, size) = next(iter(queue.events.items()))
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    queue.events[identifier] = (
        key,
        event.model_copy(update={"first_observed_at": old, "observed_at": old}),
        size,
    )
    _, events = queue.batch()
    assert len(events) == 1 and events[0].code == "logs_dropped"
    assert events[0].session_id == session and events[0].parameters.dropped == 1


async def test_revoked_credentials_discard_queue_and_old_server_backs_off():
    queue = LogQueue("agent")
    queue.emit("agent_started")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)),
        base_url="https://example.test",
    ) as client:
        assert await queue.deliver_once(client, "/technical-logs") == 3600
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
        base_url="https://example.test",
    ) as client:
        await queue.deliver_once(client, "/technical-logs")
    assert not queue.events


async def test_sms_sleep_never_calls_log_transport(monkeypatch):
    queue = LogQueue("agent")
    queue.emit("agent_started")
    monkeypatch.setattr(queue, "deliver_once", AsyncMock())
    task = asyncio.create_task(
        queue.run("https://example.test", {}, "/technical-logs", lambda: False)
    )
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    queue.deliver_once.assert_not_called()


def test_modem_optional_response_does_not_discard_imei(load_script):
    import threading

    module = load_script("gateway-agent", "sms_handler")
    handler = module.SMSHandler.__new__(module.SMSHandler)
    handler.timeout, handler._lock = 5, threading.RLock()
    handler._control_pending = threading.Event()
    handler._command = Mock(
        side_effect=[
            "123456789012345\r\nOK\r\n",
            RuntimeError(),
            "test modem\r\nOK\r\n",
            "unexpected\nmultiline\r\nOK\r\n",
        ]
    )
    assert handler.inventory() == {"imei": "123456789012345", "model": "test modem"}
    assert handler.timeout == 5
    handler._control_pending.set()
    handler._command.reset_mock()
    with pytest.raises(TimeoutError):
        handler.inventory()
    handler._command.assert_not_called()


async def test_system_report_is_sent_only_once_during_sms_sleep(monkeypatch):
    from factoryvpn_common import system_info

    collector = Mock()
    collector.collect_async = AsyncMock(
        return_value=SystemReport(
            report_id=uuid4(),
            observed_at=datetime.now(timezone.utc),
            components={"agent": {"state": "reported", "version": "1.0"}},
        )
    )
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200, request=httpx.Request("POST", "https://example.test")
    )
    manager = AsyncMock()
    manager.__aenter__.return_value = client
    monkeypatch.setattr(system_info.httpx, "AsyncClient", Mock(return_value=manager))
    sleeps = 0

    async def tick(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 5:
            raise asyncio.CancelledError()

    monkeypatch.setattr(system_info.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await system_info.system_info_loop(
            "https://example.test", {}, collector, LogQueue("agent"), lambda: False
        )
    assert collector.collect_async.await_count == client.post.await_count == 1
