"""Portal VPN discovery through real authentication, scoped queries and PostgreSQL."""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.models.provisioning import GatewayCredential, VPNCertificate
from api.config import settings
from api.models.audit_log import AuditLog
from api.models.gateway import Gateway
from api.models.session import Session
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_web_portal_postgres import browser as browser_fixture, login

access = access_fixture
encoded_password = password_fixture
browser = browser_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


async def test_gateway_label_is_scoped_audited_and_survives_machine_registration(access, browser):
    await login(browser, "admin_a")
    gateway = access.gateways["A"]
    path = f"/api/v1/admin/gateways/{gateway.id}"
    initial = (await browser.get(path)).json()
    result = await browser.patch(
        path,
        json={"display_name": "  Pressa Nord  "},
        headers={"If-Match": f'"{initial["revision"]}"'},
    )
    assert result.status_code == 200, result.text
    assert result.json()["display_name"] == "Pressa Nord"
    assert result.json()["revision"] != initial["revision"]
    assert (
        await browser.patch(
            path,
            json={"display_name": "stale"},
            headers={"If-Match": f'"{initial["revision"]}"'},
        )
    ).status_code == 412
    registered = await access.client.post(
        "/api/v1/gateways/register",
        headers={"X-Gateway-Key": access.keys[str(gateway.id)]},
        json={
            "gateway_id": str(gateway.id),
            "activation_mode": "wifi",
            "phone_number": gateway.phone_number,
            "mac_address": gateway.mac_address,
        },
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["display_name"] == "Pressa Nord"
    visible = (await access.call("GET", "/gateways/", "tech_a")).json()
    assert visible[0]["display_name"] == "Pressa Nord"
    async with access.db() as db:
        audit = await db.scalar(select(AuditLog).where(AuditLog.event_type == "gateway_updated"))
        assert audit.group_id == gateway.group_id and audit.user_id == access.users["admin_a"].id


async def test_gateway_label_requires_capability_scope_csrf_and_revision(access, browser):
    gateway = access.gateways["A"]
    path = f"/api/v1/admin/gateways/{gateway.id}"
    await login(browser, "admin_a")
    assert (await browser.patch(path, json={"display_name": "missing revision"})).status_code == 428
    before = (await browser.get(path)).json()
    assert (
        await browser.patch(
            path,
            json={"display_name": "forged"},
            headers={"If-Match": f'"{before["revision"]}"', "X-CSRF-Token": ""},
        )
    ).status_code == 403
    assert (
        await access.call(
            "PATCH", path.removeprefix("/api/v1"), "tech_a", json={"display_name": "forged"}
        )
    ).status_code == 403
    assert (
        await access.call(
            "PATCH", path.removeprefix("/api/v1"), "admin_b", json={"display_name": "forged"}
        )
    ).status_code == 404
    async with access.db() as db:
        assert (await db.get(Gateway, gateway.id)).display_name is None


@pytest.mark.parametrize("value", [None, "", "   "])
async def test_gateway_label_can_be_cleared_without_changing_live_session(access, value):
    gateway = access.gateways["A"]
    created = await access.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(gateway.id)}
    )
    assert created.status_code == 200, created.text
    path = f"/admin/gateways/{gateway.id}"
    assert (
        await access.call("PATCH", path, json={"display_name": "Named gateway"})
    ).status_code == 200
    result = await access.call("PATCH", path, json={"display_name": value})
    assert result.status_code == 200 and result.json()["display_name"] is None
    async with access.db() as db:
        session = await db.scalar(select(Session))
        assert session.status == "REQUESTED" and session.cleaned_at is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"display_name": "x" * 101},
        {"display_name": "Name", "group_id": None},
        {"display_name": "Name", "vpn_subnet": "192.168.1.0/24"},
    ],
)
async def test_gateway_label_rejects_invalid_or_unrelated_fields(access, body):
    result = await access.call("PATCH", f'/admin/gateways/{access.gateways["A"].id}', json=body)
    assert result.status_code == 422


