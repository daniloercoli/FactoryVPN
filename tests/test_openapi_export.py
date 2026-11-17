"""The downloadable client contract must match the API and actual responses."""

import json
from uuid import uuid4
from unittest.mock import Mock

from fastapi.encoders import jsonable_encoder
from httpx import ASGITransport, AsyncClient
import pytest

from api.main import app
from api.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from api.models.session import Session
from api.models.user import User
from api.routers.auth import get_current_user
from api.schemas.client import CurrentUserResponse, SessionDetailResponse
from api.time_utils import utc_now
from scripts.export_openapi import DEFAULT_OUTPUT, customer_schema, load_schema, render_schema


def test_download_matches_current_code_and_ignores_deployment_environment(monkeypatch):
    # These values would prevent API import if export loaded the deployment env.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", "invalid")
    monkeypatch.setenv("DATABASE_URL", "not-a-database-url")
    monkeypatch.setenv("APP_NAME", "Private deployment name")
    schema = customer_schema(load_schema())
    assert schema["info"]["title"] == "FactoryVPN Customer API"
    assert "Private deployment name" not in render_schema(schema)
    assert DEFAULT_OUTPUT.read_text(encoding="utf-8") == render_schema(schema)


def test_custom_label_is_json_data_and_keeps_protocol_identifiers_stable():
    label = 'Assistenza & "VPN" </script> $&'
    source = app.openapi()
    original_title = source["info"]["title"]
    schema = customer_schema(source, app_name=f"  {label}  ")
    rendered = render_schema(schema)
    assert json.loads(rendered)["info"]["title"] == f"{label} Customer API"
    assert f"Pass the returned {label} JWT" in schema["info"]["description"]
    for path, methods in schema["paths"].items():
        if path.startswith("/api/v1/backups"):
            assert all(
                f"{label} binary archive" in operation["description"]
                for operation in methods.values()
            )
    assert (
        "X-FactoryVPN-Portal: 1"
        in schema["paths"]["/api/v1/web/login"]["post"]["description"]
    )
    assert (
        schema["components"]["securitySchemes"]["PortalCookie"]["name"]
        == "__Host-factoryvpn_portal"
    )
    assert schema["externalDocs"]["url"] == "https://factoryvpn.ercoliconsulting.eu/developers.html"
    assert schema["servers"][0]["url"] == "https://vpn-api.example.com"
    assert source["info"]["title"] == original_title


