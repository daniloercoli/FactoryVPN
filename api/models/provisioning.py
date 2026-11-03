"""Persistent issuance requests and public certificate inventory; secrets are encrypted."""

from uuid import uuid4
from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Integer,
    Boolean,
    ForeignKey,
    Index,
    CheckConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base
from api.time_utils import utc_now


class VPNCertificate(Base):
    __tablename__ = "vpn_certificates"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    kind = Column(String(12), nullable=False)
    # Retained after peer deletion so the issuer can still publish its revocation.
    peer_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    serial = Column(String(50), nullable=False, unique=True)
    certificate_pem = Column(Text, nullable=False)
    not_before = Column(DateTime(timezone=True), nullable=False)
    not_after = Column(DateTime(timezone=True), nullable=False)
    state = Column(String(12), nullable=False, default="candidate")
    revoked_at = Column(DateTime(timezone=True))
    crl_applied_at = Column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("kind IN ('gateway','technician')", name="vpn_certificate_kind"),
        CheckConstraint("state IN ('candidate','active','revoked')", name="vpn_certificate_state"),
        Index(
            "uq_vpn_certificate_active_peer",
            "kind",
            "peer_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
        ),
    )


class GatewayCredential(Base):
    __tablename__ = "gateway_credentials"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    gateway_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateways.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    secret_hash = Column(String(64), nullable=False, unique=True)
    sms_secret_encrypted = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=False)
    registered_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "uq_gateway_credential_active",
            "gateway_id",
            unique=True,
            postgresql_where=text("active = true"),
        ),
    )


class ProvisioningJob(Base):
    __tablename__ = "provisioning_jobs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_id = Column(UUID(as_uuid=True), nullable=False)
    actor_version = Column(Integer, nullable=False)
    access_hash = Column(String(64), nullable=False)
    access_expires_at = Column(DateTime(timezone=True), nullable=False)
    web_session_id = Column(UUID(as_uuid=True))
    sso_session_id = Column(UUID(as_uuid=True))
    request_key = Column(UUID(as_uuid=True), nullable=False)
    request_hash = Column(String(64), nullable=False)
    kind = Column(String(12), nullable=False)
    peer_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    group_id = Column(UUID(as_uuid=True))
    parameters = Column(JSONB, nullable=False, default=dict)
    state = Column(String(12), nullable=False, default="queued")
    certificate_id = Column(
        UUID(as_uuid=True), ForeignKey("vpn_certificates.id", ondelete="RESTRICT")
    )
    credential_id = Column(
        UUID(as_uuid=True), ForeignKey("gateway_credentials.id", ondelete="SET NULL")
    )
    artifact_encrypted = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    downloaded_at = Column(DateTime(timezone=True))
    error_code = Column(String(40))
    __table_args__ = (
        CheckConstraint("kind IN ('gateway','technician')", name="provisioning_job_kind"),
        CheckConstraint(
            "state IN ('queued','ready','active','cancelled','failed','expired')",
            name="provisioning_job_state",
        ),
        Index("uq_provisioning_request", "actor_id", "request_key", unique=True),
        Index(
            "uq_provisioning_pending_peer",
            "kind",
            "peer_id",
            unique=True,
            postgresql_where=text("state IN ('queued','ready')"),
        ),
    )
