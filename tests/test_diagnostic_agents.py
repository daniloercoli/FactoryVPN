"""Machine observations use safe codes and cannot replace lifecycle control."""

import json
import asyncio
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from test_lifecycle import plan
from test_gateway_modes import agent_fixture


async def test_worker_reports_separate_peers_with_normal_control_request(load_script):
    module = load_script("session-worker", "main")
    item = plan()
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[item])
        posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200)

    vpn = Mock()
    vpn.clients.return_value = {item["gateway_cn"]: item["network"]["gateway_ip"]}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        await module.SessionWorker(client, Mock(), vpn).reconcile()
    assert len(posts) == 1 and posts[0][0].endswith("/waiting")
    assert posts[0][1]["gateway_connected"] is True
    assert posts[0][1]["technician_connected"] is False
    assert posts[0][1]["network"] == "waiting"


async def test_worker_reports_management_failure_without_exception_details(load_script):
    module = load_script("session-worker", "main")
    item = plan()
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[item])
        posts.append(json.loads(request.content))
        return httpx.Response(200)

    vpn, firewall = Mock(), Mock()
    vpn.clients.side_effect = RuntimeError("private exception details")
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RuntimeError):
            await module.SessionWorker(client, firewall, vpn).reconcile()
    firewall.apply.assert_not_called()
    assert posts[0]["code"] == "vpn_unavailable"
    assert posts[0]["technician_connected"] is None
    assert "private" not in json.dumps(posts)


async def test_gateway_stops_tunnel_and_reports_timeout_with_safe_code(load_script):
    agent = agent_fixture(load_script, "wifi")
    item = plan()
    agent.heartbeat.send.return_value = {
        "connect": True,
        "network": item["network"],
        "lease_seconds": 20,
    }
    agent.heartbeat.report = AsyncMock()
    agent.vpn.start.side_effect = TimeoutError("private OpenVPN output")
    with pytest.raises(TimeoutError) as exc:
        await agent.step()
    await agent.handle_failure(exc.value)
    agent.vpn.stop.assert_awaited_once()
    agent.heartbeat.report.assert_awaited_with(
        item["session_id"], "tunnel", "error", "tunnel_timeout"
    )


async def test_reporting_outage_does_not_skip_gateway_authorization_checks(load_script):
    agent = agent_fixture(load_script, "wifi")
    item = plan()
    agent.heartbeat.send.side_effect = [
        {"connect": True, "network": item["network"], "lease_seconds": 20},
        {"connect": False},
    ]
    agent.heartbeat.report = AsyncMock(side_effect=httpx.ConnectError("telemetry unavailable"))
    await agent.step()
    agent.vpn.stop.assert_awaited_once()
    agent.network.configure_session.assert_not_called()


async def test_worker_processes_healthy_sessions_before_failure_telemetry(load_script):
    module = load_script("session-worker", "main")
    broken, healthy = plan(), plan()
    healthy["network"].update(technician_ip="10.100.128.3", gateway_ip="10.100.0.3")
    order = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[broken, healthy])
        order.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200)

    fw, vpn = Mock(), Mock()
    fw.delete.side_effect = [RuntimeError("network failure"), None]
    vpn.clients.return_value = {}
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        await module.SessionWorker(client, fw, vpn).reconcile()
    assert order == ["waiting", "diagnostics"]


async def test_worker_diagnostic_batch_has_one_overall_deadline(load_script):
    module = load_script("session-worker", "main")
    worker = module.SessionWorker(None, None, None)
    cancelled = []

    async def slow(*args):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)

    worker.report_failure = slow
    async with asyncio.timeout(3):
        await worker.report_all([None] * 20, "network_failed")
    assert len(cancelled) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"network": "ready", "technician_connected": False},
        {"network": "ready", "code": "firewall_failed"},
        {"network": "error", "code": "none"},
        {"gateway_connected": "false"},
    ],
)
def test_contradictory_worker_evidence_is_rejected(changes):
    from datetime import datetime, timezone
    from pydantic import ValidationError
    from api.schemas.diagnostics import WorkerObservation

    with pytest.raises(ValidationError):
        WorkerObservation.model_validate(
            {
                "observed_at": datetime.now(timezone.utc),
                "gateway_connected": True,
                "technician_connected": True,
                "network": "ready",
                "code": "none",
                **changes,
            }
        )


def test_contradictory_gateway_evidence_is_rejected():
    from datetime import datetime, timezone
    from pydantic import ValidationError
    from api.schemas.diagnostics import GatewayObservation

    for changes in ({"state": "ready"}, {"component": "network"}, {"code": "none"}):
        with pytest.raises(ValidationError):
            GatewayObservation.model_validate(
                {
                    "observed_at": datetime.now(timezone.utc),
                    "component": "tunnel",
                    "state": "error",
                    "code": "tunnel_timeout",
                    **changes,
                }
            )
