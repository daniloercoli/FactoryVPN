"""
Session model
"""

import uuid
from api.time_utils import utc_now
from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Index, text, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from api.db.session import Base


class Session(Base):
    __tablename__ = "sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Historical scope survives gateway moves and group deletion.
    group_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # A missing SSO session must fail closed, including after historical data removal.
    sso_session_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # Keep missing references: deleting a browser session must fail closed.
    web_session_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    technician_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gateway_id = Column(
        UUID(as_uuid=True),
        ForeignKey("gateways.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(
        String(20), default="REQUESTED", index=True
    )  # REQUESTED, ACTIVE, CLOSED, EXPIRED, FAILED
    technician_vpn_ip = Column(String(15), nullable=True)
    gateway_vpn_ip = Column(String(15), nullable=True)
    lan_cidr = Column(String(18), nullable=True)
    requested_at = Column(DateTime(timezone=True), default=utc_now)
    activation_mode = Column(String(10), nullable=False, default="sms", server_default="sms")
    activation_received_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("activation_mode IN ('sms', 'wifi')", name="session_activation_mode"),
    )
    sms_sent_at = Column(DateTime(timezone=True), nullable=True)
    sms_last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    gateway_connected_at = Column(DateTime(timezone=True), nullable=True)
    support_started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    result = Column(String(500), nullable=True)
    error_reason = Column(String(500), nullable=True)

    network_slot = Column(Integer, nullable=True)
    gateway_ready_at = Column(DateTime(timezone=True), nullable=True)
    network_ready_at = Column(DateTime(timezone=True), nullable=True)
    worker_seen_at = Column(DateTime(timezone=True), nullable=True)
    cleaned_at = Column(DateTime(timezone=True), nullable=True)
    diagnostics_started_at = Column(DateTime(timezone=True), nullable=True)
    diagnostic_state = Column(JSONB, nullable=False, default=dict, server_default="{}")

    # Relationships
    technician = relationship("User", backref="sessions")
    gateway = relationship("Gateway", backref="sessions")


# A terminal session reserves its resources until cleanup is acknowledged.
_occupied = text(
    "status IN ('REQUESTED', 'ACTIVE') OR (network_slot IS NOT NULL AND cleaned_at IS NULL)"
)
Index("uq_sessions_technician_live", Session.technician_id, unique=True, postgresql_where=_occupied)
Index("uq_sessions_gateway_live", Session.gateway_id, unique=True, postgresql_where=_occupied)
Index(
    "uq_sessions_slot_live",
    Session.network_slot,
    unique=True,
    postgresql_where=text("network_slot IS NOT NULL AND cleaned_at IS NULL"),
)
