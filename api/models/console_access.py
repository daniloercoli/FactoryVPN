"""Short-lived, single-use console proofs; only their hashes are persisted."""

from uuid import uuid4
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base
from api.time_utils import utc_now


class ConsoleAccessCode(Base):
    __tablename__ = "console_access_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_version = Column(Integer, nullable=False)
    purpose = Column(String(16), nullable=False)
    code_hash = Column(String(64), nullable=False, unique=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("purpose IN ('login', 'confirm')", name="console_access_purpose_valid"),
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="console_access_attempts_valid"),
    )
