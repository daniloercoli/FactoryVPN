# ruff: noqa: E402
"""Fixtures for the disposable browser test database, never application startup."""

import asyncio
import json
from sqlalchemy import select
from provisioning_helpers import seed_gateway, seed_certificate
import os
import sys
from uuid import UUID, uuid4

if (
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL")
    or os.environ.get("DATABASE_URL") != os.environ["FACTORYVPN_TEST_DATABASE_URL"]
):
    raise RuntimeError("A disposable test database is required")

from api.db.session import async_session_maker, engine
from api.models import User, Group, UserGroup, Gateway, ExternalIdentity
from api.models.provisioning import GatewayCredential, VPNCertificate
from api.time_utils import utc_now
from api.services.auth_service import hash_password
from api.services.lifecycle import allocation_lock
from api.permissions import ALL_PERMISSIONS


def identifier(number):
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


async def seed():
    password = hash_password("factoryvpn-browser-tests")
    async with async_session_maker() as db:
        db.add_all(
            [
                Group(
                    id=identifier(1),
                    name="Produzione Nord",
                    description="Impianti e tecnici del distretto nord",
                ),
                Group(
                    id=identifier(2),
                    name="Produzione Sud",
                    description="Impianti del distretto sud",
                ),
            ]
        )
        users = [
            User(
                id=identifier(10),
                username="root",
                full_name="Amministratore",
                password_hash=password,
                is_admin=True,
                permissions=ALL_PERMISSIONS,
            ),
            User(
                id=identifier(11),
                username="delegate",
                full_name="Responsabile Nord",
                password_hash=password,
                permissions=ALL_PERMISSIONS,
            ),
            User(
                id=identifier(12),
                username="technician",
                full_name="Tecnico Nord",
                password_hash=password,
                permissions=["connect"],
            ),
            User(
                id=identifier(13),
                username="southern",
                full_name="Tecnico Sud",
                password_hash=password,
                permissions=["connect"],
            ),
            User(
                id=identifier(14),
                username="shared",
                full_name="Tecnico condiviso",
                password_hash=password,
                permissions=["connect"],
            ),
            User(
                id=identifier(15),
                username="sso-user",
                full_name="Utente aziendale",
                auth_mode="sso",
                password_hash=None,
                permissions=["connect"],
            ),
        ]
        for user in users:
            user.email = user.username + "@example.test"
        db.add_all(users)
        await db.flush()
        for user, groups in [(11, [1]), (12, [1]), (13, [2]), (14, [1, 2]), (15, [1])]:
            db.add_all(
                [
                    UserGroup(user_id=identifier(user), group_id=identifier(group))
                    for group in groups
                ]
            )
        db.add(
            ExternalIdentity(
                user_id=identifier(15),
                provider_id="test",
                issuer=os.environ["FACTORYVPN_TEST_IDP_URL"],
                subject="browser-user",
            )
        )
        db.add_all(
            [
                Gateway(
                    id=identifier(20),
                    display_name="Pressa Nord",
                    group_id=identifier(1),
                    mac_address="02:00:00:00:00:01",
                    activation_mode="wifi",
                    status="INACTIVE",
                ),
                Gateway(
                    id=identifier(21),
                    display_name="Pressa Sud",
                    group_id=identifier(2),
                    mac_address="02:00:00:00:00:02",
                    activation_mode="wifi",
                    status="INACTIVE",
                ),
                Gateway(
                    id=identifier(22),
                    group_id=None,
                    mac_address="02:00:00:00:00:03",
                    activation_mode="wifi",
                    status="PENDING",
                ),
                Gateway(
                    id=identifier(23),
                    group_id=identifier(1),
                    mac_address="02:00:00:00:00:04",
                    phone_number="+393331234567",
                    activation_mode="sms",
                    display_name="Gateway SMS Nord",
                    status="INACTIVE",
                ),
            ]
        )
        await db.flush()
        for user in users:
            await seed_certificate(db, "technician", user.id)
        keys = json.loads(os.environ["FACTORYVPN_TEST_GATEWAY_KEYS"])
        for gateway in (await db.scalars(select(Gateway))).all():
            await seed_gateway(db, gateway, keys[str(gateway.id)])
        await db.commit()
    await engine.dispose()


async def restore_machine_credentials():
    keys = json.loads(os.environ["FACTORYVPN_TEST_GATEWAY_KEYS"])
    async with async_session_maker() as db:
        await allocation_lock(db)
        for user in (await db.scalars(select(User))).all():
            user.email_last_sent_at = user.email_window_at = user.email_locked_until = None
            user.email_send_count = user.email_failures = 0
        for number in (20, 21, 22, 23):
            gateway = await db.get(Gateway, identifier(number))
            for credential in (
                await db.scalars(
                    select(GatewayCredential).where(GatewayCredential.gateway_id == gateway.id)
                )
            ).all():
                await db.delete(credential)
            for cert in (
                await db.scalars(
                    select(VPNCertificate).where(
                        VPNCertificate.kind == "gateway",
                        VPNCertificate.peer_id == gateway.id,
                        VPNCertificate.state == "active",
                    )
                )
            ).all():
                cert.state, cert.revoked_at = "revoked", utc_now()
            await db.flush()
            # Reuse the test transport keys, with a fresh certificate serial.
            await seed_gateway(db, gateway, keys[str(gateway.id)], serial=str(uuid4().int))
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(restore_machine_credentials() if "--credentials-only" in sys.argv else seed())
