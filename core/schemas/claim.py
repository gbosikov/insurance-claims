"""Pydantic-схемы для заявок и извлечённых данных."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ── Данные застрахованного ────────────────────────────────────────

class InsuredData(BaseModel):
    full_name:     str
    birth_date:    str                  # ISO 8601: YYYY-MM-DD
    personal_id:   str                  # личный номер
    policy_number: str | None = None


# ── Данные страхового случая ──────────────────────────────────────

class DiagnoisItem(BaseModel):
    icd10_code:  str
    description: str


class LineItem(BaseModel):
    description: str
    amount:      float


class EventData(BaseModel):
    date:          str          # ISO 8601: YYYY-MM-DD
    institution:   str | None = None
    diagnoses:     list[DiagnoisItem] = Field(default_factory=list)
    line_items:    list[LineItem] = Field(default_factory=list)
    total_claimed: float


# ── Результат извлечения (выход Слоя 4) ───────────────────────────

class ExtractionResult(BaseModel):
    insured:                InsuredData
    event:                  EventData
    extraction_confidence:  float = Field(ge=0.0, le=1.0)
    flags:                  list[str] = Field(default_factory=list)
    # low_confidence_name | missing_date | amount_mismatch | cross_validation_failed


# ── API-схемы заявки ──────────────────────────────────────────────

class DocumentRef(BaseModel):
    url:      str   # pre-signed URL файла во внешней системе
    filename: str   # оригинальное имя файла (сохраняется как есть)


class ClaimCreateRequest(BaseModel):
    policy_number:    str                        # номер медицинской карточки — обязательный
    client_reference: str | None = None          # внешний ID клиента (опционально)
    documents:        list[DocumentRef]          # ссылки на файлы во внешней системе


class ClaimResponse(BaseModel):
    claim_id:               UUID
    status:                 str
    estimated_completion_sec: int = 300

    model_config = {"from_attributes": True}


class ClaimStatusResponse(BaseModel):
    claim_id:           UUID
    status:             str
    submission_date:    datetime
    event_date:         date | None = None
    total_claimed:      float | None = None
    total_approved:     float | None = None
    final_payout:       float | None = None
    decision_type:      str | None = None
    overall_confidence: float | None = None
    routing_reason:     str | None = None
    processed_at:       datetime | None = None

    model_config = {"from_attributes": True}
