"""
Слой 7 — Decision Engine.

Два уровня принятия решений:
  Уровень 1: Детерминированные проверки (без AI)
  Уровень 2: Claude API — интерпретация договора

После решения:
  - Маппинг ICD10-кода → DiagnosID (из справочника кор-системы)
  - Построение risks_list для ClaimParsing_UNI
  - summary идёт в поле Comment ClaimParsing_UNI

ПРАВИЛО: при сомнении — ручная проверка, не отказ.
ClaimParsing_UNI вызывается ВСЕГДА (в tasks.py), не здесь.
"""

from __future__ import annotations

import json
import random
from datetime import date
from typing import Any
from uuid import UUID

import anthropic
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import PolicyLimitExhaustedError
from core.schemas.claim import ExtractionResult
from core.schemas.contract import ContractChunkSchema
from core.schemas.core_api import ICD10Item, ProviderInfo, RisksAndLimits
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema, LineItemDecisionSchema
from layers.decision.icd10_enricher import EnrichedDiagnosis, enrich_all

log = structlog.get_logger()
settings = get_settings()

PROMPT_VERSION = "decision/v3.0.0"  # categorical reasoning + ICD10 hierarchy

# ── Decision Tool ─────────────────────────────────────────────────

DECISION_TOOL: dict[str, Any] = {
    "name": "make_claim_decision",
    "description": "Принять решение по страховой заявке на основе данных и текста договора",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnoses": {
                "type": "array",
                "description": "Решения по каждому диагнозу",
                "items": {
                    "type": "object",
                    "properties": {
                        "icd10_code":         {"type": "string"},
                        "is_covered":         {"type": "boolean"},
                        "approved_amount":    {"type": "number"},
                        "rejection_reason":   {"type": ["string", "null"]},
                        "contract_reference": {"type": "string", "description": "Например: Статья 4.2, пункт 3"},
                        "confidence":         {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["icd10_code", "is_covered", "approved_amount", "confidence"]
                }
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description":     {"type": "string"},
                        "claimed_amount":  {"type": "number"},
                        "approved_amount": {"type": "number"},
                        "linked_icd10":    {"type": ["string", "null"]},
                    },
                    "required": ["description", "claimed_amount", "approved_amount"]
                }
            },
            "total_approved":           {"type": "number"},
            "deductible_applied":       {"type": "number"},
            "final_payout":             {"type": "number"},
            "requires_manual_review":   {"type": "boolean"},
            "manual_review_reason":     {"type": ["string", "null"]},
            "overall_confidence":       {"type": "number", "minimum": 0, "maximum": 1},
            "summary":                  {
                "type": "string",
                "description": (
                    "Полный вердикт на русском: решение + обоснование + уровень уверенности + флаги. "
                    "Этот текст попадёт в поле Comment в кор-системе и будет читать оператор."
                )
            },
        },
        "required": [
            "diagnoses", "total_approved", "deductible_applied",
            "final_payout", "requires_manual_review", "overall_confidence", "summary"
        ]
    }
}

