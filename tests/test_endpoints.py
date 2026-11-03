"""Integration test: verify all expected endpoints are in the OpenAPI schema."""

import pytest
from httpx import AsyncClient, ASGITransport


EXPECTED_PATHS = {
    "/health",
    "/",
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/me",
    "/api/v1/admin/users",
    "/api/v1/admin/users/{user_id}",
    "/api/v1/admin/sessions",
    "/api/v1/admin/sessions/{session_id}",
    "/api/v1/admin/gateways",
    "/api/v1/admin/gateways/{gateway_id}",
    "/api/v1/admin/gateways/{gateway_id}/devices",
    "/api/v1/admin/gateways/{gateway_id}/devices/{device_id}",
    "/api/v1/admin/audit",
    "/api/v1/admin/stats",
    "/api/v1/sessions/",
    "/api/v1/sessions/{session_id}",
    "/api/v1/sessions/expire-stale",
    "/api/v1/internal/sessions/expire-stale",
    "/api/v1/internal/sessions/namespace-states",
    "/api/v1/gateways/",
    "/api/v1/gateways/register",
    "/api/v1/devices/",
    "/api/v1/devices/gateway/{gateway_id}",
}


@pytest.mark.asyncio
async def test_openapi_contains_all_endpoints():
    """Every expected path must appear in the OpenAPI schema."""
    from api.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/openapi.json")

    assert resp.status_code == 200
    schema = resp.json()
    paths = set(schema.get("paths", {}).keys())

    missing = EXPECTED_PATHS - paths
    assert not missing, f"Missing endpoints in OpenAPI: {missing}"

    # Also verify we have a reasonable number of total paths
    assert len(paths) >= 22, f"Expected at least 22 paths, got {len(paths)}"


@pytest.mark.asyncio
async def test_admin_endpoints_exist():
    """Administrative contract includes users, groups and gateway assignments."""
    from api.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/openapi.json")

    schema = resp.json()
    admin_ops = 0
    for path, methods in schema.get("paths", {}).items():
        if path.startswith("/api/v1/admin"):
            admin_ops += len([m for m in methods if m in ("get", "post", "put", "delete")])

    assert admin_ops >= 23
    assert "/api/v1/admin/groups/{group_id}" in schema["paths"]
    assert "/api/v1/admin/gateways/{gateway_id}/group" in schema["paths"]
