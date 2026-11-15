"""Worker HTTP contracts and cleanup behavior without touching host networking."""

import importlib.util
from pathlib import Path

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