DECISION_SYSTEM_PROMPT = """Ты — эксперт-андеррайтер по ДМС. У тебя есть медицинские знания и знание страхового права.

ВАЖНО: Страховые договоры описывают КАТЕГОРИИ случаев, а не конкретные коды МКБ-10.
Твоя задача — определить, попадает ли конкретный диагноз под описанную категорию.

═══════════════════════════════════════════════════════════════════════════════════

ПРАВИЛА ИНТЕРПРЕТАЦИИ:

1. БАЗОВОЕ ПРАВИЛО КАТЕГОРИЙ:
   Если договор покрывает "острые респираторные заболевания", а диагноз J06.9 —
   это ПОКРЫТЫЙ СЛУЧАЙ. Рассуждай: J06.9 ∈ [острые] ∩ [инфекции] ∩ [органы дыхания] ✓

2. ИСКЛЮЧЕНИЯ БЕЗ CARVEOUT (обычные):
   - Исключения имеют приоритет над покрытием
   - Проверяй раздел [exclusions] для каждого диагноза
   - Если диагноз в списке исключённых → ОТКАЗ

3. ИСКЛЮЧЕНИЯ С CARVEOUT (条件付き исключения):
   Некоторые исключения имеют УСЛОВИЯ ("გარდა"/EXCEPT/КРОМЕ).

   ПРИМЕР: "N18 (хроническая почечная) ИСКЛЮЧЕНА КРОМЕ ургентного вмешательства"
   - Если service_urgency=urgent → ПОКРЫТО (CARVEOUT применяется)
   - Если service_urgency=planned → ИСКЛЮЧЕНО
   - Если service_urgency=diagnostic → ИСКЛЮЧЕНО

   CARVEOUT-УСЛОВИЯ ищи в разделе [exclusions_with_carveout]:
   - type="service_urgency" → проверь поле service_urgency из заявки
   - type="diagnosis_exception" → исключение из исключения (гепатит A)

4. ГРАНИЧНЫЕ СЛУЧАИ И НЕОПРЕДЕЛЁННОСТЬ:
   - Если непонятно → requires_manual_review=true, НЕ отказ
   - Если service_urgency=null И есть CARVEOUT → manual_review (не можем применить условие)
   - При неуверенности → manual_review, не быстрое решение

5. ЗАПРЕЩЕНО:
   - Отказывать только потому что конкретный код МКБ-10 не упомянут в договоре
   - Игнорировать CARVEOUT-условия ("помню об исключении, но забыл о условии")
   - Применять CARVEOUT без проверки условия

═══════════════════════════════════════════════════════════════════════════════════

ПРОЦЕСС ДЛЯ КАЖДОГО ДИАГНОЗА:

a) Используй "Медицинская иерархия" — цепочка категорий для диагноза
b) Найди в разделах договора категорию, под которую подпадает диагноз
c) Проверь обычные исключения [exclusions] — явно исключён?
   → ДА: ОТКАЗ (если нет CARVEOUT-условия)
d) Проверь CARVEOUT-исключения [exclusions_with_carveout]
   → Если диагноз в excluded_icd10, проверь carveout_conditions:
      ✓ service_urgency совпадает? → ПОКРЫТО
      ✗ service_urgency не совпадает? → ИСКЛЮЧЕНО (CARVEOUT не применяется)
      ? service_urgency неизвестен? → MANUAL_REVIEW
e) Вынеси решение с прямой цитатой из договора (contract_reference)

═══════════════════════════════════════════════════════════════════════════════════

ФИНАНСЫ: рассчитывай сумму с учётом coverage_pct и остатка remaining
SUMMARY: решение + обоснование + уверенность + флаги (текст для оператора)
ФОРМАТ: СТРОГО JSON-инструмент — никакого свободного текста"""


# ── Уровень 1: Детерминированные проверки ─────────────────────────

def check_remaining_limit(risks_limits: RisksAndLimits) -> None:
    """Общий остаток должен быть > 0."""
    if risks_limits.remaining <= 0:
        from core.exceptions import PolicyLimitExhaustedError
        raise PolicyLimitExhaustedError(
            risks_limits.policy_number,
            risks_limits.remaining,
            risks_limits.currency,
        )


def check_claim_filed_in_time(submission_date: date, event_date: date, max_days: int = 90) -> bool:
    """Заявка должна быть подана не позже N дней после события."""
    delta = (submission_date - event_date).days
    return 0 <= delta <= max_days


# ── Промпт Decision Engine ─────────────────────────────────────────

