"""Diagnostics and exports must never widen historical session authorization."""

import csv
import io
import os
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from api.models.session import Session
from api.models.session_event import SessionEvent
from api.models.audit_log import AuditLog
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


async def open_session(access, actor="tech_a", group="A"):
    result = await access.call(
        "POST", "/sessions/", actor, json={"gateway_id": str(access.gateways[group].id)}
    )
    assert result.status_code == 200, result.text
    return result.json()["session_id"]


async def test_history_scope_counts_csv_and_ownership_are_consistent(access):
    a, b = await open_session(access), await open_session(access, "tech_b", "B")
    detail = await access.call("GET", f"/sessions/{a}", "tech_a")
    assert detail.status_code == 200
    assert not {"diagnostic_state", "diagnostics_started_at"} & detail.json().keys()
    for actor, expected in (
        ("tech_a", [a]),
        ("admin_a", [a]),
        ("tech_b", [b]),
        ("observer", []),
        ("root", [a, b]),
    ):
        result = await access.call("GET", "/session-events", actor)
        assert result.status_code == 200
        assert result.headers["X-Total-Count"] == str(len(expected))
        assert {r["session_id"] for r in result.json()} == set(expected)
        exported = await access.call("GET", "/session-events.csv", actor)
        rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
        assert {r["session_id"] for r in rows} == set(expected)
    for actor in ("tech_b", "observer"):
        assert (await access.call("GET", f"/sessions/{a}/diagnostics", actor)).status_code == 404
        hidden = await access.call("GET", "/session-events", actor, params={"session_id": a})
        assert hidden.json() == [] and hidden.headers["X-Total-Count"] == "0"
    assert (await access.call("GET", "/admin/audit.csv", "tech_a")).status_code == 403


async def test_logout_records_one_terminal_event_and_cleanup_is_distinct(access, browser):
    await login(browser, "tech_a")
    result = await browser.post(
        "/api/v1/sessions/", json={"gateway_id": str(access.gateways["A"].id)}
    )
    identifier = result.json()["session_id"]
    assert (await browser.post("/api/v1/web/logout")).status_code == 200
    await access.call("DELETE", f"/sessions/{identifier}", "admin_a")
    history = (
        await access.call("GET", "/session-events", "admin_a", params={"session_id": identifier})
    ).json()
    assert [r["event_type"] for r in history] == ["session_closed", "session_requested"]
    assert history[0]["details"]["reason"] == "portal_logout"
    assert history[0]["actor_id"] == str(access.users["tech_a"].id)
    for _ in range(2):
        result = await access.client.post(
            f"/api/v1/internal/sessions/{identifier}/cleaned", headers=access.worker
        )
        assert result.status_code == 200
    history = (
        await access.call("GET", "/session-events", "admin_a", params={"session_id": identifier})
    ).json()
    assert [r["event_type"] for r in history].count("resources_released") == 1
    diagnostic = (await access.call("GET", f"/sessions/{identifier}/diagnostics", "admin_a")).json()
    assert diagnostic["history_complete"]
    assert diagnostic["steps"][-1]["status"] == "ok"


async def test_expiry_and_repeated_status_reads_do_not_duplicate_events(access):
    identifier = await open_session(access)
    async with access.db() as db:
        (await db.get(Session, UUID(identifier))).expires_at = utc_now() - timedelta(seconds=1)
        await db.commit()
    for _ in range(2):
        assert (await access.call("GET", f"/sessions/{identifier}/diagnostics", "tech_a")).json()[
            "status"
        ] == "EXPIRED"
    result = await access.call(
        "GET", "/session-events", "tech_a", params={"event_type": "session_expired"}
    )
    assert len(result.json()) == 1


async def test_old_sessions_are_explicitly_partial_and_not_backfilled(access):
    async with access.db() as db:
        row = Session(
            id=uuid4(),
            group_id=access.groups["A"].id,
            technician_id=access.users["tech_a"].id,
            gateway_id=access.gateways["A"].id,
            status="CLOSED",
        )
        db.add(row)
        await db.commit()
    result = await access.call("GET", f"/sessions/{row.id}/diagnostics", "tech_a")
    assert not result.json()["history_complete"]
    history = await access.call(
        "GET", "/session-events", "tech_a", params={"session_id": str(row.id)}
    )
    assert history.json() == []


