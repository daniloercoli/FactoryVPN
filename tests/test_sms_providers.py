import base64
import json
from urllib.parse import parse_qs
import httpx
import pytest
from api.config import settings
from api.services.sms_service import send_sms, send_sms_detailed


def transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )


@pytest.mark.parametrize(
    "code,accepted",
    [(200, True), (201, True), (202, True), (302, False), (401, False), (503, False)],
)
async def test_generic_acceptance_contract(monkeypatch, code, accepted):
    monkeypatch.setattr(settings, "SMS_PROVIDER", "generic")
    monkeypatch.setattr(settings, "SMS_PROVIDER_URL", "https://sms.example.test/send")
    monkeypatch.setattr(settings, "SMS_PROVIDER_API_KEY", "test-key")

    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"to": "+393331234567", "message": "test-message"}
        return httpx.Response(code)

    transport(monkeypatch, handler)
    assert await send_sms("+393331234567", "test-message") is accepted


@pytest.mark.parametrize(
    "state,accepted", [("queued", True), ("failed", False), ("undelivered", False)]
)
async def test_twilio_uses_form_auth_and_checks_provider_state(monkeypatch, state, accepted):
    sid = "AC" + "1" * 32
    monkeypatch.setattr(settings, "SMS_PROVIDER", "twilio")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", sid)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(settings, "TWILIO_FROM", "+12025550123")

    def handler(request):
        assert str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        assert (
            request.headers["Authorization"]
            == "Basic " + base64.b64encode((sid + ":test-token").encode()).decode()
        )
        assert parse_qs(request.content.decode()) == {
            "To": ["+393331234567"],
            "From": ["+12025550123"],
            "Body": ["test-message"],
        }
        return httpx.Response(201, json={"sid": "SM" + "1" * 32, "status": state})

    transport(monkeypatch, handler)
    assert await send_sms("+393331234567", "test-message") is accepted


async def test_provider_timeout_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(settings, "SMS_PROVIDER", "generic")
    monkeypatch.setattr(settings, "SMS_PROVIDER_URL", "https://sms.example.test/send")
    monkeypatch.setattr(settings, "SMS_PROVIDER_API_KEY", "test-key")

    def handler(request):
        raise httpx.ReadTimeout("timeout", request=request)

    transport(monkeypatch, handler)
    assert await send_sms("+393331234567", "test-message") is False


@pytest.mark.parametrize(
    "code,outcome", [(202, "accepted"), (401, "rejected"), (503, "unconfirmed")]
)
async def test_diagnostic_sms_result_distinguishes_rejection_and_uncertainty(
    monkeypatch, code, outcome
):
    monkeypatch.setattr(settings, "SMS_PROVIDER", "generic")
    monkeypatch.setattr(settings, "SMS_PROVIDER_URL", "https://sms.example.test/send")
    monkeypatch.setattr(settings, "SMS_PROVIDER_API_KEY", "test-key")
    transport(monkeypatch, lambda request: httpx.Response(code))
    result = await send_sms_detailed("+393331234567", "private activation")
    assert result.outcome == outcome
    assert "private" not in repr(result) and "+39333" not in repr(result)


async def test_missing_sms_configuration_is_explicit(monkeypatch):
    monkeypatch.setattr(settings, "SMS_PROVIDER", "generic")
    monkeypatch.setattr(settings, "SMS_PROVIDER_API_KEY", "")
    assert (await send_sms_detailed("+393331234567", "activation")).outcome == "not_configured"
