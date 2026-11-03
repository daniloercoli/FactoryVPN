"""Bounded aggregates of accepted observations, with immutable historical scope."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID
from sqlalchemy import select, func, delete, text
from sqlalchemy.dialects.postgresql import insert
from api.models.gateway import Gateway
from api.models.telemetry import TelemetryBucket as Bucket
from api.services.access_service import scope
from api.services.credentials import current_credential
from api.time_utils import utc_now

BUCKET_SECONDS = 300
RETENTION_DAYS = 30
SOURCE_LIMIT = 9000
INSTANCE_LIMIT = 500000
HISTORY_LOCK = 76141904
PERIODS = {"1h": (3600, 300), "24h": (86400, 300), "7d": (604800, 1800), "30d": (2592000, 3600)}


def align(at, seconds):
    return datetime.fromtimestamp(int(at.timestamp()) // seconds * seconds, timezone.utc)


async def lock(db):
    return await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": HISTORY_LOCK})


async def record(db, source, gateway_id, report, now):
    # Never wait for maintenance while holding a VPN authorization lock.
    if not await lock(db):
        return
    group_id, context_id = None, UUID(int=0)
    if gateway_id:
        gateway = await db.get(Gateway, gateway_id)
        credential = await current_credential(db, gateway_id)
        if not gateway or not credential:
            return
        group_id, context_id = gateway.group_id, credential.id
    bucket_at = align(report.observed_at, BUCKET_SECONDS)
    existing = await db.scalar(
        select(Bucket.id).where(
            Bucket.source == source, Bucket.context_id == context_id, Bucket.bucket_at == bucket_at
        )
    )
    metrics = report.metrics
    values = {"cpu": metrics.cpu_percent}
    for kind in ("memory", "disk"):
        total, available = getattr(metrics, kind + "_total_bytes"), getattr(
            metrics, kind + "_available_bytes"
        )
        values[kind] = 100 * (total - available) / total if total else None
    fields = dict(
        source=source,
        gateway_id=gateway_id,
        group_id=group_id,
        context_id=context_id,
        bucket_at=bucket_at,
        first_observed_at=report.observed_at,
        last_observed_at=report.observed_at,
        received_at=now,
        samples=1,
    )
    updates = {
        "last_observed_at": report.observed_at,
        "received_at": now,
        "samples": Bucket.samples + 1,
    }
    for name, value in values.items():
        fields.update(
            {
                name + "_sum": value or 0,
                name + "_count": int(value is not None),
                name + "_max": value,
            }
        )
        updates[name + "_sum"] = getattr(Bucket, name + "_sum") + (value or 0)
        updates[name + "_count"] = getattr(Bucket, name + "_count") + int(value is not None)
        if value is not None:
            updates[name + "_max"] = func.greatest(getattr(Bucket, name + "_max"), value)
    statement = (
        insert(Bucket)
        .values(**fields)
        .on_conflict_do_update(
            index_elements=[Bucket.source, Bucket.context_id, Bucket.bucket_at], set_=updates
        )
    )
    await db.execute(statement)
    if existing is None:
        # Only a new bucket consumes storage; regular samples avoid global counts.
        for condition, limit in ((Bucket.source == source, SOURCE_LIMIT), (True, INSTANCE_LIMIT)):
            count = await db.scalar(select(func.count()).select_from(Bucket).where(condition))
            if count > limit:
                victims = (
                    select(Bucket.id)
                    .where(condition)
                    .order_by(Bucket.bucket_at, Bucket.id)
                    .limit(count - limit)
                )
                await db.execute(delete(Bucket).where(Bucket.id.in_(victims)))


def authorized_query(actor, gateway_id):
    if gateway_id is None:
        return select(Bucket).where(Bucket.source == "server:worker", Bucket.gateway_id.is_(None))
    return (
        select(Bucket)
        .join(Gateway, Gateway.id == Bucket.gateway_id)
        .where(
            Gateway.id == gateway_id, scope(Gateway.group_id, actor), scope(Bucket.group_id, actor)
        )
    )


async def history(db, actor, gateway_id, period):
    now = utc_now()
    duration, resolution = PERIODS[period]
    # Include both boundary buckets: otherwise "last hour" drops up to five
    # minutes still inside the requested period (one hour for the 30-day view).
    first = align(now - timedelta(seconds=duration), resolution)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    retained = authorized_query(actor, gateway_id).where(
        Bucket.bucket_at >= align(cutoff, BUCKET_SECONDS),
        Bucket.last_observed_at >= cutoff,
    )
    available = retained.subquery()
    bounds = (
        await db.execute(
            select(func.min(available.c.first_observed_at), func.max(available.c.last_observed_at))
        )
    ).one()
    selected = retained.where(Bucket.bucket_at >= first, Bucket.bucket_at <= now).subquery()
    at = func.to_timestamp(
        func.floor(func.extract("epoch", selected.c.bucket_at) / resolution) * resolution
    )
    columns = [
        at.label("at"),
        func.sum(selected.c.samples).label("samples"),
        func.min(selected.c.first_observed_at).label("first_observed_at"),
        func.max(selected.c.last_observed_at).label("last_observed_at"),
    ]
    for name in ("cpu", "memory", "disk"):
        columns.extend(
            [
                func.sum(selected.c[name + "_sum"]).label(name + "_sum"),
                func.sum(selected.c[name + "_count"]).label(name + "_count"),
                func.max(selected.c[name + "_max"]).label(name + "_max"),
            ]
        )
    rows = (await db.execute(select(*columns).group_by(at).order_by(at))).mappings().all()
    by_time = {row["at"]: row for row in rows}
    points = []
    for index in range(duration // resolution + 1):
        timestamp = first + timedelta(seconds=index * resolution)
        row = by_time.get(timestamp)
        point = dict(
            at=timestamp,
            samples=row["samples"] if row else 0,
            first_observed_at=row["first_observed_at"] if row else None,
            last_observed_at=row["last_observed_at"] if row else None,
        )
        for name in ("cpu", "memory", "disk"):
            count = row[name + "_count"] if row else 0
            point[name] = {
                "samples": count,
                "mean": row[name + "_sum"] / count if count else None,
                "peak": row[name + "_max"] if count else None,
            }
        points.append(point)
    return dict(
        since=first,
        until=now,
        resolution_seconds=resolution,
        retention_days=RETENTION_DAYS,
        available_from=bounds[0],
        available_until=bounds[1],
        points=points,
    )


async def prune(db):
    if await lock(db):
        victims = (
            select(Bucket.id)
            .where(
                Bucket.bucket_at < align(utc_now() - timedelta(days=RETENTION_DAYS), BUCKET_SECONDS)
            )
            .order_by(Bucket.bucket_at, Bucket.id)
            .limit(1000)
        )
        await db.execute(delete(Bucket).where(Bucket.id.in_(victims)))


async def maintenance(factory):
    while True:
        try:
            async with asyncio.timeout(3), factory() as db:
                await prune(db)
                await db.commit()
        except Exception:
            pass
        await asyncio.sleep(60)
