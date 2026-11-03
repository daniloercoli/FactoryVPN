"""Short-lived proofs; codes and challenge bearer secrets are never persisted in plaintext."""

from uuid import uuid4
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base
from api.time_utils import utc_now


class EmailChallenge(Base):
    __tablename__ = "email_challenges"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    secret_hash = Column(String(64), nullable=False, unique=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_version = Column(Integer, nullable=False)
    purpose = Column(String(20), nullable=False)
    email = Column(String(255), nullable=False)
    next_email = Column(String(255))
    code_hash = Column(String(64), nullable=False)
    browser_hash = Column(String(64))
    web_session_id = Column(UUID(as_uuid=True))
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True))
