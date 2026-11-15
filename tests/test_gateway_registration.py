"""The agent sends its own identity and its scoped credential to the API."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def registrar_class():
    spec = importlib.util.spec_from_file_location(
        "gateway_registration", Path(__file__).parents[1] / "gateway-agent" / "registration.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GatewayRegistrar


async def test_registration_sends_gateway_credential_and_identity(monkeypatch):
    registrar = registrar_class()("https://example.test/", "gateway-test-key")

    def respond(request):
        assert request.url.path == "/api/v1/gateways/register"
        assert request.headers["X-Gateway-Key"] == "gateway-test-key"
        assert json.loads(request.content)["gateway_id"] == "11111111-1111-4111-8111-111111111111"
        assert json.loads(request.content)["phone_number"] == "+393331234567"
        return httpx.Response(200, json={"id": "gateway-id"})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw)
    )
    result = await registrar.register(
        "11111111-1111-4111-8111-111111111111",
        "AA:BB:CC:DD:EE:FF",
        phone_number="+393331234567",
    )
    assert result == {"id": "gateway-id"}


def test_registration_requires_configured_credential():
    with pytest.raises(ValueError, match="GATEWAY_API_KEY"):
        registrar_class()("https://example.test", "")
