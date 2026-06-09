"""ORM-модели для контрактов и RAG-чанков."""

import uuid
from sqlalchemy import Column, Date, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from core.database import Base

try:
    from pgvector.sqlalchemy import Vector
    VECTOR_AVAILABLE = True
except ImportError:
    # Fallback для тестовой среды без pgvector
    from sqlalchemy import LargeBinary as Vector
    VECTOR_AVAILABLE = False


class ContractVersion(Base):
    __tablename__ = "contract_versions"

    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id     = Column(UUID(as_uuid=True), nullable=False)
    policy_number = Column(String(50), nullable=False)
    version_id    = Column(String(20), nullable=False)
    content_hash  = Column(String(64))   # SHA-256 для проверки актуальности
    valid_from    = Column(Date, nullable=False)
    valid_to      = Column(Date)          # NULL = текущая версия
    pdf_path      = Column(Text, nullable=False)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    chunks = relationship("ContractChunk", back_populates="version",
                          cascade="all, delete-orphan",
                          foreign_keys="ContractChunk.version_id",
                          primaryjoin="and_(ContractVersion.policy_number==ContractChunk.policy_number, "
                                      "ContractVersion.version_id==ContractChunk.version_id, "
                                      "ContractVersion.tenant_id==ContractChunk.tenant_id)")


class ContractChunk(Base):
    __tablename__ = "contract_chunks"

    id               = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id        = Column(UUID(as_uuid=True), nullable=False)
    policy_number    = Column(String(50), nullable=False)
    version_id       = Column(String(20), nullable=False)
    section_type     = Column(String(50))  # coverage_cases | exclusions | claim_conditions | limits | definitions | appeal_process | general | exclusion_with_carveout
    title            = Column(Text)
    content          = Column(Text, nullable=False)
    key_terms        = Column(ARRAY(Text))
    # multilingual-e5-large выдаёт 1024 измерения
    embedding        = Column(Vector(1024) if VECTOR_AVAILABLE else Text)
    # Структурированная информация для CARVEOUT-исключений:
    # {"type": "exclusion_with_carveout",
    #  "excluded_icd10": ["N18", "I10"],
    #  "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
    #  "general_exceptions": ["B15"]}
    chunk_structure  = Column(JSONB)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    version = relationship(
        "ContractVersion",
        back_populates="chunks",
        foreign_keys=[version_id],
        primaryjoin="and_(ContractVersion.policy_number==ContractChunk.policy_number, "
                    "ContractVersion.version_id==ContractChunk.version_id, "
                    "ContractVersion.tenant_id==ContractChunk.tenant_id)",
    )
