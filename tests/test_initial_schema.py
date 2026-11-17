"""Validate the complete first-install schema against PostgreSQL and ORM contracts."""

import asyncio
import os
import re
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from api.db.session import Base
import api.models  # noqa: F401

pytestmark = pytest.mark.skipif(
    not os.getenv("FACTORYVPN_TEST_DATABASE_URL"), reason="Dedicated PostgreSQL required"
)


@pytest.fixture
def initial_database():
    url = make_url(os.environ["FACTORYVPN_TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg2")
    schema = "factoryvpn_initial_" + uuid4().hex
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield engine, config, schema, url
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def initialize(engine, config):
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "head")


def test_initial_schema_contains_all_models_constraints_and_indexes(initial_database):
    engine, config, _, _ = initial_database
    initialize(engine, config)
    with engine.connect() as conn:
        inspector = inspect(conn)
        assert set(inspector.get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "factoryvpn_initial"
        context = MigrationContext.configure(conn, opts={"compare_server_default": True})
        assert compare_metadata(context, Base.metadata) == []
        for table in Base.metadata.tables.values():
            from sqlalchemy import CheckConstraint

            assert {item["name"] for item in inspector.get_check_constraints(table.name)} == {
                constraint.name
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }


def test_gateway_inventory_allows_duplicates_except_serial_number(initial_database):
    engine, config, _, _ = initial_database
    initialize(engine, config)
    statement = text(
        "INSERT INTO gateways (id, activation_mode, mac_address, serial_number, imei, iccid) "
        "VALUES (:id, 'wifi', 'AA:BB:CC:DD:EE:FF', :serial, '012345678901234', '08912345678901234567')"
    )
    with engine.begin() as conn:
        for serial in ("factory-001", "factory-002", None, None):
            conn.execute(statement, {"id": uuid4(), "serial": serial})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(statement, {"id": uuid4(), "serial": "factory-001"})


def test_initialization_is_repeatable_without_rewriting_accounts(initial_database):
    engine, config, _, _ = initial_database
    initialize(engine, config)
    user = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, permissions) VALUES "
                "(:id, 'operator', 'preserved', ARRAY['connect','telemetry.read'])"
            ),
            {"id": user},
        )
    initialize(engine, config)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT password_hash, permissions, token_version FROM users WHERE id=:id"),
            {"id": user},
        ).one()
        assert row == ("preserved", ["connect", "telemetry.read"], 1)


def test_nonempty_unversioned_schema_is_not_adopted_or_modified(initial_database):
    engine, config, _, _ = initial_database
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id integer PRIMARY KEY)"))
        conn.execute(text("INSERT INTO users VALUES (42)"))
    with pytest.raises(RuntimeError, match="requires an empty"):
        initialize(engine, config)
    with engine.connect() as conn:
        assert inspect(conn).get_table_names() == ["users"]
        assert conn.scalar(text("SELECT id FROM users")) == 42


@pytest.mark.parametrize("invalid", ["permissions", "token_version", "auth_mode"])
def test_initial_user_constraints_reject_invalid_access_state(initial_database, invalid):
    engine, config, _, _ = initial_database
    initialize(engine, config)
    values = dict(id=uuid4(), permissions=["connect"], version=1, mode="local", password="secret")
    if invalid == "permissions":
        values["permissions"] = ["unknown.permission"]
    elif invalid == "token_version":
        values["version"] = 0
    else:
        values["mode"] = "sso"  # SSO must not retain a local password.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, auth_mode, permissions, token_version) "
                "VALUES (:id, 'invalid', :password, :mode, :permissions, :version)"
            ),
            values,
        )


def test_initial_session_exclusivity_and_gateway_telemetry_cleanup(initial_database):
    engine, config, _, _ = initial_database
    initialize(engine, config)
    gateway, session = uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gateways (id, mac_address, activation_mode) VALUES (:id, 'AA:BB:CC:DD:EE:FF', 'wifi')"
            ),
            {"id": gateway},
        )
        conn.execute(
            text("INSERT INTO sessions (id, gateway_id, status) VALUES (:id, :gateway, 'CLOSED')"),
            {"id": session, "gateway": gateway},
        )
        conn.execute(
            text(
                "INSERT INTO telemetry_snapshots (source, gateway_id, observed_at, received_at, payload) "
                "VALUES (:source, :gateway, now(), now(), '{}'::jsonb)"
            ),
            {"source": f"gateway:{gateway}", "gateway": gateway},
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        # A terminal session still reserves its gateway until cleanup is acknowledged.
        conn.execute(
            text(
                "INSERT INTO sessions (id, gateway_id, status) VALUES (:id, :gateway, 'REQUESTED')"
            ),
            {"id": uuid4(), "gateway": gateway},
        )
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gateways WHERE id=:id"), {"id": gateway})
        assert conn.scalar(text("SELECT count(*) FROM telemetry_snapshots")) == 0
        assert (
            conn.scalar(
                text("SELECT count(*) FROM sessions WHERE id=:id AND gateway_id IS NULL"),
                {"id": session},
            )
            == 1
        )


@pytest.mark.parametrize("revision", ["factoryvpn_initial", "0011"])
def test_api_requires_the_complete_initial_revision(initial_database, monkeypatch, revision):
    engine, config, schema, url = initial_database
    initialize(engine, config)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE alembic_version SET version_num=:revision"), {"revision": revision}
        )

    async def check():
        import api.db.session as database_module

        runtime = create_async_engine(
            url.set(drivername="postgresql+asyncpg"),
            connect_args={"server_settings": {"search_path": schema}},
        )
        monkeypatch.setattr(database_module, "engine", runtime)
        try:
            if revision == "factoryvpn_initial":
                await database_module.init_db()
            else:
                with pytest.raises(
                    RuntimeError,
                    match=re.escape(f"Initialize the {database_module.settings.APP_NAME} schema"),
                ):
                    await database_module.init_db()
        finally:
            await runtime.dispose()

    asyncio.run(check())


@pytest.mark.parametrize("missing", ["table", "permission", "mac_constraint"])
def test_api_rejects_older_structure_without_modifying_it(initial_database, monkeypatch, missing):
    engine, config, schema, url = initial_database
    initialize(engine, config)
    with engine.begin() as conn:
        if missing == "table":
            conn.execute(text("DROP TABLE gateway_system_snapshots"))
        elif missing == "permission":
            conn.execute(text("ALTER TABLE users DROP CONSTRAINT user_permissions_valid"))
            conn.execute(
                text(
                    "ALTER TABLE users ADD CONSTRAINT user_permissions_valid CHECK (permissions <@ ARRAY['connect']::varchar[])"
                )
            )
        else:
            conn.execute(text("ALTER TABLE gateways DROP CONSTRAINT gateway_wifi_mac"))

    async def check():
        import api.db.session as module

        runtime = create_async_engine(
            url.set(drivername="postgresql+asyncpg"),
            connect_args={"server_settings": {"search_path": schema}},
        )
        monkeypatch.setattr(module, "engine", runtime)
        try:
            with pytest.raises(RuntimeError, match="existing data was not modified"):
                await module.init_db()
        finally:
            await runtime.dispose()

    asyncio.run(check())
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "factoryvpn_initial"