async def test_audit_csv_uses_scope_filters_and_neutralizes_formulas(access):
    async with access.db() as db:
        db.add(AuditLog(event_type='=HYPERLINK("unsafe")', group_id=access.groups["A"].id))
        db.add(AuditLog(event_type="private", group_id=access.groups["B"].id))
        await db.commit()
    response = await access.call("GET", "/admin/audit.csv", "admin_a")
    assert response.status_code == 200
    assert "private" not in response.text
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert any(r["event_type"].startswith("'=HYPERLINK") for r in rows)
    assert "attachment;" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    for path in ("/admin/audit", "/admin/audit.csv", "/session-events", "/session-events.csv"):
        invalid = await access.call(
            "GET",
            path,
            params={
                "since": utc_now().isoformat(),
                "until": (utc_now() - timedelta(days=1)).isoformat(),
            },
        )
        assert invalid.status_code == 422


async def test_event_transaction_rolls_back_with_session_change(access):
    from api.services.session_events import terminal_event

    identifier = await open_session(access)
    async with access.db() as db:
        session = await db.get(Session, UUID(identifier))
        session.status = "CLOSED"
        terminal_event(db, session, reason="manual_close")
        await db.flush()
        await db.rollback()
    async with access.db() as db:
        assert (await db.get(Session, UUID(identifier))).status == "REQUESTED"
        rows = (
            await db.scalars(
                select(SessionEvent).where(SessionEvent.session_id == UUID(identifier))
            )
        ).all()
        assert [r.event_type for r in rows] == ["session_requested"]


def worker_report(**changes):
    return {
        "observed_at": utc_now().isoformat(),
        "gateway_connected": True,
        "technician_connected": False,
        "network": "waiting",
        "code": "none",
        **changes,
    }


async def test_machine_observations_are_scoped_typed_and_do_not_authorize_vpn(access):
    identifier = await open_session(access)
    path = f"/api/v1/internal/sessions/{identifier}/diagnostics"
    assert (
        await access.client.post(path, json=worker_report(), headers=access.headers["root"])
    ).status_code == 401
    report = worker_report()
    assert (await access.client.post(path, json=report, headers=access.worker)).json()["recorded"]
    for _ in range(3):
        report = worker_report()
        assert (
            await access.client.post(path, json=report, headers=access.worker)
        ).status_code == 200
    old = worker_report(
        observed_at=(utc_now() - timedelta(seconds=5)).isoformat(),
        technician_connected=True,
        network="ready",
    )
    assert not (await access.client.post(path, json=old, headers=access.worker)).json()["recorded"]
    # Even a fresh "ready" diagnostic report cannot change lifecycle state.
    ready = worker_report(technician_connected=True, network="ready")
    assert (await access.client.post(path, json=ready, headers=access.worker)).status_code == 200
    async with access.db() as db:
        row = await db.get(Session, UUID(identifier))
        assert row.status == "REQUESTED" and row.network_ready_at is None
    for bad in (
        worker_report(code="exception with a secret"),
        worker_report(secret="must never be stored"),
        worker_report(observed_at=(utc_now() + timedelta(hours=1)).isoformat()),
    ):
        assert (await access.client.post(path, json=bad, headers=access.worker)).status_code == 422
    gateway_path = f"/api/v1/gateways/sessions/{identifier}/diagnostics"
    body = {
        "observed_at": utc_now().isoformat(),
        "component": "tunnel",
        "state": "error",
        "code": "tunnel_timeout",
    }
    foreign = {"X-Gateway-Key": access.keys[str(access.gateways["B"].id)]}
    assert (await access.client.post(gateway_path, json=body, headers=foreign)).status_code == 404
    own = {"X-Gateway-Key": access.keys[str(access.gateways["A"].id)]}
    assert (await access.client.post(gateway_path, json=body, headers=own)).status_code == 200
    events = (await access.call("GET", "/session-events", "tech_a")).json()
    assert [r["event_type"] for r in events].count("worker_changed") == 2
    assert [r["event_type"] for r in events].count("gateway_tunnel_changed") == 1


