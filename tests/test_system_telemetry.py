import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError
from api.schemas.telemetry import ResourceMetrics
from factoryvpn_common.telemetry import ResourceSampler, report_once


def test_sampler_uses_deltas_available_memory_and_available_disk(tmp_path, monkeypatch):
    (tmp_path / "stat").write_text("cpu 10 0 10 80 0 0 0 0 10 0\n")
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 10 kB\nMemAvailable: 400 kB\n")
    (tmp_path / "uptime").write_text("123.45 999\n")
    monkeypatch.setattr(
        "os.statvfs", lambda path: SimpleNamespace(f_blocks=1000, f_bavail=300, f_frsize=4096)
    )
    sampler = ResourceSampler(tmp_path)
    assert sampler.sample()["cpu_percent"] is None
    (tmp_path / "stat").write_text("cpu 30 0 10 160 0 0 0 0 30 0\n")
    values = sampler.sample()
    assert values["cpu_percent"] == 20
    assert values["memory_total_bytes"] == 1024000
    assert values["memory_available_bytes"] == 409600
    assert values["disk_available_bytes"] == 300 * 4096
    assert values["uptime_seconds"] == 123
    ResourceMetrics.model_validate(values)
    # A reboot/reset is an unknown sample, never a negative or made-up percentage.
    (tmp_path / "stat").write_text("cpu 1 0 0 1 0 0 0 0\n")
    assert sampler.sample()["cpu_percent"] is None


def test_unreadable_metrics_are_unknown_not_zero(tmp_path, monkeypatch):
    def inaccessible(path):
        raise OSError("unavailable")

    monkeypatch.setattr("os.statvfs", inaccessible)
    values = ResourceSampler(tmp_path).sample()
    assert all(v is None for k, v in values.items() if k not in ("scope", "disk_scope"))


@pytest.mark.parametrize(
    "values",
    [
        {"cpu_percent": 101},
        {"cpu_percent": float("nan")},
        {"cpu_percent": True},
        {"cpu_percent": "25"},
        {"memory_total_bytes": 100, "memory_available_bytes": 101},
        {"disk_total_bytes": 100},
        {"uptime_seconds": -1},
        {"hostname": "private"},
    ],
)
def test_invalid_metrics_are_rejected(values):
    with pytest.raises(ValidationError):
        ResourceMetrics.model_validate(values)


async def test_report_contains_only_metrics_and_fixed_services():
    from unittest.mock import AsyncMock

    sampler = Mock()
    sampler.sample_async = AsyncMock(return_value=ResourceMetrics().model_dump())
    captured = []

    async def handler(request):
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        await report_once(
            client, "/telemetry", sampler, lambda: {"worker": "ok", "openvpn": "unknown"}
        )
    assert set(captured[0]) == {"observed_at", "metrics", "services"}
    assert datetime.fromisoformat(captured[0]["observed_at"]) <= datetime.now(timezone.utc)


