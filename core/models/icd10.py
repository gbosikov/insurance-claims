"""ORM-модель: локальный справочник диагнозов МКБ-10."""

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import relationship

from core.database import Base


class ICD10Diagnosis(Base):
    __tablename__ = "icd10_diagnoses"

    id           = Column(Integer, primary_key=True)   # оригинальный ID из справочника
    pid          = Column(Integer, index=True)          # родительский ID (иерархия)
    extcod       = Column(String(20), index=True)       # код МКБ-10: J06.9
    name_r       = Column(Text)                         # русское название
    name_g       = Column(Text)                         # грузинское название
    name_e       = Column(Text)                         # английское название
    is_available = Column(Boolean, nullable=False, default=True)
    updated_at   = Column(DateTime(timezone=True), server_default=func.now())

    def name(self, lang: str = "r") -> str | None:
        """Вернуть название на нужном языке (r=ru, g=ka, e=en)."""
        return getattr(self, f"name_{lang}", None) or self.name_r or self.name_e

    def __repr__(self) -> str:
        return f"<ICD10 {self.extcod} id={self.id}>"
