"""ORM-модель апелляций."""

import uuid
from sqlalchemy import Column, DateTime, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship

from core.database import Base


class Appeal(Base):
    __tablename__ = "appeals"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id        = Column(UUID(as_uuid=True), nullable=False)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    status          = Column(String(30), nullable=False, default="RECEIVED")  # RECEIVED | IN_REVIEW | RESOLVED
    client_reason   = Column(Text, nullable=False)
    additional_docs = Column(ARRAY(Text))
    submitted_at    = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deadline_at     = Column(DateTime(timezone=True))
    reviewed_by     = Column(UUID(as_uuid=True))
    expert_reasoning= Column(Text)
    outcome         = Column(String(20))   # upheld | overturned | partial
    revised_payout  = Column(Numeric(10, 2))
    resolved_at     = Column(DateTime(timezone=True))

    claim = relationship("Claim", back_populates="appeals",
                         foreign_keys=[claim_id],
                         primaryjoin="Appeal.claim_id==Claim.id")
