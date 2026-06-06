"""ORM-модель счётчиков частоты заявок (антифрод)."""

from sqlalchemy import Column, Date, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID

from core.database import Base


class ClaimFrequency(Base):
    __tablename__ = "claim_frequency"

    tenant_id    = Column(UUID(as_uuid=True), nullable=False, primary_key=True)
    personal_id  = Column(String(30), nullable=False, primary_key=True)
    period_start = Column(Date, nullable=False, primary_key=True)  # начало 30-дневного окна
    claim_count  = Column(Integer, default=0)
    updated_at   = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
