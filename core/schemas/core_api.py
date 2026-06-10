"""Pydantic-схемы для ответов кор-системы Lite GROUP."""

from __future__ import annotations
from datetime import date
from pydantic import BaseModel, Field


# ── Новые схемы (архитектура LiteGroup) ───────────────────────────

class RiskInfo(BaseModel):
    """Один риск из полиса (getpolicylist → RiskList.Risk)."""
    risk_id:         int                  # RiskId
    name:            str                  # RiskName
    coverage_pct:    float = 100.0        # LinitPercent (опечатка в API кор-системы)
    total_limit:     float = 0.0          # LimitAmount
    remaining_limit: float = 0.0          # LimitAmountLeft
    currency:        str = "GEL"          # AmountCurrency типа страхования
    # Суб-лимит (Шаг 23): риск с собственным денежным лимитом (LimitAmount > 0).
    # None = у риска нет отдельного лимита — check_sublimits() его пропускает.
    sublimit:        float | None = None
    # Иерархия рисков: RiskParentId (дочерние делят лимит родителя). None = корневой.
    parent_risk_id:  int | None = None
    # Количественные лимиты (например, 2 профилактических осмотра в год)
    limit_count:      int | None = None   # LimitCount ("" = нет)
    limit_count_left: int | None = None   # LimitCountLeft
    # Справочник услуг, привязанных к риску (для serviceid и ConfigKind)
    services: list[dict] = Field(default_factory=list)
    # [{serviceid: str, name: str, config_kind: int}]


class RisksAndLimits(BaseModel):
    """Риски, % покрытия, лимиты по номеру медкарточки."""
    policy_number: str
    risks:         list[RiskInfo] = Field(default_factory=list)
    annual_limit:  float = 0.0    # InsuranceType.Amount (страховая сумма)
    remaining:     float = 0.0
    currency:      str = "GEL"
    # Действие полиса (Шаг 23 + проверка активности на дату события).
    # None = кор-система не предоставила — проверки пропускаются с audit-заметкой.
    policy_start_date: date | None = None
    policy_end_date:   date | None = None
    # ObjectList.Objects.ObjectData — свободный текст кор-системы; может содержать
    # маркер освобождения от периода ожидания ("არ ეკუთვნის მოცდის პერიოდი")
    object_data: str | None = None


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


class ProviderInfo(BaseModel):
    """Провайдер (медицинское учреждение) из справочника кор-системы."""
    pers_id: int           # числовой ID провайдера (PersID для ClaimParsing_UNI)
    name:    str           # название учреждения
    inn:     str | None = None  # ИНН провайдера (не всегда доступен)


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
