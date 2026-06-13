"""SQLAlchemy-модель таблицы exclusion_rules."""
from __future__ import annotations

import uuid

from sqlalchemy import ARRAY, TIMESTAMP, Column, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase

from core.database import Base


class ExclusionRule(Base):
    __tablename__ = "exclusion_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    # 'all' — для всех застрахованных; 'family' — только члены семьи (CardNumber /2-/4)
    scope = Column(String(10), nullable=False, default="all")
    description = Column(Text, nullable=False)
    # Объединённые коды и диапазоны (колонки 2 + 3 вординга): "N18", "F00-F99" и т.п.
    icd10_codes = Column(ARRAY(String), nullable=False, default=list)
    # Условия при которых исключение НЕ действует: ['urgent', 'diagnostic', 'first_test']
    carveout_conditions = Column(ARRAY(String), nullable=False, default=list)
    source_row = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True))