@pytest.mark.parametrize("path", ["/gateways/", "/admin/gateways"])
async def test_gateway_search_filters_and_counts_keep_scope_and_literal_search(access, path):
    async with access.db() as db:
        a = await db.get(Gateway, access.gateways["A"].id)
        b = await db.get(Gateway, access.gateways["B"].id)
        a.display_name = "Pressa 100%_Nord"
        b.display_name = "Riservato Sud"
        b.activation_mode = "sms"
        await db.commit()
    matched = await access.call("GET", path, "admin_a", params={"q": "100%_nord", "limit": 1})
    assert matched.status_code == 200, matched.text
    assert matched.headers["x-total-count"] == "1"
    assert matched.json()[0]["id"] == str(access.gateways["A"].id)
    literal = await access.call("GET", path, params={"q": "%_"})
    assert literal.headers["x-total-count"] == "1"
    assert literal.json()[0]["id"] == str(access.gateways["A"].id)
    for params in (
        {"q": "100%Xnord"},
        {"q": "Riservato Sud"},
        {"q": str(access.gateways["B"].id)},
        {"group_id": str(access.groups["B"].id)},
        {"activation_mode": "sms"},
        {"status_filter": "INACTIVE"},
        {"availability": "busy"},
    ):
        result = await access.call("GET", path, "admin_a", params=params)
        assert result.json() == [] and result.headers["x-total-count"] == "0", result.text
    filtered = await access.call(
        "GET",
        path,
        "admin_a",
        params={
            "q": str(access.gateways["A"].id),
            "activation_mode": "wifi",
            "status_filter": "ACTIVE",
            "availability": "available",
            "offset": 1,
        },
    )
    assert filtered.json() == [] and filtered.headers["x-total-count"] == "1"
    pages = [
        await access.call("GET", path, params={"limit": 1, "offset": offset}) for offset in range(3)
    ]
    assert all(result.headers["x-total-count"] == "3" for result in pages)
    assert len({result.json()[0]["id"] for result in pages}) == 3


async def test_gateway_availability_does_not_claim_online_and_checks_start_permission(access):
    async with access.db() as db:
        gateway = await db.get(Gateway, access.gateways["A"].id)
        gateway.activation_mode = "sms"
        gateway.status = "INACTIVE"
        gateway.last_seen = utc_now() - timedelta(days=20)
        await db.commit()
    path = f'/gateways/{access.gateways["A"].id}'
    result = await access.call("GET", path, "tech_a")
    assert result.status_code == 200, result.text
    assert result.json()["availability"] == "available" and result.json()["can_start"]
    assert result.json()["status"] == "INACTIVE"
    observer = await access.call("GET", path, "observer")
    assert observer.json()["availability"] == "available"
    assert not observer.json()["can_start"]
    assert observer.json()["start_block_reason"] == "permission_required"
    assert (await access.call("GET", path, "tech_b")).status_code == 404
    assert (await access.call("GET", f"/gateways/{uuid4()}", "tech_b")).status_code == 404
    unassigned = f'/gateways/{access.gateways["unassigned"].id}'
    assert (await access.call("GET", unassigned, "tech_a")).status_code == 404
    assert (await access.call("GET", unassigned)).json()["availability"] == "unconfigured"


@pytest.mark.parametrize("missing", ["credentials", "certificate"])
async def test_gateway_availability_reports_incomplete_provisioning(access, monkeypatch, missing):
    if missing == "credentials":
        async with access.db() as db:
            for credential in (await db.scalars(select(GatewayCredential))).all():
                credential.active = False
            await db.commit()
    else:
        async with access.db() as db:
            certificate = await db.scalar(
                select(VPNCertificate).where(
                    VPNCertificate.peer_id == access.gateways["A"].id,
                    VPNCertificate.kind == "gateway",
                )
            )
            certificate.state = "revoked"
            await db.commit()
    result = await access.call(
        "GET", "/gateways/", "tech_a", params={"availability": "unconfigured"}
    )
    assert result.headers["x-total-count"] == "1"
    assert result.json()[0]["start_block_reason"] == "gateway_unconfigured"
    assert not result.json()[0]["can_start"]


async def start_session(access, actor, group):
    created = await access.call(
        "POST", "/sessions/", actor, json={"gateway_id": str(access.gateways[group].id)}
    )
    assert created.status_code == 200, created.text
    return created.json()["session_id"]


async def cleaned(access, identifier):
    result = await access.client.post(
        f"/api/v1/internal/sessions/{identifier}/cleaned", headers=access.worker
    )
    assert result.status_code == 200, result.text