def build_decision_prompt(
    extraction: ExtractionResult,
    enriched: dict[str, "EnrichedDiagnosis"],
    risks_limits: RisksAndLimits,
    chunks: list[ContractChunkSchema],
) -> str:
    """Собирает промпт: данные заявки + иерархия МКБ-10 + риски/лимиты + чанки договора.

    ВАЖНО: выделяет CARVEOUT-исключения отдельно с их условиями.
    """

    claim_data = {
        "insured": {
            "full_name":     extraction.insured.full_name,
            "birth_date":    extraction.insured.birth_date,
            "personal_id":   extraction.insured.personal_id,
            "policy_number": extraction.insured.policy_number,
        },
        "event": {
            "date":          extraction.event.date,
            "institution":   extraction.event.institution,
            "service_urgency": extraction.event.service_urgency,  # ← ДОБАВЛЕНО для CARVEOUT-проверки
            "diagnoses":     [
                {"icd10_code": d.icd10_code, "doctor_description": d.description}
                for d in extraction.event.diagnoses
            ],
            "line_items":    [{"description": li.description, "amount": li.amount} for li in extraction.event.line_items],
            "total_claimed": extraction.event.total_claimed,
        },
        "extraction_confidence": extraction.extraction_confidence,
        "flags": extraction.flags,
    }

    # Медицинская иерархия: каждый диагноз → цепочка категорий на русском
    hierarchy_lines = []
    for d in extraction.event.diagnoses:
        e = enriched.get(d.icd10_code)
        if e and e.name_r:
            hierarchy_lines.append(
                f"  {d.icd10_code}: {e.category_chain_ru}"
            )
        else:
            hierarchy_lines.append(f"  {d.icd10_code}: (не найден в справочнике МКБ-10)")
    hierarchy_text = "\n".join(hierarchy_lines) if hierarchy_lines else "  (диагнозы не определены)"

    risks_data = {
        "annual_limit": risks_limits.annual_limit,
        "remaining":    risks_limits.remaining,
        "currency":     risks_limits.currency,
        "risks": [
            {
                "risk_id":      r.risk_id,
                "name":         r.name,
                "coverage_pct": r.coverage_pct,
                "remaining":    r.remaining_limit,
            }
            for r in risks_limits.risks
        ],
    }

    # Разделяем чанки: CARVEOUT-исключения отдельно, остальные обычным порядком
    carveout_chunks = [c for c in chunks if c.section_type == "exclusion_with_carveout"]
    other_chunks = [c for c in chunks if c.section_type != "exclusion_with_carveout"]

    # Сортируем остальные: обычные исключения первыми
    SECTION_ORDER = {"exclusions": 0, "coverage_cases": 1, "limits": 2, "claim_conditions": 3}
    other_chunks = sorted(other_chunks, key=lambda c: SECTION_ORDER.get(c.section_type or "", 9))

    # Формируем текст для CARVEOUT-исключений с их структурой
    carveout_text = ""
    if carveout_chunks:
        carveout_lines = []
        for chunk in carveout_chunks:
            carveout_lines.append(f"[exclusions_with_carveout] {chunk.title or ''}")
            carveout_lines.append(f"Содержание: {chunk.content}")

            # Если есть chunk_structure, покажи его Claude
            if chunk.chunk_structure:
                struct = chunk.chunk_structure
                conditions_str = ", ".join(
                    f"{c.get('type')}={c.get('value')}"
                    for c in struct.get('carveout_conditions', [])
                )
                exceptions_str = ", ".join(struct.get('general_exceptions', []))
                carveout_lines.append(
                    f"Структура: исключены={struct.get('excluded_icd10', [])}, "
                    f"условия=[{conditions_str}], исключения_из_исключений=[{exceptions_str}]"
                )
            carveout_lines.append("")
        carveout_text = "\n".join(carveout_lines)

    # Формируем текст для обычных чанков
    other_text = "\n\n".join(
        f"[{chunk.section_type or 'general'}] {chunk.title or ''}\n{chunk.content}"
        for chunk in other_chunks
    )

    sections = [f"""## Данные заявки
{json.dumps(claim_data, ensure_ascii=False, indent=2)}

## Медицинская иерархия диагнозов (МКБ-10)
Используй эти категории чтобы найти соответствующий раздел в договоре:
{hierarchy_text}

## Риски и лимиты (актуальные данные из кор-системы)
{json.dumps(risks_data, ensure_ascii=False, indent=2)}"""]

    if carveout_text:
        sections.append(f"""## CARVEOUT-исключения (исключения с УСЛОВИЯМИ)
⚠️  ВАЖНО: Проверь условие перед отказом!
{carveout_text}""")

    if other_text:
        sections.append(f"""## Остальные пункты договора
{other_text}""")
    else:
        sections.append("## Остальные пункты договора\n(не найдены)")

    return "\n\n".join(sections)


# ── CARVEOUT Exclusion Logic ─────────────────────────────────────────

