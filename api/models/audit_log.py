"""
Audit log model
"""

from api.time_utils import utc_now
from sqlalchemy import Column, String, DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB, INET
from api.db.session import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), nullable=False, index=True)
    group_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    timestamp = Column(DateTime(timezone=True), default=utc_now, index=True)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    session_id = Column(UUID(as_uuid=True), nullable=True)
    gateway_id = Column(UUID(as_uuid=True), nullable=True)
    ip_address = Column(INET, nullable=True)
    details = Column(JSONB, default=dict)
