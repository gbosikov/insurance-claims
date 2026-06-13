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
    # Источник строки: 'receipt_1', 'receipt_2', ..., 'form_100'.
    # Используется для аудита и ServName в ClaimParsing_UNI.
    # НЕ добавляется в description — ServName должен быть чистым.
    doc_source:  str | None = None


class EventData(BaseModel):
    date:             str          # ISO 8601: YYYY-MM-DD
    institution:      str | None = None
    diagnoses:        list[DiagnoisItem] = Field(default_factory=list)
    line_items:       list[LineItem] = Field(default_factory=list)
    total_claimed:    float
    service_urgency:  str | None = None  # "urgent" | "diagnostic" | "planned" | None
    # urgent = სასწრაფო/გადაუდებელი (ЧС, неотложное)
    # diagnostic = პირველადი დიაგნოსტიკა (первичная диагностика, скрининг)
    # planned = გეგმიური (плановое лечение, профилактика)
    # None = не указано врачом → требуется heuristic-определение


# ── Кросс-документные данные (Шаг 25) ─────────────────────────────
# Значения, как они видны в каждом документе по отдельности.
# Заполняются Claude только из явно присутствующего текста —
# нужны для кросс-проверки согласованности между документами.

class CrossDocForm100(BaseModel):
    full_name:   str | None = None
    birth_date:  str | None = None          # YYYY-MM-DD
    date:        str | None = None          # дата события, YYYY-MM-DD
    institution: str | None = None
    diagnoses:   list[str] = Field(default_factory=list)  # коды МКБ-10
    # Услуги/процедуры, явно перечисленные в форме 100 (без сумм).
    # Используются для кросс-проверки с чеками.
    services:    list[str] = Field(default_factory=list)
    total:       float | None = None


class CrossDocIdDocument(BaseModel):
    full_name:   str | None = None
    birth_date:  str | None = None
    personal_id: str | None = None


class CrossDocReceiptLineItem(BaseModel):
    """Строка услуги из одного конкретного чека."""
    description:    str
    amount:         float
    receipt_number: int | None = None  # номер чека в пакете (1, 2, 3…)


class CrossDocReceipt(BaseModel):
    date:        str | None = None
    institution: str | None = None
    diagnoses:   list[str] = Field(default_factory=list)
    # Детализированный список услуг из всех чеков с номером чека.
    # Используется для сверки с form_100.services.
    line_items:  list[CrossDocReceiptLineItem] = Field(default_factory=list)
    total:       float | None = None


class CrossDocumentData(BaseModel):
    form_100:    CrossDocForm100 | None = None
    id_document: CrossDocIdDocument | None = None
    receipt:     CrossDocReceipt | None = None


# ── Результат извлечения (выход Слоя 4) ───────────────────────────

class ExtractionResult(BaseModel):
    insured:                InsuredData
    event:                  EventData
    extraction_confidence:  float = Field(ge=0.0, le=1.0)
    flags:                  list[str] = Field(default_factory=list)
    # low_confidence_name | missing_date | amount_mismatch | cross_validation_failed |
    # name_mismatch | birth_date_mismatch | diagnosis_mismatch | date_mismatch | institution_mismatch
    cross_document:         CrossDocumentData | None = None


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
    # validation_alias="id" — читает obj.id из SQLAlchemy-модели,
    # но сериализует как "claim_id" в JSON-ответе.
    claim_id:           UUID = Field(validation_alias="id")
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

    model_config = {"from_attributes": True, "populate_by_name": True}