def apply_carveout_exclusion_logic(
    icd10_code: str,
    service_urgency: str | None,
    carveout_chunks: list[ContractChunkSchema],
) -> tuple[bool, str | None]:
    """
    Применить CARVEOUT-исключения без Claude (детерминированно).

    Возвращает:
      (should_reject, rejection_reason)

    Логика:
      - Если диагноз в excluded_icd10 и service_urgency не совпадает условиям
        → (True, reason) — быстрый отказ
      - Если диагноз в general_exceptions → (False, None) — НЕ отказывать
      - Если диагноз не в исключённых или условие совпадает → (False, None) — пусть Claude решает
    """
    for chunk in carveout_chunks:
        if not chunk.chunk_structure:
            continue

        struct = chunk.chunk_structure
        excluded_icd10s = struct.get("excluded_icd10", [])
        general_exceptions = struct.get("general_exceptions", [])

        # Проверяем: входит ли диагноз в excluded_icd10?
        is_excluded = any(
            icd10_code.upper().startswith(code.upper())
            for code in excluded_icd10s
        )

        if not is_excluded:
            continue

        # Диагноз в списке исключённых. Проверяем: есть ли в general_exceptions?
        # (гепатит А не исключён, даже если в "гепатиты")
        is_general_exception = any(
            icd10_code.upper().startswith(code.upper())
            for code in general_exceptions
        )

        if is_general_exception:
            # Это исключение из исключения → НЕ отказываем
            return False, None

        # Диагноз исключён. Проверяем carveout_conditions.
        carveout_conditions = struct.get("carveout_conditions", [])

        if not carveout_conditions:
            # Нет условий → просто исключено
            return True, f"Исключение по договору (пункт {chunk.title or 'N/A'}): {chunk.content[:100]}..."

        # Есть условия. Проверяем: совпадает ли service_urgency?
        for condition in carveout_conditions:
            if condition.get("type") != "service_urgency":
                continue

            required_urgency = condition.get("value")  # "urgent" | "diagnostic" | "planned"

            if service_urgency is None:
                # service_urgency неизвестна, а исключение зависит от неё → manual_review позже
                # Не отказываем здесь, дождёмся should_require_manual_review_for_unknown_urgency
                return False, None

            if service_urgency == required_urgency:
                # Условие КАРВЕОУТА совпадает → диагноз НЕ исключён!
                return False, None

        # Условия есть, но service_urgency не совпадает → отказываем
        condition_text = "; ".join(
            f"{c.get('type')}={c.get('value')}" for c in carveout_conditions
        )
        return True, (
            f"Исключение по CARVEOUT-условиям договора: диагноз исключён, "
            f"но условия [{condition_text}] не совпадают с заявкой "
            f"(service_urgency={service_urgency})"
        )

    # Нет CARVEOUT-чанков, или диагноз не в исключённых → пусть Claude решает
    return False, None


# ── Маппинг на справочники кор-системы ───────────────────────────────

def find_diagnosid(icd10_code: str, icd10_list: list[ICD10Item]) -> int | None:
    """Найти DiagnosID по коду ICD10 (точное совпадение, без учёта регистра)."""
    code_upper = icd10_code.upper().strip()
    for item in icd10_list:
        if item.code.upper().strip() == code_upper:
            return item.diagnosid
    # Fallback: совпадение по префиксу (J06 совпадёт с J06.9)
    prefix = code_upper.split(".")[0]
    for item in icd10_list:
        if item.code.upper().startswith(prefix):
            return item.diagnosid
    return None


