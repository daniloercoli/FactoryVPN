"""Administrative client projection keeps identity and session scopes separate."""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.group import UserGroup
from api.models.session import Session
from api.models.user import User
from api.time_utils import utc_now
from test_vpn_catalog_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
    pytestmark as postgres_mark,
    start_session,
)

access = access_fixture
encoded_password = password_fixture
pytestmark = postgres_mark


async def test_supervisor_search_projects_only_minimal_authorized_identity(access):
    a = await start_session(access, "tech_a", "A")
    await start_session(access, "tech_b", "B")
    async with access.db() as db:
        (await db.get(User, access.users["tech_a"].id)).full_name = "Mario 100%_Nord"
        # sessions.manage alone grants supervision, not account administration.
        (await db.get(User, access.users["observer"].id)).permissions = ["sessions.manage"]
        await db.commit()
    for actor, manageable in (("admin_a", True), ("observer", False)):
        for q in ("tech_a", "100%_Nord", a):
            result = await access.call("GET", "/admin/sessions", actor, params={"q": q})
            assert result.status_code == 200, result.text
            assert result.headers["x-total-count"] == "1"
            row = result.json()[0]
            assert row["id"] == a and row["technician_username"] == "tech_a"
            assert row["technician_full_name"] == "Mario 100%_Nord"
            assert row["technician_manageable"] is manageable
            assert "email" not in row and "permissions" not in row
            assert "password_hash" not in result.text and "diagnostic_state" not in row
        for q in ("tech_b", "100X_Nord"):
            result = await access.call("GET", "/admin/sessions", actor, params={"q": q})
            assert result.json() == [] and result.headers["x-total-count"] == "0"
        result = await access.call(
            "GET", "/admin/sessions", actor, params={"q": "tech_a", "offset": 1}
        )
        assert result.json() == [] and result.headers["x-total-count"] == "1"
    own = await access.call("GET", "/sessions/", "tech_a")
    assert "technician_username" not in own.json()[0]
    assert (await access.call("GET", "/admin/sessions", "tech_a")).status_code == 403


@pytest.mark.parametrize("actor", ["shared", "root"])
async def test_shared_and_global_identity_is_masked_but_session_remains_visible(access, actor):
    identifier = await start_session(access, actor, "A")
    response = await access.call("GET", "/admin/sessions", "admin_a")
    assert response.headers["x-total-count"] == "1"
    row = response.json()[0]
    assert row["id"] == identifier
    assert row["technician_username"] is None and row["technician_full_name"] is None
    assert not row["technician_manageable"]
    search = await access.call("GET", "/admin/sessions", "admin_a", params={"q": actor})
    assert search.json() == [] and search.headers["x-total-count"] == "0"
    assert (await access.call("GET", "/admin/sessions")).json()[0]["technician_username"] == actor


async def test_identity_moved_outside_scope_is_not_revealed_by_history_or_search(access):
    identifier = await start_session(access, "tech_a", "A")
    await access.call("DELETE", f"/sessions/{identifier}", "tech_a")
    async with access.db() as db:
        await db.execute(delete(UserGroup).where(UserGroup.user_id == access.users["tech_a"].id))
        db.add(UserGroup(user_id=access.users["tech_a"].id, group_id=access.groups["B"].id))
        user = await db.get(User, access.users["tech_a"].id)
        user.full_name = "Reserved identity"
        await db.commit()
    history = await access.call("GET", "/admin/sessions", "admin_a")
    assert history.json()[0]["technician_full_name"] is None
    for q in ("Reserved identity", "tech_a"):
        result = await access.call("GET", "/admin/sessions", "admin_a", params={"q": q})
        assert result.json() == [] and result.headers["x-total-count"] == "0"


