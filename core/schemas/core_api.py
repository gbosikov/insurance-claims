"""Pydantic-схемы для ответов кор-системы Lite GROUP."""

from __future__ import annotations
from datetime import date
from pydantic import BaseModel, Field


# ── Новые схемы (архитектура LiteGroup) ───────────────────────────

class RiskInfo(BaseModel):
    """Один риск из полиса."""
    risk_id:         int
    name:            str
    coverage_pct:    float = 100.0   # % покрытия (0–100)
    total_limit:     float = 0.0
    remaining_limit: float = 0.0
    currency:        str = "GEL"
    # Справочник услуг, привязанных к риску (для serviceid и ConfigKind)
    services: list[dict] = Field(default_factory=list)
    # [{serviceid: str, name: str, config_kind: int}]


class RisksAndLimits(BaseModel):
    """Риски, % покрытия, лимиты по номеру медкарточки."""
    policy_number: str
    risks:         list[RiskInfo] = Field(default_factory=list)
    annual_limit:  float = 0.0
    remaining:     float = 0.0
    currency:      str = "GEL"


class ContractData(BaseModel):
    """Генеральный договор, полученный из кор-системы."""
    policy_number: str
    content:       str            # текст договора (для RAG)
    content_hash:  str | None = None
    version:       str | None = None


class ICD10Item(BaseModel):
    """Строка справочника диагнозов ICD10."""
    diagnosid: int    # числовой ID в кор-системе
    code:      str    # ICD10-код (J06.9, Z00.0 и т.д.)
    name:      str    # название диагноза


class SubmitClaimResult(BaseModel):
    """Ответ ClaimParsing_UNI."""
    innum:       str   # номер направления в кор-системе
    status:      int   # 0 = успех, иначе код ошибки
    status_text: str


# ── Устаревшие схемы (оставлены для обратной совместимости) ────────

class PolicyInfo(BaseModel):
    policy_number:    str
    personal_id:      str
    full_name:        str
    status:           str          # active | suspended | expired
    start_date:       date
    end_date:         date
    contract_version: str | None = None


class PolicyLimits(BaseModel):
    policy_number:    str
    annual_limit:     float
    used_amount:      float
    remaining_amount: float
    currency:         str = "GEL"
    deductible:       float = 0.0
    as_of_date:       date


class ContractMeta(BaseModel):
    policy_number: str
    version:       str
    content_hash:  str | None = None
    updated_at:    date | None = None
    pdf_url:       str | None = None
