"""ORM-модель аудит-лога (append-only)."""

import uuid
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    claim_id        = Column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False, index=True)
    step            = Column(String(50), nullable=False)  # intake | preprocessing | ocr | extraction | rag_search | decision | routing
    timestamp       = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    input_data      = Column(JSONB)
    output_data     = Column(JSONB)
    confidence      = Column(JSONB)
    rag_chunks      = Column(ARRAY(Text))   # ID чанков использованных в RAG
    prompt_version  = Column(String(20))
    model_version   = Column(String(50))
    operator_id     = Column(UUID(as_uuid=True))
    override_reason = Column(Text)
    duration_ms     = Column(Integer)

    claim = relationship("Claim", back_populates="audit_entries",
                         foreign_keys=[claim_id],
                         primaryjoin="AuditLog.claim_id==Claim.id")

    # ВАЖНО: UPDATE и DELETE запрещены на уровне SQL (CREATE RULE в миграции).
    # Эта модель используется ТОЛЬКО для INSERT и SELECT.
