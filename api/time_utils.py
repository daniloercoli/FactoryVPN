"""Timezone-aware timestamps for PostgreSQL TIMESTAMP WITH TIME ZONE columns."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