def build_risks_list(
    decision_line_items: list[LineItemDecisionSchema],
    risks_limits: RisksAndLimits,
    event_date: str,
) -> tuple[list[dict], int | None]:
    """
    Построить risks_list для ClaimParsing_UNI из одобренных позиций.
    Возвращает: (risks_list, config_kind).
    """
    risks_list = []
    config_kind: int | None = None

    # Берём первый риск с ненулевым остатком как основной
    primary_risk = next(
        (r for r in risks_limits.risks if r.remaining_limit > 0),
        risks_limits.risks[0] if risks_limits.risks else None,
    )

    if primary_risk:
        # config_kind из первого сервиса риска.
        # Значения: 1=направление, 2=акт возмещения, 3=гарантийное письмо.
        # В проекте используется 2 (акт возмещения).
        if primary_risk.services:
            config_kind = (
                primary_risk.services[0].get("config_kind")
                or primary_risk.services[0].get("ConfigKind")
            )
        # Fallback: акт возмещения (значение по умолчанию)
        if not config_kind:
            config_kind = 2

        for li in decision_line_items:
            if li.approved_amount <= 0:
                continue
            # serviceid из справочника услуг риска или fallback
            serviceid = ""
            serv_name = li.description
            if primary_risk.services:
                svc = primary_risk.services[0]
                serviceid = svc.get("serviceid", svc.get("ServiceID", ""))
                if not serv_name:
                    serv_name = svc.get("name", svc.get("ServName", ""))

            risks_list.append({
                "RiskID":      primary_risk.risk_id,
                "FinalAmount": li.approved_amount,
                "ServDate":    event_date,
                "serviceid":   serviceid,
                "ServName":    serv_name,
            })

    return risks_list, config_kind


# ── Поиск провайдера ──────────────────────────────────────────────

def find_pers_id(institution: str | None, providers: list[ProviderInfo]) -> int:
    """
    Найти PersID провайдера по названию учреждения из документов.
    Сначала точное совпадение (без учёта регистра), затем по ИНН если передан,
    затем частичное совпадение по подстроке.
    Возвращает 0 если провайдер не найден.
    """
    if not institution or not providers:
        return 0

    inst_lower = institution.lower().strip()

    # Точное совпадение
    for p in providers:
        if p.name.lower().strip() == inst_lower:
            return p.pers_id

    # Частичное совпадение: название провайдера содержится в названии учреждения или наоборот
    for p in providers:
        p_lower = p.name.lower().strip()
        if p_lower in inst_lower or inst_lower in p_lower:
            return p.pers_id

    return 0


# ── Антифрод ──────────────────────────────────────────────────────

async def check_fraud(
    db: AsyncSession,
    tenant_id: UUID,
    personal_id: str,
    event_date: date,
    institution: str | None,
    total_amount: float,
) -> list[str]:
    """Детерминированные антифрод-проверки."""
    from sqlalchemy import func, select
    from core.models.claim import Claim, ClaimStatus

    fraud_flags: list[str] = []

    # 1. Дубль: тот же personal_id + event_date
    dup = await db.execute(
        select(Claim).where(
            Claim.tenant_id == tenant_id,
            Claim.personal_id_number == personal_id,
            Claim.event_date == event_date,
            Claim.status.notin_([ClaimStatus.REJECTED, ClaimStatus.RECEIVED]),
        )
    )
    if dup.scalars().first():
        fraud_flags.append("duplicate_claim")

    # 2. Частота: > MAX за N дней
    from datetime import timedelta
    window_start = event_date - timedelta(days=settings.fraud_frequency_window_days)
    cnt = await db.execute(
        select(func.count(Claim.id)).where(
            Claim.tenant_id == tenant_id,
            Claim.personal_id_number == personal_id,
            Claim.event_date >= window_start,
            Claim.event_date <= event_date,
        )
    )
    if (cnt.scalar() or 0) > settings.fraud_frequency_max_claims:
        fraud_flags.append("frequency_anomaly")

    # 3. Аномальная сумма (TODO: после накопления статистики)

    return fraud_flags


# ── Основная функция ──────────────────────────────────────────────

