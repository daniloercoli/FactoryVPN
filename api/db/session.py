"""
Database session management
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from api.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)

async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()


async def init_db():
    """Require the complete initial schema before serving application traffic."""
    import re
    from sqlalchemy import text, inspect, CheckConstraint

    async with engine.connect() as conn:
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
        if version != "factoryvpn_initial":
            raise RuntimeError(f"Initialize the {settings.APP_NAME} schema before starting the API")
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        import api.models  # noqa: F401

        def differences(connection):
            context = MigrationContext.configure(connection, opts={"compare_server_default": True})
            changes = compare_metadata(context, Base.metadata)
            if changes:
                return changes
            # Alembic does not compare CHECK constraints. Verify their presence
            # and allowed literal values too (notably delegated permissions).
            inspector = inspect(connection)
            for table in Base.metadata.tables.values():
                actual = {
                    c["name"]: c["sqltext"] for c in inspector.get_check_constraints(table.name)
                }
                expected = {
                    c.name: str(c.sqltext)
                    for c in table.constraints
                    if isinstance(c, CheckConstraint)
                }
                if actual.keys() != expected.keys():
                    return True
                for name, expression in expected.items():
                    if set(re.findall(r"'([^']*)'", actual[name])) != set(
                        re.findall(r"'([^']*)'", expression)
                    ):
                        return True
            return False

        if await conn.run_sync(differences):
            raise RuntimeError(
                f"Initialize the {settings.APP_NAME} schema matching this application; existing data was not modified"
            )


async def get_db():
    """Dependency injection for database session"""
    async with async_session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
