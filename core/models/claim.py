"""ORM-модели для заявок и связанных сущностей."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    UUID, Boolean, Column, Date, DateTime, Enum, ForeignKey,
    Integer, Numeric, String, Text, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import relationship

from core.database import Base

import enum


class ClaimStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PREPROCESSING = "PREPROCESSING"
    OCR_PROCESSING = "OCR_PROCESSING"
    EXTRACTING = "EXTRACTING"
    IDENTITY_CHECK = "IDENTITY_CHECK"
    RAG_SEARCH = "RAG_SEARCH"
    DECISION_PENDING = "DECISION_PENDING"
    AUTO_APPROVED = "AUTO_APPROVED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DOCS_REQUESTED = "DOCS_REQUESTED"
    FRAUD_FLAG = "FRAUD_FLAG"
    REJECTED = "REJECTED"
    PAID = "PAID"


class DocType(str, enum.Enum):
    FORM_100 = "form_100"
    ID_DOCUMENT = "id_document"
    RECEIPT = "receipt"


class Claim(Base):
    __tablename__ = "claims"

    id                  = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id           = Column(UUID(as_uuid=True), nullable=False, index=True)
    policy_number       = Column(String(50))
    personal_id_number  = Column(String(30))
    status              = Column(
        Enum(ClaimStatus, name="claim_status"),
        nullable=False,
        default=ClaimStatus.RECEIVED,
    )
    submission_date     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    event_date          = Column(Date)
    total_claimed       = Column(Numeric(10, 2))
    total_approved      = Column(Numeric(10, 2))
    deductible_applied  = Column(Numeric(10, 2))
    final_payout        = Column(Numeric(10, 2))
    decision_type       = Column(String(30))   # auto_approved | manual | rejected | fraud_flag
    overall_confidence  = Column(Numeric(4, 3))
    routing_reason      = Column(Text)
    client_reference    = Column(String(100))
    processed_at        = Column(DateTime(timezone=True))
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    documents           = relationship("ClaimDocument", back_populates="claim", cascade="all, delete-orphan")
    diagnosis_decisions = relationship("DiagnosisDecision", back_populates="claim", cascade="all, delete-orphan")
    line_items          = relationship("LineItemDecision", back_populates="claim", cascade="all, delete-orphan")
    audit_entries       = relationship("AuditLog", back_populates="claim")
    manual_review       = relationship("ManualReviewQueue", back_populates="claim")
    appeals             = relationship("Appeal", back_populates="claim")


class ClaimDocument(Base):
    __tablename__ = "claim_documents"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id           = Column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    tenant_id          = Column(UUID(as_uuid=True), nullable=False)
    doc_type           = Column(Enum(DocType, name="doc_type"), nullable=False)
    # filename_hint | ocr_rules | operator
    doc_type_source    = Column(String(30), nullable=False, default="filename_hint")
    # TRUE когда тип верифицирован (auto_approved или оператором) → годится для обучения
    doc_type_confirmed = Column(Boolean, nullable=False, default=False)
    storage_path       = Column(Text, nullable=False)
    preprocessed_path  = Column(Text)
    ocr_text           = Column(Text)
    ocr_confidence     = Column(Numeric(4, 3))
    extracted_data     = Column(JSONB)
    quality_score      = Column(Numeric(4, 3))
    quality_flags      = Column(ARRAY(Text))
    created_at         = Column(DateTime(timezone=True), server_default=func.now())

    claim = relationship("Claim", back_populates="documents")


class DiagnosisDecision(Base):
    __tablename__ = "diagnosis_decisions"

    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id           = Column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    tenant_id          = Column(UUID(as_uuid=True), nullable=False)
    icd10_code         = Column(String(20))
    description        = Column(Text)
    is_covered         = Column(Boolean)
    approved_amount    = Column(Numeric(10, 2))
    rejection_reason   = Column(Text)
    contract_reference = Column(Text)
    confidence         = Column(Numeric(4, 3))
    created_at         = Column(DateTime(timezone=True), server_default=func.now())

    claim = relationship("Claim", back_populates="diagnosis_decisions")


class LineItemDecision(Base):
    __tablename__ = "line_item_decisions"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_id        = Column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    tenant_id       = Column(UUID(as_uuid=True), nullable=False)
    description     = Column(Text)
    claimed_amount  = Column(Numeric(10, 2))
    approved_amount = Column(Numeric(10, 2))
    linked_icd10    = Column(String(20))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    claim = relationship("Claim", back_populates="line_items")