async def test_availability_tracks_reservations_and_cleanup_without_disclosing_owners(
    access, browser
):
    await login(browser, "shared")
    path_a = f'/gateways/{access.gateways["A"].id}'
    path_b = f'/gateways/{access.gateways["B"].id}'
    assert (await browser.get("/api/v1" + path_a)).json()["can_start"]
    identifier = await start_session(access, "shared", "A")
    for actor in ("tech_a", "admin_a"):
        busy = await access.call("GET", path_a, actor)
        assert busy.json()["availability"] == "busy"
        assert busy.json()["start_block_reason"] == "gateway_busy"
        assert str(access.users["shared"].id) not in busy.text
        assert (busy.json()["current_session"] is not None) == (actor == "admin_a")
        if actor == "tech_a":
            assert identifier not in busy.text
        catalog = await access.call("GET", "/gateways/", actor)
        assert identifier not in catalog.text
        assert "technician_id" not in busy.json() and "session_id" not in busy.json()
    other = (await browser.get("/api/v1" + path_b)).json()
    assert other["availability"] == "available" and not other["can_start"]
    assert other["start_block_reason"] == "technician_busy"
    # A previous "available" read never bypasses allocation-time checks.
    raced = await access.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(access.gateways["A"].id)}
    )
    assert raced.status_code == 409
    assert (await access.call("DELETE", f"/sessions/{identifier}", "shared")).status_code == 200
    pending = await access.call(
        "GET", "/gateways/", "shared", params={"availability": "cleanup_pending"}
    )
    assert pending.headers["x-total-count"] == "1"
    assert pending.json()[0]["start_block_reason"] == "gateway_cleanup_pending"
    assert (await access.call("GET", path_b, "shared")).json()[
        "start_block_reason"
    ] == "technician_cleanup_pending"
    await cleaned(access, identifier)
    assert (await access.call("GET", path_a, "shared")).json()["can_start"]
    assert (await access.call("GET", path_b, "shared")).json()["can_start"]


async def test_own_cleanup_in_lost_group_blocks_start_without_exposing_session(access):
    identifier = await start_session(access, "shared", "A")
    result = await access.call(
        "PUT",
        f'/admin/users/{access.users["shared"].id}',
        json={"group_ids": [str(access.groups["B"].id)]},
    )
    assert result.status_code == 200
    await access.login("shared")
    own = await access.call("GET", "/sessions/", "shared", params={"reserved": True})
    assert own.json() == [] and own.headers["x-total-count"] == "0"
    gateways = await access.call("GET", "/gateways/", "shared")
    assert gateways.headers["x-total-count"] == "1"
    assert gateways.json()[0]["start_block_reason"] == "technician_cleanup_pending"
    assert identifier not in gateways.text


async def test_session_search_filters_counts_and_cleanup_keep_ownership(access):
    a_id = await start_session(access, "shared", "A")
    b_id = await start_session(access, "tech_b", "B")
    for actor, path, expected in (
        ("shared", "/sessions/", [a_id]),
        ("root", "/sessions/", []),
        ("admin_a", "/admin/sessions", [a_id]),
        ("admin_b", "/admin/sessions", [b_id]),
    ):
        response = await access.call("GET", path, actor, params={"reserved": True})
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()] == expected
        assert response.headers["x-total-count"] == str(len(expected))
    assert (await access.call("GET", "/admin/sessions", "shared")).status_code == 403
    for path, actor in (("/sessions/", "shared"), ("/admin/sessions", "admin_a")):
        for params in (
            {"q": b_id},
            {"gateway_id": str(access.gateways["B"].id)},
            {"group_id": str(access.groups["B"].id)},
            {"status_filter": "CLOSED"},
            {"reserved": False},
            {"cleanup_pending": True},
            {"activation_mode": "sms"},
        ):
            response = await access.call("GET", path, actor, params=params)
            assert response.json() == [] and response.headers["x-total-count"] == "0"
        response = await access.call("GET", path, actor, params={"q": a_id, "offset": 1})
        assert response.json() == [] and response.headers["x-total-count"] == "1"
        response = await access.call(
            "GET",
            path,
            actor,
            params={
                "gateway_id": str(access.gateways["A"].id),
                "group_id": str(access.groups["A"].id),
                "status_filter": "REQUESTED",
                "activation_mode": "wifi",
                "reserved": True,
                "cleanup_pending": False,
            },
        )
        assert response.headers["x-total-count"] == "1"
        assert response.json()[0]["id"] == a_id
    response = await access.call(
        "GET",
        "/admin/sessions",
        "admin_a",
        params={"technician_id": str(access.users["tech_b"].id)},
    )
    assert response.json() == [] and response.headers["x-total-count"] == "0"
    all_rows = await access.call("GET", "/admin/sessions", params={"limit": 1})
    assert len(all_rows.json()) == 1 and all_rows.headers["x-total-count"] == "2"
    await access.call("DELETE", f"/sessions/{a_id}", "shared")
    pending = await access.call(
        "GET",
        "/sessions/",
        "shared",
        params={"status_filter": "CLOSED", "reserved": True, "cleanup_pending": True},
    )
    assert pending.headers["x-total-count"] == "1"
    assert pending.json()[0]["cleanup_pending"] and pending.json()[0]["cleaned_at"] is None
    detail = await access.call("GET", f"/admin/sessions/{a_id}", "admin_a")
    assert detail.json()["cleaned_at"] is None
    await cleaned(access, a_id)
    history = await access.call("GET", "/sessions/", "shared", params={"reserved": False})
    assert history.headers["x-total-count"] == "1"
    assert not history.json()[0]["cleanup_pending"] and history.json()[0]["cleaned_at"]