@pytest.mark.parametrize("search", [False, True])
async def test_identity_scope_is_rechecked_when_membership_changes_after_count(
    access, monkeypatch, search
):
    identifier = await start_session(access, "tech_a", "A")
    original_scalar = AsyncSession.scalar
    moved = False

    async def scalar(db, statement, *args, **kwargs):
        nonlocal moved
        result = await original_scalar(db, statement, *args, **kwargs)
        if not moved and "count(" in str(statement) and "sessions" in str(statement):
            moved = True
            changed = await access.call(
                "PUT",
                f'/admin/users/{access.users["tech_a"].id}',
                json={"group_ids": [str(access.groups["B"].id)], "full_name": "Private South"},
            )
            assert changed.status_code == 200, changed.text
        return result

    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    result = await access.call(
        "GET", "/admin/sessions", "admin_a", params={"q": "tech_a" if search else identifier}
    )
    assert moved and result.status_code == 200, result.text
    # The pre-transfer count can describe the old authorized snapshot. The data
    # read must not disclose the current identity from the newly assigned group.
    assert result.headers["x-total-count"] == "1"
    assert "Private South" not in result.text and "tech_a" not in result.text
    if search:
        assert result.json() == []
    else:
        assert result.json()[0]["id"] == identifier
        assert result.json()[0]["technician_username"] is None
        assert not result.json()[0]["technician_manageable"]


@pytest.mark.parametrize(
    "observed_age,received_age,connected,expected",
    [
        (0, 0, True, "ok"),
        (0, 0, False, "waiting"),
        (31, 0, True, "unknown"),
        (0, 31, True, "unknown"),
        (None, None, True, "unknown"),
    ],
)
async def test_client_evidence_requires_recent_observation_and_receipt(
    access, observed_age, received_age, connected, expected
):
    identifier = await start_session(access, "tech_a", "A")
    now = utc_now()
    async with access.db() as db:
        session = await db.get(Session, UUID(identifier))
        session.status = "ACTIVE"
        if observed_age is not None:
            session.diagnostic_state = {
                "worker": {
                    "observed_at": (now - timedelta(seconds=observed_age)).isoformat(),
                    "received_at": (now - timedelta(seconds=received_age)).isoformat(),
                    "technician_connected": connected,
                    "gateway_connected": True,
                    "network": "ready" if connected else "waiting",
                    "code": "none",
                }
            }
        await db.commit()
    response = await access.call("GET", "/admin/sessions", "admin_a", params={"reserved": True})
    row = response.json()[0]
    assert row["technician_vpn_ip"] and row["gateway_vpn_ip"]
    steps = {s["id"]: s for s in row["diagnostics"]["steps"]}
    assert steps["technician"]["status"] == expected
    if observed_age is None:
        assert row["observation_valid_until"] is None
    else:
        assert row["observation_valid_until"]
    await access.call("DELETE", f"/admin/sessions/{identifier}", "admin_a")
    closed = (await access.call("GET", "/admin/sessions", "admin_a")).json()[0]
    assert closed["cleanup_pending"] and closed["status"] == "CLOSED"
    assert all(
        s["status"] != "ok"
        for s in closed["diagnostics"]["steps"]
        if s["id"] in ("technician", "tunnel", "network")
    )


async def test_client_evidence_deadline_respects_session_expiry_and_unavailable_vpn(access):
    identifier = await start_session(access, "tech_a", "A")
    now = utc_now()
    async with access.db() as db:
        session = await db.get(Session, UUID(identifier))
        session.expires_at = now + timedelta(seconds=10)
        session.diagnostic_state = {
            "worker": {
                "observed_at": now.isoformat(),
                "received_at": now.isoformat(),
                "technician_connected": True,
                "gateway_connected": True,
                "network": "waiting",
                "code": "none",
            }
        }
        await db.commit()
    row = (await access.call("GET", "/admin/sessions", "admin_a")).json()[0]
    assert row["observation_valid_until"] == row["expires_at"]
    async with access.db() as db:
        session = await db.get(Session, UUID(identifier))
        session.diagnostic_state = {
            "worker": {
                **session.diagnostic_state["worker"],
                "code": "vpn_unavailable",
                "network": "unknown",
                "technician_connected": None,
                "gateway_connected": None,
            }
        }
        await db.commit()
    row = (await access.call("GET", "/admin/sessions", "admin_a")).json()[0]
    assert row["observation_valid_until"] is None
