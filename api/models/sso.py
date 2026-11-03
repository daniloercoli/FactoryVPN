"""External identities and server-side SSO sessions; no external authorization grants."""

from uuid import uuid4
from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Boolean,
    false,
)
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base
from api.time_utils import utc_now


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id = Column(String(64), nullable=False)
    issuer = Column(String(512), nullable=False)
    subject = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_external_identity"),)


class SSOSession(Base):
    __tablename__ = "sso_sessions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True)
    provider_id = Column(String(64), nullable=False)
    provider_fingerprint = Column(String(64), nullable=False)
    issuer = Column(String(512), nullable=False)
    subject = Column(String(255), nullable=False)
    token_version = Column(Integer, nullable=False)
    mfa_verified = Column(Boolean, nullable=False, default=False, server_default=false())
    tokens_encrypted = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    verified_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revocation_reason = Column(String(200), nullable=True)


class SSOLoginAttempt(Base):
    __tablename__ = "sso_login_attempts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    portal = Column(Boolean, nullable=False, default=False, server_default=false())
    provider_id = Column(String(64), nullable=False)
    provider_fingerprint = Column(String(64), nullable=False)
    state_hash = Column(String(64), nullable=False, unique=True)
    secrets_encrypted = Column(Text, nullable=True)
    browser_hash = Column(String(64), nullable=True)
    client_challenge = Column(String(43), nullable=False)
    client_state = Column(String(128), nullable=False)
    redirect_uri = Column(String(1024), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    exchange_hash = Column(String(64), nullable=True, unique=True)
    sso_session_id = Column(UUID(as_uuid=True), nullable=True)
