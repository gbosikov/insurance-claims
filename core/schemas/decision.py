"""Pydantic-схемы для решений Decision Engine."""

from __future__ import annotations
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field


class DiagnosisDecisionSchema(BaseModel):
    icd10_code:          str
    is_covered:          bool
    approved_amount:     float
    rejection_reason:    str | None = None
    contract_reference:  str | None = None
    coverage_clarity:    float = Field(ge=0.0, le=1.0, default=0.5)
    # coverage_clarity: насколько однозначно договор покрывает/исключает этот диагноз.
    # 1.0 = прямое упоминание в договоре; 0.4 = интерпретация по категории; 0.1 = договор молчит.


class LineItemDecisionSchema(BaseModel):
    description:     str
    claimed_amount:  float
    approved_amount: float
    linked_icd10:    str | None = None
    # Услуга найдена в POSITIVE LIST договора → покрыта 100% независимо от диагноза
    positive_list_applied: bool = False


class ClaimDecision(BaseModel):
    """Полное решение по заявке — выход Decision Engine."""
    claim_id:               UUID
    diagnoses:              list[DiagnosisDecisionSchema]
    line_items:             list[LineItemDecisionSchema] = Field(default_factory=list)
    total_approved:         float
    deductible_applied:     float
    final_payout:           float
    status:                 Literal["approved", "partial", "rejected", "manual_review"]
    requires_manual_review: bool
    manual_review_reason:   str | None = None
    fraud_flags:            list[str] = Field(default_factory=list)
    overall_confidence:     float = Field(ge=0.0, le=1.0)
    # Разбивка по трём сигналам: data_score, coverage_signal, amount_gate + итоговый routing_signal
    signal_breakdown:       dict = Field(default_factory=dict)
    summary:                str = ""   # полный вердикт → идёт в Comment ClaimParsing_UNI
    rag_chunks_used:        list[str] = Field(default_factory=list)
    prompt_version:         str = ""
    model_version:          str = ""

    # ── Поля для ClaimParsing_UNI ─────────────────────────────────
    # Заполняются в Decision Engine после маппинга на справочники кор-системы
    diagnosid:   str | None = None   # ICD10 код для DiagnosID в ClaimParsing_UNI (например "I10")
    pers_id:     int | None = None   # код провайдера (TODO: нужен справочник провайдеров)
    config_kind: int | None = None   # вид направления из рисков
    # [{RiskID, FinalAmount, ServDate, serviceid, ServName}]
    risks_list:  list[dict] = Field(default_factory=list)
