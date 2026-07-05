"""ORM-модель пользователя веб-портала (platform.users).

Отдельна от platform.api_keys (machine-to-machine).
Аутентификация: email + bcrypt → JWT Bearer (core/portal_auth.py).
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

from core.database import Base


class PortalUser(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "platform"}

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id     = Column(UUID(as_uuid=True), ForeignKey("platform.tenants.id"), nullable=False)
    email         = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(200), nullable=False)
    full_name     = Column(String(200))
    # viewer | operator | admin
    role          = Column(String(20), nullable=False, default="viewer")
    is_active     = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
