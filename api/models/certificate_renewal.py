"""Durable device CSR renewal; independent from human browser provisioning jobs."""

from uuid import uuid4
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Index, CheckConstraint, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base
from api.time_utils import utc_now


class CertificateRenewal(Base):
    __tablename__ = "certificate_renewals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    gateway_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateways.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    credential_id = Column(
        UUID(as_uuid=True), ForeignKey("gateway_credentials.id", ondelete="CASCADE"), nullable=False
    )
    group_id = Column(UUID(as_uuid=True), nullable=False)
    previous_certificate_id = Column(
        UUID(as_uuid=True), ForeignKey("vpn_certificates.id", ondelete="RESTRICT"), nullable=False
    )
    certificate_id = Column(
        UUID(as_uuid=True), ForeignKey("vpn_certificates.id", ondelete="RESTRICT")
    )
    request_key = Column(UUID(as_uuid=True), nullable=False)
    csr_pem = Column(Text, nullable=False)
    csr_sha256 = Column(String(64), nullable=False)
    challenge = Column(Text, nullable=False)
    state = Column(String(12), nullable=False, default="challenge")
    public_bundle = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    challenge_expires_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    maintenance_until = Column(DateTime(timezone=True))
    confirmed_at = Column(DateTime(timezone=True))
    error_code = Column(String(40))
    __table_args__ = (
        CheckConstraint(
            "state IN ('challenge','queued','ready','active','confirmed','cancelled','failed')",
            name="certificate_renewal_state",
        ),
        Index("uq_certificate_renewal_request", "gateway_id", "request_key", unique=True),
        Index(
            "uq_certificate_renewal_pending",
            "gateway_id",
            unique=True,
            postgresql_where=text("state IN ('challenge','queued','ready','active')"),
        ),
    )
