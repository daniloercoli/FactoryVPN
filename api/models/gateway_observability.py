"""Snapshots and bounded technical history, separate from audit and authorization."""

from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base


class GatewaySystemSnapshot(Base):
    __tablename__ = "gateway_system_snapshots"
    gateway_id = Column(
        UUID(as_uuid=True), ForeignKey("gateways.id", ondelete="CASCADE"), primary_key=True
    )
    credential_id = Column(UUID(as_uuid=True), nullable=False)
    report_id = Column(UUID(as_uuid=True), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    payload = Column(JSONB, nullable=False)


class GatewayTechnicalLog(Base):
    __tablename__ = "gateway_technical_logs"
    id = Column(Integer, primary_key=True)
    gateway_id = Column(
        UUID(as_uuid=True), ForeignKey("gateways.id", ondelete="CASCADE"), nullable=False
    )
    # Historical identifiers intentionally survive deletion of sessions/users/groups.
    group_id = Column(UUID(as_uuid=True))
    session_id = Column(UUID(as_uuid=True))
    technician_id = Column(UUID(as_uuid=True))
    context_id = Column(UUID(as_uuid=True), nullable=False)
    source = Column(String(10), nullable=False)
    event_id = Column(UUID(as_uuid=True), nullable=False)
    code = Column(String(40), nullable=False)
    component = Column(String(16), nullable=False)
    severity = Column(String(10), nullable=False)
    message = Column(String(200), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    first_observed_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    occurrences = Column(Integer, nullable=False)
    parameters = Column(JSONB, nullable=False)
    __table_args__ = (
        Index(
            "uq_gateway_log_event", "gateway_id", "source", "context_id", "event_id", unique=True
        ),
        Index("ix_gateway_log_gateway_time", "gateway_id", "received_at", "id"),
        Index("ix_gateway_log_group_time", "group_id", "received_at", "id"),
        Index("ix_gateway_log_session_time", "session_id", "received_at", "id"),
        Index("ix_gateway_log_time", "received_at", "id"),
    )
