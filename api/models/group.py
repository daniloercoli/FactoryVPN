"""Access groups and explicit user membership, independent of authentication."""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from api.db.session import Base
from api.time_utils import utc_now


class Group(Base):
    __tablename__ = "access_groups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class UserGroup(Base):
    __tablename__ = "user_groups"

    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id = Column(
        UUID(as_uuid=True),
        ForeignKey("access_groups.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
