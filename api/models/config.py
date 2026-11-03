"""
Config model (key-value settings)
"""

from api.time_utils import utc_now
from sqlalchemy import Column, String, Text, DateTime
from api.db.session import Base


class Config(Base):
    __tablename__ = "config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
