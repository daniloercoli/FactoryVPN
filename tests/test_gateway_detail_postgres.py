"""Gateway cards and inventory use real authentication and PostgreSQL scopes."""

import os
import asyncio
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from api.models.audit_log import AuditLog
from api.models.gateway import Gateway
from api.models.user import User
from api.models.session import Session
from api.models.device import Device
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
INPUT = {
    "name": "PLC reparto",
    "ip_address": "192.168.1.25",
    "mac_address": "aa-bb-cc-dd-ee-ff",
    "device_type": "PLC",
}


async def test_card_projects_network_without_metadata_or_foreign_resources(access):
    gateway = access.gateways["A"]
    async with access.db() as db:
        row = await db.get(Gateway, gateway.id)
        row.extra_metadata = {
            "lan_interface": "eth0",
            "lan_address": "192.168.1.1",
            "secret": "never-return",
        }
        row.serial_number = "serial-001"
        await db.commit()
    result = await access.call("GET", f"/gateways/{gateway.id}", "tech_a")
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    data = result.json()
    assert data["lan_cidr"] == "192.168.1.0/24" and data["lan_interface"] == "eth0"
    assert data["serial_number"] == "serial-001" and data["current_session"] is None
    assert "metadata" not in data and "never-return" not in result.text
    for actor in ("tech_b", "outsider"):
        assert (await access.call("GET", f"/gateways/{gateway.id}", actor)).status_code == 404
    assert (
        await access.call("GET", f'/gateways/{access.gateways["unassigned"].id}', "tech_a")
    ).status_code == 404


