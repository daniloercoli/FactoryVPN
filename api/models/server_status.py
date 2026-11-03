"""One deployment-wide SMTP check and its shared rate limit, no credentials."""

from sqlalchemy import Column, Integer, String, DateTime, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base


class SmtpCheck(Base):
    __tablename__ = "smtp_check"
    id = Column(Integer, primary_key=True)
    request_id = Column(UUID(as_uuid=True), nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    mode = Column(String(16), nullable=False)
    code = Column(String(32), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True))
    window_at = Column(DateTime(timezone=True), nullable=False)
    window_count = Column(Integer, nullable=False)
    __table_args__ = (CheckConstraint("id = 1", name="smtp_check_singleton"),)
