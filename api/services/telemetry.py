from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from api.config import settings
from api.models.telemetry import TelemetrySnapshot
from api.time_utils import utc_now


async def record(db, source, gateway_id, report):
    now = utc_now()
    if report.observed_at > now + timedelta(seconds=30):
        raise HTTPException(422, "Observation timestamp is in the future")
    if report.observed_at < now - timedelta(minutes=5):
        return False
    stmt = insert(TelemetrySnapshot).values(
        source=source,
        gateway_id=gateway_id,
        observed_at=report.observed_at,
        received_at=now,
        payload=report.model_dump(mode="json", exclude={"observed_at"}),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["source"],
        set_={
            "observed_at": stmt.excluded.observed_at,
            "received_at": now,
            "payload": stmt.excluded.payload,
        },
        where=(TelemetrySnapshot.observed_at < report.observed_at)
        & (TelemetrySnapshot.received_at <= now - timedelta(seconds=10)),
    ).returning(TelemetrySnapshot.source)
    accepted = (await db.scalar(stmt)) is not None
    if accepted:
        from api.services.telemetry_history import record as record_history

        await record_history(db, source, gateway_id, report, now)
    return accepted


def project(row, gateway=None, *, server=False):
    remaining = (
        max(0, min(90, 90 - (utc_now() - min(row.observed_at, row.received_at)).total_seconds()))
        if row
        else 0
    )
    fresh = remaining > 0
    names = ("worker", "openvpn") if server else ("agent", "control", "openvpn", "network")
    services = row.payload["services"] if fresh else dict.fromkeys(names, "unknown")
    if server:
        # This authenticated read has just queried PostgreSQL successfully.
        services = {**services, "api": "ok", "database": "ok"}
    return dict(
        gateway_id=gateway.id if gateway else None,
        display_name=gateway.display_name if gateway else f"Server {settings.APP_NAME}",
        mac_address=gateway.mac_address if gateway else None,
        activation_mode=gateway.activation_mode if gateway else None,
        freshness="fresh" if fresh else "stale" if row else "missing",
        valid_for_seconds=remaining,
        observed_at=row.observed_at if row else None,
        received_at=row.received_at if row else None,
        metrics=row.payload["metrics"] if fresh else None,
        services=services,
    )
