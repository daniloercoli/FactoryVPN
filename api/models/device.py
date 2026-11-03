"""
Device model
"""

import uuid
from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from api.db.session import Base


class Device(Base):
    __tablename__ = "devices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gateway_id = Column(UUID(as_uuid=True), ForeignKey("gateways.id"), nullable=False, index=True)
    name = Column(String(100), nullable=True)
    ip_address = Column(String(15), nullable=True)
    mac_address = Column(String(17), nullable=True)
    device_type = Column(String(50), default="OTHER")  # PLC, HMI, SERVER, OTHER
    status = Column(String(20), default="ACTIVE")
    last_seen = Column(DateTime(timezone=True), nullable=True)
    extra_metadata = Column("metadata", JSONB, default=dict)