async def test_historical_session_search_cannot_reveal_moved_gateway_metadata(access):
    a_id = await start_session(access, "tech_a", "A")
    await access.call(
        "PATCH",
        f'/admin/gateways/{access.gateways["A"].id}',
        json={"display_name": "Visible North"},
    )
    visible = await access.call("GET", "/sessions/", "tech_a", params={"q": "Visible North"})
    assert visible.headers["x-total-count"] == "1"
    await access.call("DELETE", f"/sessions/{a_id}", "tech_a")
    await cleaned(access, a_id)
    assert (
        await access.call(
            "PUT",
            f'/admin/gateways/{access.gateways["A"].id}/group',
            json={"group_id": str(access.groups["B"].id)},
        )
    ).status_code == 200
    await access.call(
        "PATCH", f'/admin/gateways/{access.gateways["A"].id}', json={"display_name": "Secret South"}
    )
    for path, actor in (("/sessions/", "tech_a"), ("/admin/sessions", "admin_a")):
        response = await access.call("GET", path, actor, params={"q": "Secret South"})
        assert response.json() == [] and response.headers["x-total-count"] == "0"
        history = await access.call("GET", path, actor, params={"q": a_id})
        assert history.headers["x-total-count"] == "1"
        row = history.json()[0]
        assert row["gateway_display_name"] is None and row["gateway_mac_address"] is None
    assert (await access.call("GET", "/admin/sessions", "admin_b")).json() == []
    global_rows = await access.call("GET", "/admin/sessions", params={"q": "Secret South"})
    assert global_rows.headers["x-total-count"] == "1"


async def test_terminal_session_without_cleanup_remains_reserved(access):
    async with access.db() as db:
        db.add(
            Session(
                id=uuid4(),
                technician_id=access.users["tech_a"].id,
                gateway_id=access.gateways["A"].id,
                group_id=access.groups["A"].id,
                status="CLOSED",
                activation_mode="wifi",
                cleaned_at=None,
            )
        )
        await db.commit()
    result = await access.call(
        "GET", "/sessions/", "tech_a", params={"reserved": True, "cleanup_pending": True}
    )
    assert result.headers["x-total-count"] == "1"
    assert result.json()[0]["cleanup_pending"]


@pytest.mark.parametrize("path", ["/gateways/", "/admin/gateways", "/sessions/", "/admin/sessions"])
async def test_catalog_rejects_invalid_pagination_and_filters(access, path):
    for params in (
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
        {"q": "x" * 101},
        {"group_id": "invalid"},
        {"activation_mode": "invalid"},
        {"status_filter": "invalid"},
    ):
        result = await access.call("GET", path, params=params)
        assert result.status_code == 422, (params, result.text)


async def test_bearer_catalog_exposes_total_count_to_an_allowed_browser_origin(access):
    result = await access.client.get(
        "/api/v1/gateways/",
        headers={**access.headers["tech_a"], "Origin": settings.CORS_ORIGINS[0]},
    )
    assert result.status_code == 200
    assert "X-Total-Count" in result.headers["access-control-expose-headers"]
    assert result.headers["x-total-count"] == "1"