async def make_decision(
    *,
    claim_id: UUID,
    tenant_id: UUID,
    extraction: ExtractionResult,
    risks_limits: RisksAndLimits,
    icd10_list: list[ICD10Item],
    providers: list[ProviderInfo],
    contract_chunks: list[ContractChunkSchema],
    submission_date: date,
    db: AsyncSession,
) -> ClaimDecision:
    """
    Принимает решение по заявке.
    Уровень 1 → Уровень 2 (Claude) → Антифрод → Маппинг на кор-систему → Аудит
    """
    with AuditTimer() as timer:

        # ── Уровень 1: Детерминированные проверки ─────────────────

        # Парсинг даты события — отдельно, чтобы ValueError не попал в бизнес-ветки
        try:
            event_date = date.fromisoformat(extraction.event.date)
        except ValueError:
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason="invalid_event_date",
                fraud_flags=[],
                overall_confidence=0.0,
                summary=(
                    f"Не удалось разобрать дату события: «{extraction.event.date}». "
                    "Требуется ручная проверка оператором."
                ),
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        # Лимит полиса — исчерпан → ручная проверка (не автоотказ).
        # Данные из кор-системы могут быть устаревшими; окончательное решение за оператором.
        try:
            check_remaining_limit(risks_limits)
        except PolicyLimitExhaustedError as e:
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason="limit_exhausted",
                fraud_flags=[],
                overall_confidence=1.0,
                summary=(
                    f"Годовой лимит полиса исчерпан: остаток {e.remaining} {e.currency}. "
                    "Автоматическое одобрение невозможно. Требуется проверка оператором."
                ),
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        # Срок подачи — однозначный детерминированный отказ (> 90 дней после события).
        if not check_claim_filed_in_time(submission_date, event_date):
            delta_days = (submission_date - event_date).days
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="rejected",
                requires_manual_review=False,
                fraud_flags=[],
                overall_confidence=1.0,
                summary=(
                    f"Заявка подана через {delta_days} дней после события "
                    f"(допустимый срок: 90 дней). Отказ на основании условий договора."
                ),
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        # ── Антифрод (параллельно с обогащением и Уровнем 2) ────────
        import asyncio
        personal_id = extraction.insured.personal_id
        fraud_task = asyncio.create_task(check_fraud(
            db, tenant_id, personal_id,
            event_date, extraction.event.institution,
            extraction.event.total_claimed,
        ))

        # ── Обогащение диагнозов иерархией МКБ-10 ────────────────
        diagnosis_codes = [d.icd10_code for d in extraction.event.diagnoses]
        enriched: dict[str, EnrichedDiagnosis] = await enrich_all(diagnosis_codes, db)

        # ── CARVEOUT Preprocessing (детерминированное исключение) ────
        # Для каждого диагноза проверяем CARVEOUT-условия без Claude
        carveout_chunks = [c for c in contract_chunks if c.section_type == "exclusion_with_carveout"]
        carveout_rejections: dict[str, str] = {}  # {icd10_code: rejection_reason}

        for diag in extraction.event.diagnoses:
            should_reject, reason = apply_carveout_exclusion_logic(
                diag.icd10_code,
                extraction.event.service_urgency,
                carveout_chunks,
            )
            if should_reject:
                carveout_rejections[diag.icd10_code] = reason
                log.info(
                    "carveout_quick_rejection",
                    claim_id=str(claim_id),
                    icd10_code=diag.icd10_code,
                    reason=reason,
                )

        # ── Уровень 2: Claude API ─────────────────────────────────
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        user_prompt = build_decision_prompt(extraction, enriched, risks_limits, contract_chunks)

        try:
            response = await client.messages.create(
                model=settings.claude_model,
                max_tokens=settings.claude_decision_max_tokens,
                temperature=settings.claude_decision_temperature,
                system=DECISION_SYSTEM_PROMPT,
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": "make_claim_decision"},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIError as e:
            log.error("decision_claude_error", claim_id=str(claim_id), error=str(e))
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason=f"Claude API error: {e}",
                fraud_flags=[],
                overall_confidence=0.0,
                summary=f"Ошибка AI-анализа: {e}. Требуется ручная проверка оператором.",
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason="Claude did not return tool_use block",
                fraud_flags=[],
                overall_confidence=0.0,
                summary="AI не вернул структурированный ответ. Требуется ручная проверка.",
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        raw: dict[str, Any] = tool_block.input
        fraud_flags = await fraud_task

        diagnoses = [DiagnosisDecisionSchema(**d) for d in raw.get("diagnoses", [])]
        line_items = [LineItemDecisionSchema(**li) for li in raw.get("line_items", [])]

        # ── Применить CARVEOUT-отказы (переопределить решение Claude) ────────
        # Если диагноз попал в быстрый CARVEOUT-отказ, переопределяем решение
        for diag in diagnoses:
            if diag.icd10_code in carveout_rejections:
                diag.is_covered = False
                diag.approved_amount = 0.0
                diag.rejection_reason = carveout_rejections[diag.icd10_code]
                # Не обновляем contract_reference — CARVEOUT-причина в rejection_reason

        all_covered = all(d.is_covered for d in diagnoses) if diagnoses else False
        any_covered = any(d.is_covered for d in diagnoses) if diagnoses else False

        if raw.get("requires_manual_review") or fraud_flags:
            status = "manual_review"
        elif all_covered:
            status = "approved"
        elif any_covered:
            status = "partial"
        else:
            status = "rejected"

        # ── Маппинг на справочники кор-системы ───────────────────
        # DiagnosID: берём из первого диагноза с покрытием
        diagnosid: int | None = None
        for diag in diagnoses:
            found = find_diagnosid(diag.icd10_code, icd10_list)
            if found is not None:
                diagnosid = found
                break
        # Если ни один не нашли — берём первый диагноз
        if diagnosid is None and extraction.event.diagnoses:
            diagnosid = find_diagnosid(extraction.event.diagnoses[0].icd10_code, icd10_list)

        # risks_list и config_kind из одобренных позиций
        risks_list, config_kind = build_risks_list(line_items, risks_limits, extraction.event.date)

        # PersID: ищем по названию учреждения из документов
        pers_id = find_pers_id(extraction.event.institution, providers)

        # ── Stochastic QA sampling (Шаг 28) ──────────────────────
        # 5% автоодобренных заявок → manual_review для контроля точности.
        # Payout не меняется — оператор только верифицирует решение.
        qa_sample = (
            status == "approved"
            and not raw.get("requires_manual_review", False)
            and not fraud_flags
            and random.random() < settings.decision_stochastic_qa_rate
        )
        if qa_sample:
            log.info("stochastic_qa_sample_triggered", claim_id=str(claim_id))

        decision = ClaimDecision(
            claim_id=claim_id,
            diagnoses=diagnoses,
            line_items=line_items,
            total_approved=raw.get("total_approved", 0.0),
            deductible_applied=raw.get("deductible_applied", 0.0),
            final_payout=raw.get("final_payout", 0.0),
            status=status,
            requires_manual_review=raw.get("requires_manual_review", False) or bool(fraud_flags) or qa_sample,
            manual_review_reason="stochastic_qa_sample" if qa_sample else raw.get("manual_review_reason"),
            fraud_flags=fraud_flags,
            overall_confidence=raw.get("overall_confidence", 0.0),
            summary=raw.get("summary", ""),
            rag_chunks_used=[str(chunk.id) for chunk in contract_chunks],
            prompt_version=PROMPT_VERSION,
            model_version=settings.claude_model,
            # Поля для ClaimParsing_UNI
            diagnosid=diagnosid,
            pers_id=pers_id,
            config_kind=config_kind,
            risks_list=risks_list,
        )

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="decision",
        input_data={
            "diagnoses_count": len(extraction.event.diagnoses),
            "total_claimed":   extraction.event.total_claimed,
            "rag_chunks_count": len(contract_chunks),
            "risks_count":     len(risks_limits.risks),
        },
        output_data={
            "status":          decision.status,
            "final_payout":    decision.final_payout,
            "requires_manual_review": decision.requires_manual_review,
            "fraud_flags":     decision.fraud_flags,
            "diagnosid":       decision.diagnosid,
            "pers_id":         decision.pers_id,
            "config_kind":     decision.config_kind,
            "summary":         decision.summary[:300],
            "qa_sample":       qa_sample,
        },
        confidence={"overall": decision.overall_confidence},
        rag_chunks=[str(c.id) for c in contract_chunks],
        prompt_version=PROMPT_VERSION,
        model_version=settings.claude_model,
        duration_ms=timer.duration_ms,
    )

    log.info(
        "decision_made",
        claim_id=str(claim_id),
        status=decision.status,
        final_payout=decision.final_payout,
        diagnosid=decision.diagnosid,
        confidence=decision.overall_confidence,
        fraud_flags=decision.fraud_flags,
    )

    return decision
