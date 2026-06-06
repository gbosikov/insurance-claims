"""Pydantic-схемы для контрактов и RAG-чанков."""

from __future__ import annotations
from datetime import date
from uuid import UUID
from pydantic import BaseModel


class ContractChunkSchema(BaseModel):
    id:           UUID
    policy_number: str
    version_id:   str
    section_type: str | None = None
    title:        str | None = None
    content:      str
    key_terms:    list[str] = []

    model_config = {"from_attributes": True}


class ContractVersionSchema(BaseModel):
    id:           UUID
    policy_number: str
    version_id:   str
    content_hash: str | None = None
    valid_from:   date
    valid_to:     date | None = None
    pdf_path:     str

    model_config = {"from_attributes": True}
