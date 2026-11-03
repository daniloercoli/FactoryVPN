"""Worker HTTP contracts and cleanup behavior without touching host networking."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest


@pytest.fixture
def worker(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "factoryvpn_worker", Path(__file__).parents[1] / "session-worker" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WORKER_API_KEY", "worker-test-key")
    return module


def mock_api(monkeypatch, worker, handler):
    client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kw: client(transport=transport, **kw))


async def test_cleanup_preserves_active_unknown_and_unrelated_namespaces(worker, monkeypatch):
    def respond(request):
        assert request.url.path == "/api/v1/internal/sessions/namespace-states"
        assert request.headers["X-Worker-Key"] == "worker-test-key"
        return httpx.Response(
            200,
            json={
                "protected": ["factoryvpn_aaaaaaaa"],
                "terminal": ["factoryvpn_aaaaaaaa", "factoryvpn_bbbbbbbb"],
            },
        )

    mock_api(monkeypatch, worker, respond)
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout=("factoryvpn_aaaaaaaa (id: 0)\nfactoryvpn_bbbbbbbb\nfactoryvpn_cccccccc\nother\n")
        )
    )
    monkeypatch.setattr(worker.subprocess, "run", run)
    await worker.cleanup_namespaces()
    commands = [call.args[0] for call in run.call_args_list]
    assert commands == [["ip", "netns", "list"], ["ip", "netns", "delete", "factoryvpn_bbbbbbbb"]]


@pytest.mark.parametrize(
    "failure", ["unauthorized", "server_error", "timeout", "malformed", "invalid_name", "empty"]
)
async def test_cleanup_never_deletes_without_valid_confirmation(worker, monkeypatch, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("API timeout", request=request)
        if failure == "unauthorized":
            return httpx.Response(401)
        if failure == "server_error":
            return httpx.Response(500)
        if failure == "malformed":
            return httpx.Response(200, json={"terminal": ["factoryvpn_bbbbbbbb"]})
        if failure == "invalid_name":
            return httpx.Response(200, json={"protected": [], "terminal": ["other_namespace"]})
        return httpx.Response(200, json={"protected": [], "terminal": []})

    mock_api(monkeypatch, worker, respond)
    run = Mock()
    monkeypatch.setattr(worker.subprocess, "run", run)
    await worker.cleanup_namespaces()
    run.assert_not_called()


async def test_expiration_uses_authenticated_internal_endpoint(worker, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"expired": 2})

    mock_api(monkeypatch, worker, respond)
    await worker.expire_stale_sessions()
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/internal/sessions/expire-stale"
    assert requests[0].headers["X-Worker-Key"] == "worker-test-key"


async def test_expiration_logs_api_rejection(worker, monkeypatch, caplog):
    mock_api(monkeypatch, worker, lambda request: httpx.Response(401))
    await worker.expire_stale_sessions()
    assert "Failed to expire stale sessions" in caplog.text


async def test_worker_fails_at_startup_without_credentials(worker, monkeypatch):
    monkeypatch.setattr(worker, "WORKER_API_KEY", "")
    with pytest.raises(RuntimeError, match="WORKER_API_KEY"):
        await worker.main()
