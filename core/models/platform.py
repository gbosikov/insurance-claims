"""ORM-модели платформы (схема platform): тенанты и API-ключи.

Таблицы созданы миграцией 001_initial.sql — модели только маппят их
для аутентификации (core/auth.py) и CLI генерации ключей.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from core.database import Base


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = {"schema": "platform"}

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug       = Column(String(50), unique=True, nullable=False)
    name       = Column(String(200), nullable=False)
    plan       = Column(String(20), nullable=False, default="starter")
    status     = Column(String(20), nullable=False, default="active")  # active | suspended
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = {"schema": "platform"}

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id      = Column(UUID(as_uuid=True), ForeignKey("platform.tenants.id"), nullable=False)
    key_hash       = Column(String(64), unique=True, nullable=False)  # SHA-256 hex, сам ключ не хранится
    name           = Column(String(100))
    environment    = Column(String(20), default="production")  # production | test
    scopes         = Column(ARRAY(Text), default=["claims:write", "claims:read"])
    rate_limit_rpm = Column(Integer, default=60)
    last_used_at   = Column(DateTime(timezone=True))
    expires_at     = Column(DateTime(timezone=True))
    revoked_at     = Column(DateTime(timezone=True))
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