async def test_idle_sms_gateway_does_not_make_telemetry_requests(monkeypatch):
    import factoryvpn_common.telemetry as telemetry

    async def stop(_):
        raise asyncio.CancelledError

    report = Mock(side_effect=AssertionError("must not contact API"))
    sampler = ResourceSampler()
    sampler.previous_cpu = (100, 50)
    monkeypatch.setattr(telemetry, "ResourceSampler", lambda: sampler)
    monkeypatch.setattr(telemetry, "report_once", report)
    monkeypatch.setattr(telemetry.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await telemetry.telemetry_loop(
            "http://test", {}, "/telemetry", lambda: {}, enabled=lambda: False
        )
    report.assert_not_called()
    assert sampler.previous_cpu is None


async def test_telemetry_outage_is_retried_without_escaping_the_background_task(monkeypatch):
    import factoryvpn_common.telemetry as telemetry
    from unittest.mock import AsyncMock

    report = AsyncMock(side_effect=httpx.ConnectError("unavailable"))
    waits = 0

    async def wait(_):
        nonlocal waits
        waits += 1
        if waits == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(telemetry, "report_once", report)
    monkeypatch.setattr(telemetry.asyncio, "sleep", wait)
    with pytest.raises(asyncio.CancelledError):
        await telemetry.telemetry_loop("http://test", {}, "/telemetry", lambda: {})
    assert report.await_count == 2


async def test_worker_reports_control_api_failure_even_when_diagnostics_are_not_sent(load_script):
    from test_lifecycle import plan

    module = load_script("session-worker", "main")
    item = plan()

    def handler(request):
        return httpx.Response(200, json=[item]) if request.method == "GET" else httpx.Response(503)

    vpn = Mock()
    vpn.clients.return_value = {}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        worker = module.SessionWorker(client, Mock(), Mock(), vpn)
        await worker.reconcile()
        assert worker.telemetry_services() == {"worker": "error", "openvpn": "ok"}


async def test_gateway_service_failure_is_attributed_to_the_correct_component(load_script):
    import time
    from test_gateway_modes import agent_fixture
    from factoryvpn_common.network import SessionNetwork
    from uuid import uuid4

    agent = agent_fixture(load_script, "wifi")
    agent.last_cycle = time.monotonic()
    agent.cycle_state = "ok"
    spec = SessionNetwork(str(uuid4()), 1, "10.100.128.2", "10.100.0.2", "192.168.1.0/24")
    agent.heartbeat.send.return_value = {
        "connect": True,
        "network": spec.to_dict(),
        "lease_seconds": 20,
    }
    await agent.stop_session()
    agent.vpn.start.side_effect = RuntimeError("Tunnel failed")
    with pytest.raises(RuntimeError):
        await agent.step()
    assert agent.telemetry_services()["openvpn"] == "error"
    assert agent.telemetry_services()["network"] == "idle"
    agent.vpn.start.side_effect = None
    agent.vpn.is_running.return_value = True
    agent.network.configure_session.side_effect = RuntimeError("Network failed")
    with pytest.raises(RuntimeError):
        await agent.step()
    assert agent.telemetry_services()["network"] == "error"
    assert agent.telemetry_services()["openvpn"] == "ok"
    # A later API failure must not reuse the previous diagnostic component.
    await agent.stop_session()
    agent.heartbeat.send.side_effect = httpx.ConnectError("API unavailable")
    with pytest.raises(httpx.ConnectError):
        await agent.step()
    assert agent.telemetry_services()["network"] == "idle"


async def test_network_cleanup_failure_is_not_reported_as_ready(load_script):
    import time
    from test_gateway_modes import agent_fixture

    agent = agent_fixture(load_script, "wifi")
    agent.last_cycle, agent.cycle_state = time.monotonic(), "ok"
    agent.spec = Mock()
    agent.network_state = "ok"
    agent.network.disable_nat.side_effect = RuntimeError("Cleanup failed")
    with pytest.raises(RuntimeError):
        await agent.stop_session()
    assert agent.spec is not None  # Still needed by the next cleanup attempt.
    assert agent.telemetry_services()["network"] == "error"


async def test_network_is_unknown_while_configuration_is_in_progress(load_script):
    import time
    from test_gateway_modes import agent_fixture
    from factoryvpn_common.network import SessionNetwork
    from uuid import uuid4

    agent = agent_fixture(load_script, "wifi")
    agent.last_cycle, agent.cycle_state = time.monotonic(), "ok"
    spec = SessionNetwork(str(uuid4()), 1, "10.100.128.2", "10.100.0.2", "192.168.1.0/24")
    agent.heartbeat.send.return_value = {
        "connect": True,
        "network": spec.to_dict(),
        "lease_seconds": 20,
    }
    observations = []
    agent.network.configure_session.side_effect = lambda *_: observations.append(
        agent.telemetry_services()["network"]
    )
    await agent.step()
    assert observations == ["unknown"]
    assert agent.telemetry_services()["network"] == "ok"


async def test_timed_out_system_probe_does_not_accumulate_threads(monkeypatch):
    import threading

    sampler = ResourceSampler()
    release = threading.Event()
    calls = []

    def slow_probe():
        calls.append(True)
        assert release.wait(timeout=2)
        return ResourceMetrics().model_dump()

    monkeypatch.setattr(sampler, "sample", slow_probe)
    try:
        async with httpx.AsyncClient(base_url="http://test") as client:
            for _ in range(3):
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(0.02):
                        await report_once(client, "/telemetry", sampler, lambda: {})
            assert len(calls) == 1
    finally:
        release.set()
        if sampler._sample_task:
            await sampler._sample_task
    # Discard the late result and sample again with a clean CPU baseline.
    sampler.previous_cpu = (100, 50)
    monkeypatch.setattr(sampler, "sample", lambda: {"baseline": sampler.previous_cpu})
    assert await sampler.sample_async() == {"baseline": None}
