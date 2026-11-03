"""Durable opening receipts, scoped to the requesting account."""

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base


class SessionRequest(Base):
    __tablename__ = "session_requests"
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    request_key = Column(UUID(as_uuid=True), primary_key=True)
    request_hash = Column(String(64), nullable=False)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
