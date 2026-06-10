"""ORM-модели для ручной проверки."""

import uuid
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from core.database import Base


class ManualReviewQueue(Base):
    __tablename__ = "manual_review_queue"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id      = Column(UUID(as_uuid=True), ForeignKey("claims.id"), nullable=False)
    tenant_id     = Column(UUID(as_uuid=True), nullable=False)
    priority      = Column(String(20), default="normal")  # urgent | high | normal
    reason        = Column(String(100), nullable=False)
    operator_note = Column(Text)
    assigned_to   = Column(UUID(as_uuid=True))
    resolved_at   = Column(DateTime(timezone=True))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    claim = relationship("Claim", back_populates="manual_review")


class ManualReviewOutcome(Base):
    __tablename__ = "manual_review_outcomes"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id           = Column(UUID(as_uuid=True), nullable=False)
    tenant_id          = Column(UUID(as_uuid=True), nullable=False)
    auto_decision      = Column(JSONB)   # что решила система
    expert_decision    = Column(JSONB)   # что решил эксперт
    discrepancy_reason = Column(Text)
    # Что исправил оператор: amount | diagnosis | coverage | none (Шаг 30, миграция 008)
    correction_type    = Column(String(30))
    # Почему Claude ошибся: ocr_quality | contract_gap | extraction_error | fraud_missed | correct
    claude_error_reason = Column(String(50))
    operator_id        = Column(UUID(as_uuid=True), nullable=False)
    reviewed_at        = Column(DateTime(timezone=True), server_default=func.now())