async def test_current_session_requires_ownership_or_session_permission_and_historical_scope(
    access,
):
    gateway = access.gateways["A"]
    created = await access.call(
        "POST", "/sessions/", "tech_a", json={"gateway_id": str(gateway.id)}
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    for actor, visible in [
        ("tech_a", True),
        ("admin_a", True),
        ("root", True),
        ("shared", False),
        ("observer", False),
    ]:
        data = (await access.call("GET", f"/gateways/{gateway.id}", actor)).json()
        assert data["availability"] == "busy"
        assert (data["current_session"] is not None) == visible
        if visible:
            assert data["current_session"]["id"] == session_id
        else:
            assert session_id not in str(data)
    async with access.db() as db:
        row = await db.scalar(select(Session))
        row.status = "CLOSED"
        await db.commit()
    data = (await access.call("GET", f"/gateways/{gateway.id}", "admin_a")).json()
    assert data["availability"] == "cleanup_pending" and data["current_session"]["cleanup_pending"]
    # Even inconsistent historical scope must never expose another group's session.
    async with access.db() as db:
        row = await db.scalar(select(Session))
        row.group_id = access.groups["B"].id
        await db.commit()
    assert (await access.call("GET", f"/gateways/{gateway.id}", "admin_a")).json()[
        "current_session"
    ] is None


@pytest.mark.parametrize("admin", [False, True])
async def test_inventory_search_counts_pagination_and_scope(access, admin):
    gateway = access.gateways["A"].id
    path = f"/admin/gateways/{gateway}/devices" if admin else f"/devices/gateway/{gateway}"
    actor = "admin_a" if admin else "tech_a"
    async with access.db() as db:
        db.add_all(
            [
                Device(gateway_id=gateway, name="PLC 100%_A", device_type="PLC"),
                Device(gateway_id=gateway, name="HMI A", device_type="HMI"),
            ]
        )
        await db.commit()
    result = await access.call(
        "GET", path, actor, params={"q": "%_", "device_type": "PLC", "limit": 1}
    )
    assert result.status_code == 200 and result.headers["x-total-count"] == "1"
    assert result.json()[0]["name"] == "PLC 100%_A" and result.json()[0]["revision"]
    result = await access.call("GET", path, actor, params={"q": "PLC", "limit": 1, "offset": 1})
    assert len(result.json()) == 1 and result.headers["x-total-count"] == "2"
    assert (await access.call("GET", path, "admin_b")).status_code == 404
    assert (await access.call("GET", path, actor, params={"q": "PLC B"})).headers[
        "x-total-count"
    ] == "0"


async def test_browser_inventory_crud_revisions_csrf_and_audit(access, browser):
    await login(browser, "admin_a")
    gateway = access.gateways["A"].id
    base = f"/api/v1/admin/gateways/{gateway}/devices"
    revision = (await browser.get(f"/api/v1/gateways/{gateway}")).json()["revision"]
    headers = {"If-Match": f'"{revision}"'}
    assert (await browser.post(base, json=INPUT)).status_code == 428
    assert (
        await browser.post(base, json=INPUT, headers={**headers, "X-CSRF-Token": ""})
    ).status_code == 403
    created = await browser.post(base, json=INPUT, headers=headers)
    assert created.status_code == 201, created.text
    identifier = created.json()["id"]
    rows = (await browser.get(f"/api/v1/devices/gateway/{gateway}")).json()
    row = next(r for r in rows if r["id"] == identifier)
    assert row["mac_address"] == "AA:BB:CC:DD:EE:FF" and row["last_seen"] is None
    path = base + "/" + identifier
    assert (await browser.put(path, json=INPUT)).status_code == 428
    assert (await browser.delete(path)).status_code == 428
    old = {"If-Match": f'"{row["revision"]}"'}
    edited = await browser.put(
        path, json={**INPUT, "name": "HMI reparto", "device_type": "HMI"}, headers=old
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] != row["revision"]
    assert (await browser.put(path, json=INPUT, headers=old)).status_code == 412
    assert (await browser.delete(path, headers=old)).status_code == 412
    assert (
        await browser.delete(path, headers={"If-Match": f'"{edited.json()["revision"]}"'})
    ).status_code == 204
    async with access.db() as db:
        events = (
            await db.scalars(select(AuditLog).where(AuditLog.event_type.like("device_%")))
        ).all()
        assert {e.event_type for e in events} == {
            "device_added",
            "device_updated",
            "device_deleted",
        }
        assert all(e.group_id == access.groups["A"].id and e.gateway_id == gateway for e in events)


@pytest.mark.parametrize("actor,expected", [("tech_a", 403), ("observer", 403), ("admin_b", 404)])
async def test_inventory_mutations_check_capability_and_scope(access, actor, expected):
    base = f'/admin/gateways/{access.gateways["A"].id}/devices'
    path = base + "/" + str(access.devices["A"].id)
    for method, url, body in [("POST", base, INPUT), ("PUT", path, INPUT), ("DELETE", path, None)]:
        assert (
            await access.call(method, url, actor, **({"json": body} if body else {}))
        ).status_code == expected


async def test_device_cannot_be_addressed_through_a_different_gateway(access):
    path = f'/admin/gateways/{access.gateways["A"].id}/devices/{access.devices["B"].id}'
    assert (await access.call("PUT", path, json=INPUT)).status_code == 404
    assert (await access.call("DELETE", path)).status_code == 404


async def test_transfer_invalidates_device_form_even_when_both_groups_visible(access, browser):
    await login(browser)
    gateway = access.gateways["A"].id
    base = f"/api/v1/admin/gateways/{gateway}"
    before = (await browser.get(base + "/devices")).json()[0]
    gw = (await browser.get(base)).json()
    assert (
        await browser.put(
            base + "/group",
            json={"group_id": str(access.groups["B"].id)},
            headers={"If-Match": f'"{gw["revision"]}"'},
        )
    ).status_code == 200
    old = {"If-Match": f'"{before["revision"]}"'}
    assert (
        await browser.put(base + "/devices/" + before["id"], json=INPUT, headers=old)
    ).status_code == 412
    assert (
        await browser.post(
            base + "/devices", json=INPUT, headers={"If-Match": f'"{gw["revision"]}"'}
        )
    ).status_code == 412
    for url in (f"/gateways/{gateway}", f"/devices/gateway/{gateway}"):
        assert (await access.call("GET", url, "admin_a")).status_code == 404


async def test_revoked_browser_cannot_write_inventory(access, browser):
    await login(browser, "admin_a")
    gateway = access.gateways["A"].id
    revision = (await browser.get(f"/api/v1/gateways/{gateway}")).json()["revision"]
    async with access.db() as db:
        user = await db.get(User, access.users["admin_a"].id)
        user.token_version += 1
        await db.commit()
    assert (
        await browser.post(
            f"/api/v1/admin/gateways/{gateway}/devices",
            json=INPUT,
            headers={"If-Match": f'"{revision}"'},
        )
    ).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"name": "   "},
        {"ip_address": "::1"},
        {"mac_address": "bad"},
        {"device_type": "UNKNOWN"},
        {"status": "ACTIVE"},
        {"gateway_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_inventory_rejects_invalid_or_uneditable_fields(access, change):
    path = f'/admin/gateways/{access.gateways["A"].id}/devices/{access.devices["A"].id}'
    assert (await access.call("PUT", path, json={**INPUT, **change})).status_code == 422


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("after_count", [False, True])
async def test_inventory_rechecks_scope_in_data_and_count_queries(
    access, monkeypatch, admin, after_count
):
    """A transfer after the initial lookup must not disclose the new group's data."""
    from api.services import device_inventory

    gateway = access.gateways["A"].id
    moved = False

    async def transfer():
        nonlocal moved
        if moved:
            return
        moved = True
        result = await access.call(
            "PUT", f"/admin/gateways/{gateway}/group", json={"group_id": str(access.groups["B"].id)}
        )
        assert result.status_code == 200, result.text
        result = await access.call(
            "POST",
            f"/admin/gateways/{gateway}/devices",
            json={**INPUT, "name": "Private new group inventory"},
        )
        assert result.status_code == 201, result.text

    original_lookup = device_inventory.gateway_for_user
    original_scalar = AsyncSession.scalar

    async def lookup(*args, **kwargs):
        found = await original_lookup(*args, **kwargs)
        if not after_count:
            await transfer()
        return found

    async def scalar(db, statement, *args, **kwargs):
        result = await original_scalar(db, statement, *args, **kwargs)
        if after_count and "count(" in str(statement) and "devices" in str(statement):
            await transfer()
        return result

    monkeypatch.setattr(device_inventory, "gateway_for_user", lookup)
    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    path = f"/admin/gateways/{gateway}/devices" if admin else f"/devices/gateway/{gateway}"
    result = await access.call("GET", path, "admin_a" if admin else "tech_a")
    assert moved
    assert result.status_code == 200, result.text
    assert result.json() == []
    # A count read before transfer can report the old authorized snapshot only.
    assert result.headers["x-total-count"] == ("1" if after_count else "0")


async def test_concurrent_device_updates_allow_only_one_revision(access, browser):
    await login(browser, "admin_a")
    gateway = access.gateways["A"].id
    row = (await browser.get(f"/api/v1/devices/gateway/{gateway}")).json()[0]
    path = f'/api/v1/admin/gateways/{gateway}/devices/{row["id"]}'
    results = await asyncio.gather(
        *[
            browser.put(
                path, json={**INPUT, "name": name}, headers={"If-Match": f'"{row["revision"]}"'}
            )
            for name in ("Editor one", "Editor two")
        ]
    )
    assert sorted(result.status_code for result in results) == [200, 412]
    winner = next(result.json() for result in results if result.status_code == 200)
    async with access.db() as db:
        row = await db.get(Device, access.devices["A"].id)
        assert row.name == winner["name"]


async def test_legacy_device_creation_cannot_bypass_browser_revision_or_csrf(access, browser):
    await login(browser, "admin_a")
    gateway = access.gateways["A"].id
    revision = (await browser.get(f"/api/v1/gateways/{gateway}")).json()["revision"]
    body = {**INPUT, "gateway_id": str(gateway)}
    path = "/api/v1/devices/"
    assert (await browser.post(path, json=body)).status_code == 428
    headers = {"If-Match": f'"{revision}"'}
    assert (
        await browser.post(path, json=body, headers={**headers, "X-CSRF-Token": ""})
    ).status_code == 403
    result = await browser.post(path, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert result.json()["last_seen"] is None and result.json()["revision"]
    assert (
        await browser.post(
            path, json={**body, "gateway_id": str(access.gateways["B"].id)}, headers=headers
        )
    ).status_code == 404


async def test_inventory_revision_uses_the_gateway_assignment_read_with_the_device(
    access, monkeypatch
):
    from api.services import device_inventory

    gateway = access.gateways["A"].id
    original = device_inventory.gateway_for_user
    earlier = []

    async def lookup(*args, **kwargs):
        row = await original(*args, **kwargs)
        if not earlier:
            earlier.append(row)  # Keep the old ORM instance alive in the identity map.
            result = await access.call(
                "PUT",
                f"/admin/gateways/{gateway}/group",
                json={"group_id": str(access.groups["B"].id)},
            )
            assert result.status_code == 200
        return row

    monkeypatch.setattr(device_inventory, "gateway_for_user", lookup)
    path = f"/devices/gateway/{gateway}"
    during = await access.call("GET", path)
    after = await access.call("GET", path)
    assert during.status_code == after.status_code == 200
    assert during.json()[0]["revision"] == after.json()[0]["revision"]