def test_customer_contract_has_typed_responses_and_no_machine_surface():
    schema = customer_schema(app.openapi())
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/auth/login/verify" in schema["paths"]
    assert "/api/v1/auth/login/resend" in schema["paths"]
    assert "/api/v1/admin/users" in schema["paths"]
    assert "/api/v1/gateways/register" not in schema["paths"]
    assert "/api/v1/gateways/heartbeat" not in schema["paths"]
    assert not any(path.startswith("/api/v1/internal/") for path in schema["paths"])
    assert not any(
        path.startswith(("/api/v1/factory/", "/api/v1/gateway-enrollment/"))
        for path in schema["paths"]
    )
    for path, methods in schema["paths"].items():
        if path.startswith("/api/v1/admin/enrollments"):
            assert all(operation["x-required-role"] == "admin" for operation in methods.values())
    assert set(schema["components"]["securitySchemes"]) == {
        "HTTPBearer",
        "PortalCookie",
        "VerificationCookie",
    }
    assert "/api/v1/web/login" in schema["paths"]
    assert "GatewayRegisterRequest" not in schema["components"]["schemas"]
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            for status, response in operation["responses"].items():
                if status.startswith("2") and status != "204":
                    content_type = (
                        "text/csv"
                        if path.endswith(".csv")
                        else (
                            "application/octet-stream"
                            if path.endswith("/download")
                            else "application/json"
                        )
                    )
                    assert response["content"][content_type]["schema"], (method, path)

    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = schema
                for part in value["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)
    assert "/api/v1/internal/sessions/work" in app.openapi()["paths"]


@pytest.mark.parametrize("suffix", ["verify", "resend"])
def test_email_challenge_contract_does_not_require_an_authenticated_session(suffix, monkeypatch):
    from api.config import settings
    from api.services.email_security import binding_cookie

    monkeypatch.setattr(settings, "PORTAL_PUBLIC_URL", "https://vpn.example.com")
    schema = customer_schema(app.openapi())
    operation = schema["paths"][f"/api/v1/web/login/{suffix}"]["post"]
    assert operation["security"] == [{"VerificationCookie": []}]
    cookie = schema["components"]["securitySchemes"]["VerificationCookie"]
    assert cookie["in"] == "cookie"
    assert cookie["name"] == binding_cookie()
    assert "X-FactoryVPN-Portal: 1" in operation["description"]
    assert "No authenticated portal session or CSRF token is required" in operation["description"]
    assert "bearer" not in operation["responses"]["401"]["description"].lower()
    for surface in ("auth", "web"):
        responses = schema["paths"][f"/api/v1/{surface}/login/{suffix}"]["post"]["responses"]
        assert {"400", "429"} <= responses.keys()
        if suffix == "resend":
            assert "503" in responses


def test_browser_contract_distinguishes_initial_login_from_conditional_sessions():
    paths = customer_schema(app.openapi())["paths"]
    assert paths["/api/v1/web/login"]["post"]["security"] == []
    for path in ("/api/v1/web/sso/start", "/api/v1/web/logout"):
        assert paths[path]["post"]["security"] == [{}, {"PortalCookie": []}]
    assert "reauthenticate=true" in paths["/api/v1/web/sso/start"]["post"]["description"]
    assert "even expired or revoked" in paths["/api/v1/web/logout"]["post"]["description"]
    for path, method in (("session", "get"), ("password", "post"), ("logout-all", "post")):
        assert paths[f"/api/v1/web/{path}"][method]["security"] == [{"PortalCookie": []}]


def test_session_contract_excludes_internal_diagnostic_storage():
    session = Session(
        id=uuid4(),
        technician_id=uuid4(),
        gateway_id=uuid4(),
        status="REQUESTED",
        activation_mode="wifi",
        requested_at=utc_now(),
        expires_at=utc_now(),
    )
    # DB-loaded rows have all columns populated, including SQL NULL values.
    for column in Session.__table__.columns:
        setattr(session, column.key, getattr(session, column.key))
    payload = jsonable_encoder(session)
    parsed = SessionDetailResponse.model_validate(payload)
    assert set(payload) - {"diagnostic_state", "diagnostics_started_at"} == set(
        SessionDetailResponse.model_fields
    )
    assert not {"diagnostic_state", "diagnostics_started_at"} & parsed.model_dump().keys()
    assert parsed.cleaned_at is None
    assert parsed.gateway_ready_at is None


@pytest.mark.asyncio
async def test_current_user_contract_matches_http_payload():
    user = User(
        id=uuid4(),
        username="technician",
        auth_mode="local",
        email=None,
        full_name=None,
        is_admin=False,
        permissions=["connect"],
        last_login=None,
    )
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: user
    db = Mock(spec=AsyncSession)
    db.scalars.return_value = Mock()
    db.scalars.return_value.all.return_value = []

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v1/auth/me")
        assert response.status_code == 200
        parsed = CurrentUserResponse.model_validate(response.json())
        assert str(parsed.id) == str(user.id)
        assert parsed.is_admin is False
        assert set(response.json()) == set(CurrentUserResponse.model_fields)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_admin_roles_and_gateway_selector_are_documented():
    schema = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    assert schema["paths"]["/api/v1/admin/users"]["post"]["x-required-permission"] == "users.manage"
    assert (
        schema["paths"]["/api/v1/admin/audit.csv"]["get"]["x-required-permission"] == "audit.read"
    )
    assert schema["paths"]["/api/v1/auth/register"]["post"]["x-required-role"] == "admin"
    assert schema["paths"]["/api/v1/telemetry/server"]["get"]["x-required-role"] == "admin"
    assert (
        schema["paths"]["/api/v1/telemetry/gateways"]["get"]["x-required-permission"]
        == "telemetry.read"
    )
    assert "/api/v1/gateways/telemetry" not in schema["paths"]
    selector = schema["components"]["schemas"]["SessionCreateRequest"]
    assert len(selector["oneOf"]) == 2
    status = schema["components"]["schemas"]["SessionDetailResponse"]["properties"]["status"]
    assert set(status["enum"]) == {"REQUESTED", "ACTIVE", "CLOSED", "EXPIRED", "FAILED"}


def test_vpn_catalog_contract_exposes_pagination_availability_and_safe_label_edit():
    schema = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    for path in ("/gateways/", "/admin/gateways", "/sessions/", "/admin/sessions"):
        operation = schema["paths"]["/api/v1" + path]["get"]
        parameters = {param["name"] for param in operation["parameters"]}
        assert {
            "q",
            "group_id",
            "status_filter",
            "activation_mode",
            "limit",
            "offset",
        } <= parameters
        response = operation["responses"]["200"]
        assert response["headers"]["X-Total-Count"]["schema"]["type"] == "integer"
        # Keep existing array bodies compatible with current clients.
        assert response["content"]["application/json"]["schema"]["type"] == "array"
    detail = schema["paths"]["/api/v1/gateways/{gateway_id}"]["get"]
    assert {"HTTPBearer": []} in detail["security"]
    assert {"PortalCookie": []} in detail["security"]
    properties = schema["components"]["schemas"]["GatewayCatalogResponse"]["properties"]
    assert set(properties["availability"]["enum"]) == {
        "available",
        "busy",
        "cleanup_pending",
        "unconfigured",
    }
    assert {"can_start", "start_block_reason"} <= properties.keys()
    assert not {"session_id", "technician_id"} & properties.keys()
    patch = schema["paths"]["/api/v1/admin/gateways/{gateway_id}"]["patch"]
    assert patch["x-required-permission"] == "gateways.manage"
    assert "if-match" in {param["name"].lower() for param in patch["parameters"]}
    update = schema["components"]["schemas"]["GatewayUpdateRequest"]
    assert update["additionalProperties"] is False
    assert not update.get("required")
    assert update["minProperties"] == 1
    assert set(update["properties"]) == {
        "display_name",
        "serial_number",
        "imei",
        "iccid",
        "activation_mode",
        "phone_number",
        "mac_address",
    }


def test_component_and_log_contracts_do_not_export_machine_routes():
    schema = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    paths = schema["paths"]
    assert "/api/v1/gateways/system-info" not in paths
    assert "/api/v1/gateways/technical-logs" not in paths
    for suffix in ("technical-logs", "technical-logs.csv"):
        assert (
            paths[f"/api/v1/gateways/{{gateway_id}}/{suffix}"]["get"]["x-required-permission"]
            == "logs.read"
        )
    assert "SystemReport" not in schema["components"]["schemas"]
