"""
User model
"""

import uuid
from api.time_utils import utc_now
from sqlalchemy import Column, String, Boolean, DateTime, Integer, CheckConstraint, text
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from api.db.session import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True)
    auth_mode = Column(String(10), nullable=False, default="local", server_default="local")
    email = Column(String(255), unique=True, nullable=True)
    email_verified_at = Column(DateTime(timezone=True))
    email_two_step = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    email_last_sent_at = Column(DateTime(timezone=True))
    email_window_at = Column(DateTime(timezone=True))
    email_send_count = Column(Integer, nullable=False, default=0, server_default="0")
    email_failures = Column(Integer, nullable=False, default=0, server_default="0")
    email_locked_until = Column(DateTime(timezone=True))
    full_name = Column(String(200), nullable=True)
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    permissions = Column(
        ARRAY(String(40)),
        nullable=False,
        default=lambda: ["connect"],
        server_default=text("ARRAY['connect']::varchar[]"),
    )
    token_version = Column(Integer, nullable=False, default=1, server_default="1")
    __table_args__ = (
        CheckConstraint(
            "(auth_mode = 'local' AND password_hash IS NOT NULL) OR "
            "(auth_mode = 'sso' AND password_hash IS NULL)",
            name="user_authentication_mode",
        ),
        CheckConstraint("token_version > 0", name="user_token_version_positive"),
        CheckConstraint(
            "permissions <@ ARRAY['connect','users.manage','groups.manage',"
            "'gateways.manage','gateways.provision','sessions.manage','audit.read','telemetry.read','logs.read']::varchar[]",
            name="user_permissions_valid",
        ),
    )
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    vpn_ip = Column(String(15), unique=True, nullable=True)
    last_login = Column(DateTime(timezone=True), nullable=True)
