"""
Gateway model
"""

import uuid
from api.time_utils import utc_now
from sqlalchemy import Column, String, DateTime, CheckConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base


class Gateway(Base):
    __tablename__ = "gateways"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id = Column(
        UUID(as_uuid=True), ForeignKey("access_groups.id", ondelete="RESTRICT"), index=True
    )
    mac_address = Column(String(17), nullable=True, index=True)
    display_name = Column(String(100), nullable=True)
    phone_number = Column(String(20), nullable=True, unique=True, index=True)
    activation_mode = Column(String(10), nullable=False, default="sms", server_default="sms")
    __table_args__ = (
        CheckConstraint(
            "activation_mode != 'wifi' OR mac_address IS NOT NULL", name="gateway_wifi_mac"
        ),
        CheckConstraint("activation_mode IN ('sms', 'wifi')", name="gateway_activation_mode"),
        CheckConstraint(
            "activation_mode != 'sms' OR phone_number IS NOT NULL", name="gateway_sms_phone"
        ),
    )
    serial_number = Column(String(100), unique=True, nullable=True)
    imei = Column(String(15), nullable=True, index=True)
    iccid = Column(String(22), nullable=True, index=True)
    vpn_ip = Column(String(15), unique=True, nullable=True)
    status = Column(String(20), default="PENDING")  # PENDING, ACTIVE, INACTIVE
    last_seen = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    extra_metadata = Column("metadata", JSONB, default=dict)
