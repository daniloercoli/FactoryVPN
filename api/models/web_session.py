"""Opaque browser sessions; only a hash of the cookie is persisted."""

from uuid import uuid4
from sqlalchemy import Column, String, DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base
from api.time_utils import utc_now


class WebSession(Base):
    __tablename__ = "web_sessions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    secret_hash = Column(String(64), nullable=False, unique=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_version = Column(Integer, nullable=False)
    # Retain a missing reference so deletion cannot turn SSO into local authentication.
    sso_session_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    reauthenticated_at = Column(DateTime(timezone=True), nullable=True)
    second_factor_at = Column(DateTime(timezone=True), nullable=True)
    second_factor_method = Column(String(16), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
