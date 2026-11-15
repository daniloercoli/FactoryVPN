"""Identità di fine linea, separata dai gateway operativi e dalle loro credenziali."""

from sqlalchemy import Column, String, Integer, DateTime, CheckConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from api.db.session import Base
from api.time_utils import utc_now


class GatewayEnrollment(Base):
    __tablename__ = "gateway_enrollments"

    # Nessuna FK al gateway: revoca e prenotazione sopravvivono alla cancellazione.
    id = Column(UUID(as_uuid=True), primary_key=True)
    station_id = Column(String(80), nullable=False)
    request_key = Column(UUID(as_uuid=True), nullable=False)
    request_hash = Column(String(64), nullable=False)
    serial_number = Column(String(100), unique=True)
    original_serial_number = Column(String(100), nullable=False)
    secret_hash = Column(String(64), unique=True)
    external_reference = Column(String(200))
    display_name = Column(String(100))
    activation_mode = Column(String(10))
    phone_number = Column(String(20))
    mac_address = Column(String(17), index=True)
    imei = Column(String(15), index=True)
    iccid = Column(String(22), index=True)
    group_id = Column(UUID(as_uuid=True), index=True)
    approved_by = Column(UUID(as_uuid=True))
    approved_at = Column(DateTime(timezone=True))
    presented_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    blocked_at = Column(DateTime(timezone=True))
    blocked_reason = Column(String(500))
    delivery_mode = Column(
        String(12), nullable=False, default="automatic", server_default="automatic"
    )
    configuration_version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (
        Index("uq_enrollment_station_request", "station_id", "request_key", unique=True),
        CheckConstraint("delivery_mode IN ('automatic','manual')", name="enrollment_delivery_mode"),
        CheckConstraint("configuration_version > 0", name="enrollment_configuration_version"),
        CheckConstraint(
            "activation_mode IS NULL OR activation_mode IN ('sms','wifi')",
            name="enrollment_activation_mode",
        ),
    )
