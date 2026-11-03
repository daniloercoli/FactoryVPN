from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Float, BigInteger, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base


class TelemetrySnapshot(Base):
    __tablename__ = "telemetry_snapshots"
    source = Column(String(60), primary_key=True)
    gateway_id = Column(
        UUID(as_uuid=True), ForeignKey("gateways.id", ondelete="CASCADE"), unique=True
    )
    observed_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False)


class TelemetryBucket(Base):
    __tablename__ = "telemetry_buckets"
    id = Column(BigInteger, primary_key=True)
    source = Column(String(60), nullable=False)
    gateway_id = Column(UUID(as_uuid=True), ForeignKey("gateways.id", ondelete="CASCADE"))
    group_id = Column(UUID(as_uuid=True))  # Historical scope survives group deletion.
    context_id = Column(UUID(as_uuid=True), nullable=False)
    bucket_at = Column(DateTime(timezone=True), nullable=False)
    first_observed_at = Column(DateTime(timezone=True), nullable=False)
    last_observed_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    samples = Column(Integer, nullable=False)
    cpu_sum = Column(Float, nullable=False)
    cpu_count = Column(Integer, nullable=False)
    cpu_max = Column(Float)
    memory_sum = Column(Float, nullable=False)
    memory_count = Column(Integer, nullable=False)
    memory_max = Column(Float)
    disk_sum = Column(Float, nullable=False)
    disk_count = Column(Integer, nullable=False)
    disk_max = Column(Float)
    __table_args__ = (
        Index("uq_telemetry_bucket", "source", "context_id", "bucket_at", unique=True),
        Index("ix_telemetry_bucket_source_time", "source", "bucket_at", "id"),
        Index("ix_telemetry_bucket_gateway_time", "gateway_id", "bucket_at"),
        Index("ix_telemetry_bucket_group_time", "group_id", "bucket_at"),
        Index("ix_telemetry_bucket_time", "bucket_at", "id"),
    )