async def test_freshness_and_terminal_state_override_diagnostic_success(access):
    identifier = await open_session(access)
    path = f"/api/v1/internal/sessions/{identifier}/diagnostics"
    await access.client.post(path, json=worker_report(), headers=access.worker)
    diagnostic = (await access.call("GET", f"/sessions/{identifier}/diagnostics", "tech_a")).json()
    steps = {r["id"]: r for r in diagnostic["steps"]}
    assert steps["tunnel"]["status"] == "ok"
    assert steps["technician"]["status"] == "waiting"
    async with access.db() as db:
        row = await db.get(Session, UUID(identifier))
        row.diagnostic_state = {
            "worker": {
                **row.diagnostic_state["worker"],
                "observed_at": (utc_now() - timedelta(seconds=31)).isoformat(),
            }
        }
        await db.commit()
    diagnostic = (await access.call("GET", f"/sessions/{identifier}/diagnostics", "tech_a")).json()
    assert next(r for r in diagnostic["steps"] if r["id"] == "tunnel")["status"] == "unknown"
    await access.call("DELETE", f"/sessions/{identifier}", "tech_a")
    late = await access.client.post(
        path, json=worker_report(network="ready", technician_connected=True), headers=access.worker
    )
    assert late.status_code == 200 and not late.json()["recorded"]
    diagnostic = (await access.call("GET", f"/sessions/{identifier}/diagnostics", "tech_a")).json()
    assert diagnostic["status"] == "CLOSED"
    assert next(r for r in diagnostic["steps"] if r["id"] == "tunnel")["status"] == "unknown"


async def test_history_follows_session_scope_after_gateway_moves_and_access_revocation(access):
    identifier = await open_session(access, "shared")
    await access.call("DELETE", f"/sessions/{identifier}", "shared")
    await access.client.post(
        f"/api/v1/internal/sessions/{identifier}/cleaned", headers=access.worker
    )
    moved = await access.call(
        "PUT",
        f'/admin/gateways/{access.gateways["A"].id}/group',
        json={"group_id": str(access.groups["B"].id)},
    )
    assert moved.status_code == 200
    assert (await access.call("GET", "/session-events", "admin_b")).json() == []
    assert len((await access.call("GET", "/session-events", "admin_a")).json()) == 3
    await access.call(
        "PUT",
        f'/admin/users/{access.users["shared"].id}',
        json={"group_ids": [str(access.groups["B"].id)]},
    )
    await access.login("shared")
    assert (await access.call("GET", "/session-events", "shared")).json() == []
    exported = await access.call("GET", "/session-events.csv", "shared")
    assert identifier not in exported.text


async def test_export_refuses_oversize_results_instead_of_silently_truncating(access, monkeypatch):
    import api.services.csv_export as exporter
    import api.routers.session_diagnostics as router

    identifier = await open_session(access)
    await access.call("DELETE", f"/sessions/{identifier}", "tech_a")
    monkeypatch.setattr(exporter, "EXPORT_LIMIT", 1)
    monkeypatch.setattr(router, "EXPORT_LIMIT", 1)
    response = await access.call("GET", "/session-events.csv", "tech_a")
    assert response.status_code == 422


async def test_audit_export_preserves_nullable_legacy_dates_and_csv_cells(access):
    values = ["=1+1", " +SUM(1,2)", "\t=1", "\r=1", "\n=1", "@SUM(1)", "-1+2", 'comma,quote"\nline']
    async with access.db() as db:
        for value in values:
            db.add(AuditLog(event_type=value, group_id=access.groups["A"].id))
        await db.flush()
        await db.execute(text("UPDATE audit_log SET timestamp=NULL"))
        await db.commit()
    response = await access.call("GET", "/admin/audit.csv", "observer")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    rows = [row for row in rows if row["event_type"] != "seed"]
    assert [row["event_type"] for row in rows] == ["'" + value for value in values[:-1]] + values[
        -1:
    ]
    assert all(row["timestamp"] == "" for row in rows)


async def test_exports_recheck_authentication_and_independent_permissions(access, browser):
    from api.models.user import User

    identifier = await open_session(access)
    async with access.db() as db:
        (await db.get(User, access.users["admin_a"].id)).permissions = ["sessions.manage"]
        await db.commit()
    assert (await access.call("GET", "/admin/audit.csv", "admin_a")).status_code == 403
    assert identifier in (await access.call("GET", "/session-events.csv", "admin_a")).text
    assert identifier not in (await access.call("GET", "/session-events.csv", "observer")).text
    assert (await access.call("GET", "/admin/audit.csv", "observer")).status_code == 200
    await login(browser, "tech_a")
    assert (await browser.get("/api/v1/session-events.csv")).status_code == 200
    cookie = browser.cookies["__Host-factoryvpn_portal"]
    await browser.post("/api/v1/web/logout")
    assert (await browser.get("/api/v1/session-events.csv")).status_code == 403
    assert (
        await browser.get(
            "/api/v1/session-events.csv", headers={"Cookie": f"__Host-factoryvpn_portal={cookie}"}
        )
    ).status_code == 401
