"""Inventario ricercabile, duplicati ammessi e isolamento fra gruppi."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.models.gateway import Gateway
from api.time_utils import utc_now
from test_access_control_postgres import (
    access as access_fixture,
    encoded_password as password_fixture,
)
from test_web_portal_postgres import browser as browser_fixture, login

access, encoded_password, browser = access_fixture, password_fixture, browser_fixture
pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)
INVENTORY = {
    "serial_number": "Linea 100%_Nord",
    "imei": "012345678901234",
    "iccid": "08912345678901234567",
}


async def test_inventory_patch_preserves_omitted_fields_and_checks_scope_revision_and_unique_serial(
    access, browser
):
    gateway = access.gateways["A"].id
    path = f"/api/v1/admin/gateways/{gateway}"
    await login(browser, "admin_a")
    before = (await browser.get(path)).json()
    changed = await browser.patch(
        path, json=INVENTORY, headers={"If-Match": f'"{before["revision"]}"'}
    )
    assert changed.status_code == 200, changed.text
    assert all(changed.json()[key] == value for key, value in INVENTORY.items())
    assert changed.json()["revision"] != before["revision"]
    assert (
        await browser.patch(
            path, json={"imei": None}, headers={"If-Match": f'"{before["revision"]}"'}
        )
    ).status_code == 412
    for actor, status in (("tech_a", 403), ("admin_b", 404)):
        assert (
            await access.call("PATCH", path.removeprefix("/api/v1"), actor, json=INVENTORY)
        ).status_code == status
    named = await access.call(
        "PATCH", path.removeprefix("/api/v1"), "admin_a", json={"display_name": "New name"}
    )
    assert named.status_code == 200 and named.json()["imei"] == INVENTORY["imei"]
    duplicate = await access.call(
        "PATCH", f'/admin/gateways/{access.gateways["B"].id}', "root", json=INVENTORY
    )
    assert duplicate.status_code == 409
    cleared = await access.call(
        "PATCH", path.removeprefix("/api/v1"), "admin_a", json={"imei": "  ", "iccid": None}
    )
    assert (
        cleared.status_code == 200
        and cleared.json()["imei"] is None
        and cleared.json()["iccid"] is None
    )
    assert (
        cleared.json()["serial_number"] == INVENTORY["serial_number"]
        and cleared.json()["display_name"] == "New name"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"imei": "bad"},
        {"imei": "1" * 16},
        {"iccid": "12ABC"},
        {"iccid": "1" * 23},
        {"serial_number": "x" * 101},
        {"mac_address": "invalid"},
    ],
)
async def test_inventory_validation_does_not_change_network_configuration(access, body):
    result = await access.call(
        "PATCH", f'/admin/gateways/{access.gateways["A"].id}', "admin_a", json=body
    )
    assert result.status_code == 422


async def test_delegated_inventory_access_does_not_allow_operational_mac_change(access):
    result = await access.call(
        "PATCH",
        f'/admin/gateways/{access.gateways["A"].id}',
        "admin_a",
        json={"mac_address": "AA:BB:CC:DD:EE:FF"},
    )
    assert result.status_code == 403


@pytest.mark.parametrize("path,actor", [("/gateways/", "tech_a"), ("/admin/gateways", "admin_a")])
async def test_search_identifiers_duplicates_normalized_mac_pagination_and_scope(
    access, path, actor
):
    async with access.db() as db:
        for group in ("A", "B"):
            row = await db.get(Gateway, access.gateways[group].id)
            row.mac_address = "AA:BB:CC:DD:EE:FF"
            row.imei, row.iccid = INVENTORY["imei"], INVENTORY["iccid"]
            row.serial_number = INVENTORY["serial_number"] if group == "A" else "Hidden serial"
        duplicate = Gateway(
            id=uuid4(),
            group_id=access.groups["A"].id,
            activation_mode="wifi",
            mac_address="AA:BB:CC:DD:EE:FF",
            imei=INVENTORY["imei"],
            iccid=INVENTORY["iccid"],
        )
        db.add(duplicate)
        await db.commit()
    for query in (
        "  012345678901234  ",
        "345678901234",
        INVENTORY["iccid"],
        "aa-bb-cc-dd-ee-ff",
        "aabbccddeeff",
        "aabb.ccdd.eeff",
        "bb:cc:dd",
    ):
        first = await access.call("GET", path, actor, params={"q": query, "limit": 1})
        second = await access.call("GET", path, actor, params={"q": query, "limit": 1, "offset": 1})
        assert first.status_code == 200 and first.headers["x-total-count"] == "2", first.text
        assert second.headers["x-total-count"] == "2"
        assert {first.json()[0]["id"], second.json()[0]["id"]} == {
            str(access.gateways["A"].id),
            str(duplicate.id),
        }
        assert first.json()[0]["iccid"] == INVENTORY["iccid"]
    for query in ("linea", "%_", "  Linea 100%_Nord  "):
        result = await access.call("GET", path, actor, params={"q": query})
        assert (
            result.headers["x-total-count"] == "1"
            and result.json()[0]["serial_number"] == INVENTORY["serial_number"]
        )
    assert (await access.call("GET", path, actor, params={"q": "Hidden serial"})).json() == []


async def test_detected_identifiers_are_searchable_and_failed_or_old_reports_preserve_them(access):
    gateway = access.gateways["A"].id
    headers = {"X-Gateway-Key": access.keys[str(gateway)]}
    body = {
        "report_id": str(uuid4()),
        "observed_at": utc_now().isoformat(),
        "components": {
            "modem": {"state": "reported", "imei": INVENTORY["imei"], "iccid": INVENTORY["iccid"]}
        },
    }
    result = await access.client.post("/api/v1/gateways/system-info", json=body, headers=headers)
    assert result.status_code == 200 and result.json()["recorded"]
    detail = (await access.call("GET", f"/gateways/{gateway}", "tech_a")).json()
    assert detail["imei"] == INVENTORY["imei"] and detail["iccid"] == INVENTORY["iccid"]
    failed = {
        "report_id": str(uuid4()),
        "observed_at": utc_now().isoformat(),
        "components": {"modem": {"state": "collection_failed"}},
    }
    assert (
        await access.client.post("/api/v1/gateways/system-info", json=failed, headers=headers)
    ).json()["recorded"]
    replacement = {
        "report_id": str(uuid4()),
        "observed_at": utc_now().isoformat(),
        "components": {"modem": {"state": "reported", "iccid": "89123456789012345678"}},
    }
    assert (
        await access.client.post("/api/v1/gateways/system-info", json=replacement, headers=headers)
    ).json()["recorded"]
    assert not (
        await access.client.post("/api/v1/gateways/system-info", json=body, headers=headers)
    ).json()["recorded"]
    async with access.db() as db:
        row = await db.scalar(select(Gateway).where(Gateway.id == gateway))
        assert row.imei == INVENTORY["imei"] and row.iccid == "89123456789012345678"
    found = await access.call("GET", "/gateways/", "tech_a", params={"q": "89123456789012345678"})
    assert found.headers["x-total-count"] == "1" and found.json()[0]["id"] == str(gateway)
