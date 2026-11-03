"""Structured assistance history. Payloads contain only application-defined fields."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base
from api.time_utils import utc_now


class SessionEvent(Base):
    __tablename__ = "session_events"

    id = Column(Integer, primary_key=True)
    session_id = Column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    timestamp = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    source = Column(String(16), nullable=False)
    event_type = Column(String(50), nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=True)
    details = Column(JSONB, nullable=False, default=dict)
    __table_args__ = (Index("ix_session_events_session_time", "session_id", "timestamp", "id"),)
