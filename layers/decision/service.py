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
import re
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import PolicyLimitExhaustedError
from core.llm_client import BaseLLMClient, LLMAPIError, LLMNoToolBlockError, get_llm_client
from core.schemas.claim import ExtractionResult
from core.schemas.contract import ContractChunkSchema
from core.schemas.core_api import ICD10Item, ProviderInfo, RisksAndLimits
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema, LineItemDecisionSchema
from core.tenant_config import get_tenant_config_float
from layers.core_adapter.risk_matcher import match_risks
from layers.decision.exclusion_checker import (
    ExclusionResult,
    apply_wording_carveout,
    check_exclusions,
)
from layers.decision.icd10_enricher import EnrichedDiagnosis, enrich_all

log = structlog.get_logger()
settings = get_settings()

# v3.1.0: coherence_flags (Шаг 21), исключения через дерево МКБ-10 (Шаг 22),
#         reasoning через thinking/CoT (Шаг 26)
PROMPT_VERSION = "decision/v3.1.0"

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
            "coherence_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Медицинские несоответствия услуг диагнозам (Шаг 21), например "
                    "'МРТ позвоночника не соответствует J06.9 (ОРВИ)'. "
                    "Пустой массив если услуги согласованы с диагнозами."
                ),
            },
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
   - Проверяй раздел [exclusions] для каждого диагноза И КАЖДОГО ЕГО ПРЕДКА
     из «Медицинской иерархии»: если ЛЮБОЙ предок диагноза входит в исключённую
     категорию — исключение применяется.
     Пример: C34.1 → предок «Злокачественные новообразования» → исключение
     «онкологические заболевания» применяется, хотя код C34.1 в договоре не упомянут.
   - Если диагноз (или его предок) в списке исключённых → ОТКАЗ

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

МЕДИЦИНСКАЯ СОГЛАСОВАННОСТЬ (coherence_flags):
Дополнительно проверь — логически ли связаны услуги (line_items) с диагнозами?
Пример несоответствия: «МРТ позвоночника» при J06.9 (ОРВИ).
Несоответствие → опиши его в coherence_flags и снизь confidence,
но НЕ отказывай автоматически — окончательное решение примет оператор.

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


def check_waiting_period(
    policy_start_date: date,
    event_date: date,
    service_type: str,
    waiting_days: int,
) -> bool:
    """
    Шаг 23: период ожидания — первые N дней полиса плановые услуги не покрываются.
    Экстренные случаи (emergency/urgent) обходят период ожидания.
    """
    if service_type in ("emergency", "urgent"):
        return True
    return (event_date - policy_start_date).days >= waiting_days


def check_sublimits(
    line_items: list,
    risks_limits: RisksAndLimits,
) -> list[str]:
    """
    Шаг 23: проверить позиции заявки против суб-лимитов рисков.

    Возвращает список превышений (пусто = всё в норме). Пока кор-система
    не передаёт маппинг услуга→риск, каждая позиция консервативно сверяется
    с МИНИМАЛЬНЫМ суб-лимитом: возможные превышения уходят в manual_review
    (не отказ), ложные срабатывания разбирает оператор.
    """
    violations: list[str] = []
    risks_with_sublimit = [r for r in risks_limits.risks if r.sublimit is not None]
    if not risks_with_sublimit or not line_items:
        return violations

    strictest = min(risks_with_sublimit, key=lambda r: r.sublimit)
    for item in line_items:
        if item.amount > strictest.sublimit:
            violations.append(
                f"«{item.description}» ({item.amount} {strictest.currency}) превышает "
                f"суб-лимит {strictest.sublimit} {strictest.currency} риска «{strictest.name}»"
            )
    return violations


# ── Промпт Decision Engine ─────────────────────────────────────────

def build_decision_prompt(
    extraction: ExtractionResult,
    enriched: dict[str, "EnrichedDiagnosis"],
    risks_limits: RisksAndLimits,
    chunks: list[ContractChunkSchema],
    positive_list_match: dict[str, tuple[bool, str | None]] | None = None,
) -> str:
    """Собирает промпт: данные заявки + иерархия МКБ-10 + риски/лимиты + чанки договора.

    ВАЖНО: выделяет CARVEOUT-исключения и POSITIVE LIST отдельно.

    Args:
        positive_list_match: результат check_positive_list() вида {description: (is_in_list, procedure_name)}
    """
    if positive_list_match is None:
        positive_list_match = {}

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
    # + полный список предков с кодами для проверки исключений (Шаг 22)
    hierarchy_lines = []
    for d in extraction.event.diagnoses:
        e = enriched.get(d.icd10_code)
        if e and e.name_r:
            hierarchy_lines.append(
                f"  {d.icd10_code}: {e.category_chain_ru}"
            )
            for a in e.ancestors:
                code_part = f" [{a.extcod}]" if a.extcod else ""
                name = a.name_r or a.name_e or a.extcod or "—"
                hierarchy_lines.append(f"      предок: {name}{code_part}")
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

    # Разделяем чанки: CARVEOUT-исключения и обычные исключения — отдельные секции
    carveout_chunks = [c for c in chunks if c.section_type == "exclusion_with_carveout"]
    exclusion_chunks = [c for c in chunks if c.section_type == "exclusions"]
    other_chunks = [
        c for c in chunks
        if c.section_type not in ("exclusion_with_carveout", "exclusions")
    ]

    SECTION_ORDER = {"coverage_cases": 1, "limits": 2, "claim_conditions": 3}
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

    # POSITIVE LIST: явно покрытые процедуры (100%)
    positive_list_text = ""
    positive_procedures = [
        f"  ✓ {desc}" for desc, (is_in_list, proc_name) in positive_list_match.items()
        if is_in_list
    ]
    if positive_procedures:
        positive_list_text = "\n".join(positive_procedures)

    # Шаг 22: обычные исключения — отдельной секцией ПЕРЕД секциями покрытия,
    # с явной инструкцией проверять предков диагноза
    if exclusion_chunks:
        exclusions_text = "\n\n".join(
            f"[exclusions] {chunk.title or ''}\n{chunk.content}"
            for chunk in exclusion_chunks
        )
        sections.append(f"""## Исключения (проверь КАЖДЫЙ диагноз И КАЖДОГО его предка против этого списка)
{exclusions_text}""")

    if carveout_text:
        sections.append(f"""## CARVEOUT-исключения (исключения с УСЛОВИЯМИ)
⚠️  ВАЖНО: Проверь условие перед отказом!
{carveout_text}""")

    if positive_list_text:
        sections.append(f"""## POSITIVE LIST — явно покрытые процедуры (100%)
✅ ЭТИ ПРОЦЕДУРЫ ВСЕГДА ПОКРЫТЫ (раздел 1.7.3-1.7.4), не требуют диагностики:
{positive_list_text}""")

    if other_text:
        sections.append(f"""## Остальные пункты договора
{other_text}""")
    else:
        sections.append("## Остальные пункты договора\n(не найдены)")

    return "\n\n".join(sections)


# ── POSITIVE LIST — явно покрытые процедуры ──────────────────────────────

async def check_positive_list(
    line_items: list[LineItem],
    tenant_id: UUID,
    policy_number: str,
    version_id: str,
    db: AsyncSession,
) -> dict[str, tuple[bool, str | None]]:
    """
    Проверить есть ли процедуры/услуги в POSITIVE LIST.

    Возвращает словарь:
      {
        "line_item_description": (is_in_positive_list, procedure_name)
      }

    Если процедура в POSITIVE LIST → ВСЕГДА ПОКРЫТА (100%).
    Этот результат переопределяет любые CARVEOUT-исключения.
    """
    from core.models.contract import PositiveListProcedure
    from sqlalchemy import or_, select

    if not line_items:
        return {}

    results = {}

    # Загружаем все процедуры для этого контракта
    stmt = select(PositiveListProcedure).where(
        PositiveListProcedure.tenant_id == tenant_id,
        PositiveListProcedure.policy_number == policy_number,
        PositiveListProcedure.version_id == version_id,
    )
    db_result = await db.execute(stmt)
    procedures_in_list = db_result.scalars().all()

    if not procedures_in_list:
        # POSITIVE LIST пуст — все результаты (False, None)
        for item in line_items:
            results[item.description] = (False, None)
        return results

    # Для каждой услуги в заявке проверяем совпадение с POSITIVE LIST
    from difflib import SequenceMatcher

    for item in line_items:
        desc_lower = (item.description or "").lower().strip()
        best_match = None
        best_ratio = 0.0

        for proc in procedures_in_list:
            # Проверяем совпадение по всем названиям
            names_to_check = [
                proc.procedure_name_ka or "",
                proc.procedure_name_ru or "",
                proc.procedure_name_en or "",
            ]

            for name in names_to_check:
                if not name:
                    continue

                name_lower = name.lower().strip()
                ratio = SequenceMatcher(None, desc_lower, name_lower).ratio()

                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = proc

        # Если совпадение ≥ 0.70 → в POSITIVE LIST
        if best_ratio >= 0.70 and best_match:
            procedure_name = (
                best_match.procedure_name_ru or
                best_match.procedure_name_ka or
                best_match.procedure_name_en or
                "Unknown"
            )
            results[item.description] = (True, procedure_name)
        else:
            results[item.description] = (False, None)

    return results


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

_ICD10_STD_PART_RE = re.compile(r'^([A-Z]\d{2,3}(?:\.[0-9]{1,2})?)')


def _normalize_icd10_code(code: str) -> str:
    """
    Нормализует нестандартные форматы кодов МКБ-10 из грузинских мед. форм:
      E55D     → E55     (буква-суффикс после цифр)
      I10-I15  → I10     (диапазон)
      E78.2A   → E78.2   (буква после десятичной части)
    """
    code = code.strip().upper()
    code = code.split("-")[0]          # диапазон: I10-I15 → I10
    m = _ICD10_STD_PART_RE.match(code)
    return m.group(1) if m else code


def find_diagnosid(icd10_code: str, icd10_list: list[ICD10Item]) -> str | None:
    """
    Поиск DiagnosID по коду МКБ-10. Возвращает EXTCOD (строку вида "J06.9").
    Пробует: 1) точное совпадение, 2) prefix, 3) нормализованный код + его prefix.
    """
    code_upper = icd10_code.upper().strip()
    exact_map: dict[str, ICD10Item] = {item.code.upper().strip(): item for item in icd10_list}

    # 1. Точное совпадение
    if code_upper in exact_map:
        return exact_map[code_upper].code

    # 2. Prefix-совпадение: J06 ↔ J06.9 (пробуем для оригинального кода)
    prefix = code_upper.split(".")[0]
    for db_code, item in exact_map.items():
        if db_code.split(".")[0] == prefix:
            return item.code

    # 3. Нормализация + повтор (E55D → E55, I10-I15 → I10)
    normalized = _normalize_icd10_code(code_upper)
    if normalized != code_upper:
        if normalized in exact_map:
            return exact_map[normalized].code
        norm_prefix = normalized.split(".")[0]
        for db_code, item in exact_map.items():
            if db_code.split(".")[0] == norm_prefix:
                return item.code

    return None


# Regex для поиска кодов МКБ-10 в OCR-тексте (Latin letter + digits, например J06.9, M54)
_ICD10_OCR_RE = re.compile(r"\b([A-Z]\d{1,2}(?:\.[0-9]{1,2})?)\b", re.UNICODE)


def find_diagnosid_in_ocr(
    ocr_texts: list[str],
    icd10_list: list[ICD10Item],
) -> tuple[str | None, str | None]:
    """
    Инвертированный поиск диагноза в OCR-документах.

    Алгоритм:
    1. Извлечь все ICD-10-подобные паттерны из OCR ([A-Z]\\d{1,2}(\\.\\d{1,2})?)
    2. Каждый найденный код проверить против локального справочника icd10_diagnoses
    3. Совпадение → вернуть (EXTCOD, EXTCOD); ничего → (None, None) → requires_manual_review

    Returns:
        (diagnosid, icd10_code) — оба значения EXTCOD-строка вида "J06.9".
        diagnosid передаётся в ClaimParsing_UNI как DiagnosID.
    """
    if not ocr_texts or not icd10_list:
        return None, None

    combined = " ".join(ocr_texts)
    found_codes: set[str] = {m.upper() for m in _ICD10_OCR_RE.findall(combined)}

    if not found_codes:
        return None, None

    # Индекс: код (верхний регистр) → ICD10Item
    exact_map: dict[str, ICD10Item] = {item.code.upper(): item for item in icd10_list}

    # 1. Точное совпадение
    for code in sorted(found_codes):
        if code in exact_map:
            item = exact_map[code]
            return item.code, item.code

    # 2. Prefix-совпадение: J06 ↔ J06.9
    for code in sorted(found_codes):
        prefix = code.split(".")[0]
        for db_code, item in exact_map.items():
            if db_code.split(".")[0] == prefix:
                return item.code, item.code

    return None, None


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

# Правовые формы в начале названия провайдера — отбрасываем для core-поиска.
# Используется в find_pers_id_in_ocr() для стрипания начала строки провайдера.
_LEGAL_PREFIX_RE = re.compile(
    r"^(?:შ\.?პ\.?ს\.?\s*[\"„]?\s*|შπс\s*[\"„]?\s*|"
    r"ип\s+|ооо\s+|зао\s+|оао\s+|тоо\s+)",
    re.IGNORECASE | re.UNICODE,
)
# Правовые формы в любой позиции строки (и префикс и суффикс) —
# используется в find_pers_id() для нормализации OCR-строки учреждения.
#
# Georgian "შ.პ.ს." встречается в трёх вариантах написания:
#   • Чистый Georgian:   შ.პ.ს.  (U+10E8, U+10DE, U+10E1)
#   • Mixed Geoгрузинский+кириллица: შпс   (U+10E8, U+043F, U+0441)  ← самый частый в документах
#   • Mixed Georgian+греческий:      შπс   (U+10E8, U+03C0, U+0441)  ← встречается в legacy-источниках
# Символьный класс [პпπ] и [სс] охватывает все три варианта.
_LEGAL_FORM_RE = re.compile(
    r"შ\.?[პпπ]\.?[სс]\.?"  # Georgian LP: შ.პ.ს. / შпс / შπс
    r"|\b(?:ип|ооо|зао|оао|тоо|пао)\b"               # Russian: ИП, ООО, ЗАО ...
    r"|\b(?:llc|ltd|l\.l\.c\.|l\.t\.d\.|cjsc|jsc|inc|corp)\b",  # English
    re.IGNORECASE | re.UNICODE,
)
# Пунктуация и лишние пробелы — нормализуем для сравнения
_PUNCT_NORM_RE = re.compile(r'[\s"\'„"«»{}()\[\].,;:!?\-]+', re.UNICODE)


def _institution_core(name: str) -> str:
    """Нормализованное ядро названия: убираем правовые формы и пунктуацию."""
    s = _LEGAL_FORM_RE.sub("", name.lower())
    s = _PUNCT_NORM_RE.sub(" ", s)
    return s.strip()


def find_pers_id_in_ocr(ocr_texts: list[str], providers: list[ProviderInfo]) -> int:
    """
    Инвертированный поиск провайдера: берём всех провайдеров из справочника
    и ищем каждого в полном тексте OCR-документов.

    Преимущество перед find_pers_id: OCR может усекать название клиники,
    переносить на несколько строк, добавлять мусор — но хотя бы часть
    полного имени всегда присутствует где-то в тексте.

    Алгоритм:
    1. Объединить все OCR-тексты, привести к нижнему регистру
    2. Для каждого провайдера вычислить "ядро" (без правовой формы)
    3. Проверить: есть ли ядро как подстрока в OCR-тексте
    4. Среди всех совпавших — вернуть провайдера с САМЫМ ДЛИННЫМ ядром
       (наиболее специфичное совпадение выигрывает)
    """
    if not ocr_texts or not providers:
        return 0

    combined_lower = "\n".join(ocr_texts).lower()
    # Нормализованный OCR: пунктуация → пробел (Georgian OCR часто добавляет лишние знаки)
    combined_norm = _PUNCT_NORM_RE.sub(" ", combined_lower)

    best_core_len = 0
    best_id = 0

    for p in providers:
        p_lower = p.name.lower()

        # Ядро: без правовой формы, без кавычек/пунктуации
        p_core = _LEGAL_PREFIX_RE.sub("", p_lower).strip()
        p_core_norm = _PUNCT_NORM_RE.sub(" ", p_core).strip()

        if len(p_core_norm) < 5:
            continue

        if p_core_norm in combined_norm and len(p_core_norm) > best_core_len:
            best_core_len = len(p_core_norm)
            best_id = p.pers_id

    return best_id


def find_pers_id(institution: str | None, providers: list[ProviderInfo]) -> int:
    """
    Резервный поиск провайдера по извлечённому названию учреждения.
    Используется как fallback если find_pers_id_in_ocr не нашёл результата.

    Порядок поиска:
    1. Точное совпадение (регистронезависимо)
    2. Подстрока (один включает другой)
    3. Точное совпадение нормализованных ядер (без правовых форм и пунктуации)
    4. Подстрока нормализованных ядер
    5. Fuzzy по нормализованным ядрам с порогом 0.65

    Нормализация (_institution_core) убирает: ООО, შпс, LLC, Ltd и т.п.;
    очищает пунктуацию/кавычки — это ключевое улучшение по сравнению со старым
    подходом где "შпс „Аврора"" не матчился с "Аврора".
    """
    from difflib import SequenceMatcher

    if not institution or not providers:
        return 0

    inst_lower = institution.lower().strip()

    # Шаг 1: точное совпадение сырых строк
    for p in providers:
        if p.name.lower().strip() == inst_lower:
            return p.pers_id

    # Шаг 2: подстрока сырых строк
    for p in providers:
        p_lower = p.name.lower().strip()
        if p_lower in inst_lower or inst_lower in p_lower:
            return p.pers_id

    # Шаг 3-5: нормализованные ядра (без правовых форм и пунктуации)
    inst_core = _institution_core(institution)
    if len(inst_core) < 4:
        return 0

    # Шаг 3: точное совпадение ядер
    for p in providers:
        if _institution_core(p.name) == inst_core:
            return p.pers_id

    # Шаг 4: подстрока ядер
    for p in providers:
        p_core = _institution_core(p.name)
        if len(p_core) >= 4 and (p_core in inst_core or inst_core in p_core):
            return p.pers_id

    # Шаг 5: fuzzy по нормализованным ядрам, порог 0.65
    best_score = 0.0
    best_id = 0
    for p in providers:
        p_core = _institution_core(p.name)
        if not p_core:
            continue
        score = SequenceMatcher(None, inst_core, p_core).ratio()
        if score > best_score:
            best_score = score
            best_id = p.pers_id
    if best_score >= 0.65:
        return best_id

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


# ── Utility: Extract version_id from contract chunks ────────────────

def extract_contract_version_id(chunks: list[ContractChunkSchema]) -> str:
    """
    Извлечь version_id из contract_chunks.

    Все чанки одного контракта имеют одинаковый version_id.
    Если чанки пусты или version_id не найден → fallback на "latest".

    Args:
        chunks: список ContractChunkSchema с полем version_id

    Returns:
        version_id (строка), например "v20240609" или "latest" (fallback)
    """
    if not chunks:
        log.warning("extract_version_id_no_chunks")
        return "latest"

    for chunk in chunks:
        if hasattr(chunk, "version_id") and chunk.version_id:
            return chunk.version_id

    log.warning("extract_version_id_not_found")
    return "latest"


# ── Шаг 26: расширенное рассуждение ───────────────────────────────

def _is_complex_case(extraction: ExtractionResult) -> bool:
    """Триггер расширенного рассуждения: несколько диагнозов, крупная сумма
    или низкая уверенность извлечения."""
    return (
        len(extraction.event.diagnoses) > 1
        or extraction.event.total_claimed > settings.decision_extended_thinking_threshold
        or extraction.extraction_confidence < settings.decision_extended_thinking_extraction_conf_threshold
    )



async def _second_pass_diagnosis(
    *,
    client: "BaseLLMClient",
    target: DiagnosisDecisionSchema,
    enriched: dict[str, EnrichedDiagnosis],
    contract_chunks: list[ContractChunkSchema],
    claim_id: UUID,
    tenant_id: UUID,
    db: AsyncSession,
) -> bool:
    """
    Шаг 26: узконаправленный повторный вызов по одному неуверенному диагнозу.

    Контекст — только этот диагноз, его иерархия МКБ-10 и разделы
    исключений/CARVEOUT. Решение merge-ится в target (мутация на месте).
    Возвращает True если решение уточнено.
    """
    e = enriched.get(target.icd10_code)
    hierarchy = e.category_chain_ru if e and e.name_r else "(не найден в справочнике МКБ-10)"
    ancestors_text = ""
    if e and e.ancestors:
        ancestors_text = "\n".join(
            f"  предок: {a.name_r or a.name_e or a.extcod or '—'}"
            + (f" [{a.extcod}]" if a.extcod else "")
            for a in e.ancestors
        )

    relevant_chunks = [
        c for c in contract_chunks
        if c.section_type in ("exclusions", "exclusion_with_carveout")
    ]
    chunks_text = "\n\n".join(
        f"[{c.section_type}] {c.title or ''}\n{c.content}"
        for c in relevant_chunks
    ) or "(разделы исключений не найдены)"

    prompt = f"""Повторно оцени ОДИН спорный диагноз (первичная уверенность была низкой).

Диагноз: {target.icd10_code}
Медицинская категория: {hierarchy}
{ancestors_text}

Первичное решение: is_covered={target.is_covered}, approved_amount={target.approved_amount}, confidence={target.confidence}

## Исключения и CARVEOUT договора
{chunks_text}

Вызови инструмент make_claim_decision: в diagnoses верни решение ТОЛЬКО по этому диагнозу,
с цитатой из договора в contract_reference. Остальные поля заполни нулями/пустыми."""

    sp_input_tokens = 0
    sp_output_tokens = 0
    try:
        sp_result = await client.call_tool(
            system=DECISION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            tool=DECISION_TOOL,
            tool_name="make_claim_decision",
            max_tokens=settings.claude_decision_max_tokens,
            temperature=settings.claude_decision_temperature,
        )
        sp_input_tokens = sp_result.input_tokens
        sp_output_tokens = sp_result.output_tokens
    except (LLMAPIError, LLMNoToolBlockError) as exc:
        log.warning("second_pass_failed", claim_id=str(claim_id), error=str(exc))
        return False

    refined = next(
        (d for d in (sp_result.tool_input or {}).get("diagnoses") or []
         if d.get("icd10_code") == target.icd10_code),
        None,
    )
    if refined is None:
        return False

    before = target.model_dump()
    target.is_covered = refined.get("is_covered", target.is_covered)
    target.approved_amount = refined.get("approved_amount", target.approved_amount)
    target.rejection_reason = refined.get("rejection_reason", target.rejection_reason)
    target.contract_reference = refined.get("contract_reference", target.contract_reference)
    target.confidence = refined.get("confidence", target.confidence)

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="decision_second_pass",
        input_data={
            "icd10_code": target.icd10_code,
            "before": before,
            "prompt_chars": len(prompt),
            "user_prompt": prompt,
        },
        output_data={
            "after": target.model_dump(),
            "llm_raw_response": sp_result.tool_input,
            "input_tokens": sp_input_tokens,
            "output_tokens": sp_output_tokens,
        },
        prompt_version=PROMPT_VERSION,
        model_version=settings.claude_model,
    )
    log.info(
        "second_pass_completed",
        claim_id=str(claim_id),
        icd10_code=target.icd10_code,
        confidence_before=before["confidence"],
        confidence_after=target.confidence,
    )
    return True


# ── Основная функция ──────────────────────────────────────────────

async def make_decision(
    *,
    claim_id: UUID,
    tenant_id: UUID,
    policy_number: str,
    extraction: ExtractionResult,
    risks_limits: RisksAndLimits,
    icd10_list: list[ICD10Item],
    providers: list[ProviderInfo],
    contract_chunks: list[ContractChunkSchema],
    submission_date: date,
    db: AsyncSession,
    form_100_ocr_text: str = "",
    ocr_texts: list[str] | None = None,
) -> ClaimDecision:
    """
    Принимает решение по заявке.
    Уровень 1 → Уровень 2 (Claude) → Антифрод → Маппинг на кор-систему → Аудит
    """
    with AuditTimer() as timer:

        # ── Уровень 1: Детерминированные проверки ─────────────────

        # PersID — вычисляется здесь, до всех early-return путей,
        # чтобы ClaimParsing_UNI всегда получал реальный код клиники,
        # а не fallback 914450 (дистанционный провайдер).
        pers_id: int = find_pers_id_in_ocr(ocr_texts or [], providers)
        if pers_id == 0 and extraction.event.institution:
            _institution_candidates: list[str | None] = [extraction.event.institution]
            if extraction.cross_document:
                _institution_candidates.append(
                    extraction.cross_document.receipt.institution
                    if extraction.cross_document.receipt else None
                )
                _institution_candidates.append(
                    extraction.cross_document.form_100.institution
                    if extraction.cross_document.form_100 else None
                )
            for _inst in _institution_candidates:
                if _inst:
                    pers_id = find_pers_id(_inst, providers)
                    if pers_id:
                        break
        if pers_id == 0:
            log.warning("pers_id_not_found", claim_id=str(claim_id),
                        institution=extraction.event.institution)

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
                pers_id=pers_id,
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
                pers_id=pers_id,
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
                pers_id=pers_id,
            )

        # ── Уровень 1: полис активен на дату события ───────────────
        # Даты приходят из getpolicylist (Objects.StartDate/EndDate, DD/MM/YYYY).
        # Вне периода → manual_review (не отказ: данные могут быть неточны).
        if (
            (risks_limits.policy_start_date and event_date < risks_limits.policy_start_date)
            or (risks_limits.policy_end_date and event_date > risks_limits.policy_end_date)
        ):
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason="event_outside_policy_period",
                fraud_flags=[],
                overall_confidence=1.0,
                summary=(
                    f"Дата события {event_date} вне периода действия полиса "
                    f"({risks_limits.policy_start_date or '?'} — "
                    f"{risks_limits.policy_end_date or '?'}). "
                    "Требуется проверка оператором."
                ),
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
                pers_id=pers_id,
            )

        # ── Шаг 23: период ожидания (детерминированно, уровень 1) ──
        # Управляется флагом DECISION_WAITING_PERIOD_ENABLED (.env).
        # Только если кор-система предоставила policy_start_date;
        # нарушение → manual_review (данные кор-системы могут быть устаревшими).
        waiting_period_note = "passed"
        if not settings.decision_waiting_period_enabled:
            waiting_period_note = "disabled_by_config"
        else:
            exempt_marker = settings.core_api_waiting_period_exempt_marker
            if risks_limits.policy_start_date is None:
                waiting_period_note = "skipped_no_policy_start_date"
            elif (
                exempt_marker
                and risks_limits.object_data
                and exempt_marker in risks_limits.object_data
            ):
                # Кор-система явно освободила объект от периода ожидания
                # (Objects.ObjectData: "არ ეკუთვნის მოცდის პერიოდი")
                waiting_period_note = "exempt_by_policy"
            else:
                from layers.extraction.service import resolve_service_urgency
                resolved_urgency = resolve_service_urgency(
                    extraction.event.service_urgency, extraction.event.diagnoses
                )
                service_type = "emergency" if resolved_urgency == "urgent" else "planned"
                if not check_waiting_period(
                    risks_limits.policy_start_date, event_date, service_type,
                    settings.decision_default_waiting_period_days,
                ):
                    days_since_start = (event_date - risks_limits.policy_start_date).days
                    return ClaimDecision(
                        claim_id=claim_id,
                        diagnoses=[],
                        total_approved=0.0,
                        deductible_applied=0.0,
                        final_payout=0.0,
                        status="manual_review",
                        requires_manual_review=True,
                        manual_review_reason="waiting_period_violation",
                        fraud_flags=[],
                        overall_confidence=1.0,
                        summary=(
                            f"Событие на {days_since_start}-й день полиса при периоде ожидания "
                            f"{settings.decision_default_waiting_period_days} дней (услуга плановая). "
                            "Требуется проверка оператором."
                        ),
                        prompt_version=PROMPT_VERSION,
                        model_version=settings.claude_model,
                        pers_id=pers_id,
                    )

        # ── Шаг 23: суб-лимиты (детерминированно, уровень 1) ───────
        # Превышение не останавливает решение — уходит в manual_review ниже.
        # Используем только чековые строки: форма 100 содержит дозировки/количества,
        # которые не являются ценами и не должны проверяться против лимитов.
        _receipt_items_sl = [
            li for li in extraction.event.line_items
            if li.doc_source and li.doc_source.startswith("receipt")
        ]
        _items_for_sublimit = _receipt_items_sl if _receipt_items_sl else extraction.event.line_items
        sublimit_violations = check_sublimits(_items_for_sublimit, risks_limits)
        if sublimit_violations:
            log.info(
                "sublimit_violations_detected",
                claim_id=str(claim_id),
                violations=sublimit_violations,
            )

        # ── Уровень 1: исключения по вордингу страховых условий ──────
        # Детерминированная проверка из таблицы exclusion_rules (Excel → DB).
        # Выполняется ДО Claude. Скоуп: 'all' для всех; 'family' — доп. для
        # членов семьи (CardNumber /2, /3, /4 → insured_type='family').
        wording_exclusions: dict[str, tuple[str, ExclusionResult]] = {}
        # {icd10_code → (rejection_reason, excl_result)}
        wording_manual_review: dict[str, ExclusionResult] = {}
        # {icd10_code → excl_result} — для случаев неизвестной срочности при CARVEOUT

        for diag in extraction.event.diagnoses:
            excl = await check_exclusions(
                diag.icd10_code,
                risks_limits.insured_type,
                tenant_id,
                db,
            )
            if excl is None:
                continue
            is_excluded, reason = apply_wording_carveout(
                excl, extraction.event.service_urgency
            )
            if is_excluded and reason:
                wording_exclusions[diag.icd10_code] = (reason, excl)
                log.info(
                    "wording_exclusion_applied",
                    claim_id=str(claim_id),
                    icd10_code=diag.icd10_code,
                    scope=excl.scope,
                    carveout_conditions=excl.carveout_conditions,
                )
            elif not is_excluded and excl.carveout_conditions:
                # CARVEOUT есть, но service_urgency неизвестна → manual_review
                wording_manual_review[diag.icd10_code] = excl

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

        # ── POSITIVE LIST Preprocessing (явно покрытые процедуры) ────
        # Проверяем какие услуги в POSITIVE LIST (всегда 100% покрыты)
        contract_version_id = extract_contract_version_id(contract_chunks)
        positive_list_match = await check_positive_list(
            extraction.event.line_items,
            tenant_id=tenant_id,
            policy_number=policy_number,
            version_id=contract_version_id,
            db=db,
        )
        log.info(
            "positive_list_check_done",
            claim_id=str(claim_id),
            contract_version_id=contract_version_id,
            matched_count=sum(1 for v in positive_list_match.values() if v[0]),
        )

        # ── Уровень 2: LLM API (Anthropic или Gemini) ────────────────
        llm_client = get_llm_client()
        user_prompt = build_decision_prompt(
            extraction, enriched, risks_limits, contract_chunks,
            positive_list_match=positive_list_match,
        )

        # ── Шаг 26: режим рассуждения для сложных случаев ─────────
        # thinking поддерживается только Anthropic; CoT работает с обоими.
        use_thinking = (
            bool(settings.decision_extended_thinking_enabled)
            and llm_client.supports_thinking
            and _is_complex_case(extraction)
        )
        use_cot = (
            not use_thinking
            and bool(settings.decision_chain_of_thought_enabled)
            and _is_complex_case(extraction)
        )
        reasoning_parts: list[str] = []
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        claude_input_tokens = 0
        claude_output_tokens = 0

        try:
            if use_cot:
                cot_result = await llm_client.call_text(
                    system=DECISION_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": user_prompt + (
                            "\n\nПроанализируй заявку шаг за шагом: категории диагнозов, "
                            "применимые разделы договора, исключения. Пока БЕЗ финального решения."
                        ),
                    }],
                    max_tokens=settings.claude_decision_max_tokens,
                    temperature=settings.claude_decision_temperature,
                )
                cot_text = cot_result.text or ""
                if cot_text:
                    reasoning_parts.append(cot_text)
                    messages = [{
                        "role": "user",
                        "content": f"{user_prompt}\n\n## Предварительный анализ\n{cot_text}",
                    }]
                claude_input_tokens += cot_result.input_tokens
                claude_output_tokens += cot_result.output_tokens

            # thinking-путь: добавляем явную инструкцию вызвать инструмент
            tool_messages = (
                [{"role": "user", "content": user_prompt + "\n\nВызови инструмент make_claim_decision с финальным решением."}]
                if use_thinking else messages
            )

            main_result = await llm_client.call_tool(
                system=DECISION_SYSTEM_PROMPT,
                messages=tool_messages,
                tool=DECISION_TOOL,
                tool_name="make_claim_decision",
                max_tokens=settings.claude_decision_max_tokens,
                temperature=settings.claude_decision_temperature,
                use_thinking=use_thinking,
            )
            claude_input_tokens += main_result.input_tokens
            claude_output_tokens += main_result.output_tokens
            if main_result.reasoning:
                reasoning_parts.append(main_result.reasoning)

        except (LLMAPIError, LLMNoToolBlockError) as e:
            log.error("decision_llm_error", claim_id=str(claim_id), error=str(e))
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason=f"LLM API error: {e}",
                fraud_flags=[],
                overall_confidence=0.0,
                summary=f"Ошибка AI-анализа: {e}. Требуется ручная проверка оператором.",
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        if main_result.tool_input is None:
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason="LLM did not return tool call result",
                fraud_flags=[],
                overall_confidence=0.0,
                summary="AI не вернул структурированный ответ. Требуется ручная проверка.",
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        raw: dict[str, Any] = main_result.tool_input  # type: ignore[assignment]
        fraud_flags = await fraud_task

        # ── Шаг 26: reasoning (thinking/CoT) → audit ──────────────
        # reasoning_parts собраны выше: CoT-текст и/или thinking из main_result
        reasoning = "\n".join(p for p in reasoning_parts if p)
        reasoning = reasoning[:settings.decision_reasoning_audit_max_chars]

        # ── Калибровка confidence (Шаги 27/29) ───────────────────
        # Фактор обновляется ежедневным job-ом calibrate_confidence
        # (tasks_analytics.py) в platform.tenant_configs — читаем на каждом
        # решении; Settings кэшируется при старте процесса и не подходит.
        raw_confidence = raw.get("overall_confidence", 0.0)
        calibration_factor = await get_tenant_config_float(
            db, tenant_id, "confidence_calibration_factor",
            settings.decision_confidence_calibration_factor,
        )
        effective_confidence = max(0.0, min(1.0, raw_confidence * calibration_factor))

        # ── Шаг 21: медицинская согласованность ───────────────────
        # Несоответствие услуг диагнозам → штраф к confidence + manual_review.
        # Сознательное отклонение от спеки: НЕ в fraud_flags — они форсируют
        # FRAUD_FLAG-роутинг с priority=urgent, что чрезмерно для «МРТ при ОРВИ».
        coherence_flags: list[str] = []
        if settings.decision_coherence_check_enabled:
            _raw_flags = [str(f) for f in (raw.get("coherence_flags") or [])]
            # Штрафуем только конкретную несогласованность (например "МРТ позвоночника при ОРВИ").
            # Общие фразы "невозможно определить" — неуверенность Claude, не реальный конфликт.
            _GENERIC_PHRASES = (
                "невозможно определить",
                "невозможно рассчитать",
                "не удалось",
                "cannot determine",
                "insufficient",
                "без явного",
                "нет информации",
                "без информации",
                "отсутствует информация",
            )
            coherence_flags = [
                f for f in _raw_flags
                if not any(p in f.lower() for p in _GENERIC_PHRASES)
            ]
            if len(_raw_flags) != len(coherence_flags):
                log.info(
                    "coherence_flags_generic_filtered",
                    claim_id=str(claim_id),
                    total=len(_raw_flags),
                    specific=len(coherence_flags),
                    filtered_out=len(_raw_flags) - len(coherence_flags),
                )
        if coherence_flags:
            effective_confidence = max(
                0.0, effective_confidence - settings.decision_coherence_confidence_penalty
            )
            log.info(
                "medical_coherence_flags",
                claim_id=str(claim_id),
                flags=coherence_flags,
            )

        diagnoses = [DiagnosisDecisionSchema(**d) for d in raw.get("diagnoses", [])]
        line_items = [LineItemDecisionSchema(**li) for li in raw.get("line_items", [])]

        # ── Post-process: вычислить суммы если Claude вернул 0 при покрытых диагнозах ──
        # Причина: нет детализации чека → Claude не может распределить суммы, возвращает 0.
        # Решение: deterministic fallback — total_claimed × coverage_pct / 100.
        _covered_diagnoses = [d for d in diagnoses if d.is_covered]
        if _covered_diagnoses and all(d.approved_amount == 0.0 for d in _covered_diagnoses):
            _total_cl = extraction.event.total_claimed or 0.0
            _coverage_pct = 100.0
            if risks_limits and risks_limits.risks:
                _pr = next(
                    (r for r in risks_limits.risks if r.remaining_limit > 0),
                    risks_limits.risks[0],
                )
                if _pr.coverage_pct:
                    _coverage_pct = float(_pr.coverage_pct)
            _approved_total = round(_total_cl * _coverage_pct / 100.0, 2)
            # Обновляем итоговые суммы в raw-ответе (используются ниже в ClaimDecision)
            if not raw.get("total_approved"):
                raw["total_approved"] = _approved_total
            if not raw.get("final_payout"):
                raw["final_payout"] = _approved_total
            # Распределяем поровну между покрытыми диагнозами
            _per_diag = round(_approved_total / len(_covered_diagnoses), 2)
            for _d in _covered_diagnoses:
                _d.approved_amount = _per_diag
            # Создаём line_items если Claude их не вернул — только позиции из чеков
            if not line_items:
                _rcpt = [
                    li for li in extraction.event.line_items
                    if li.doc_source and li.doc_source.startswith("receipt")
                ]
                if _rcpt:
                    for _li in _rcpt:
                        line_items.append(LineItemDecisionSchema(
                            description=_li.description,
                            claimed_amount=_li.amount,
                            approved_amount=round(_li.amount * _coverage_pct / 100.0, 2),
                            linked_icd10=(
                                _covered_diagnoses[0].icd10_code if _covered_diagnoses else None
                            ),
                        ))
                else:
                    line_items.append(LineItemDecisionSchema(
                        description="Медицинские услуги",
                        claimed_amount=_total_cl,
                        approved_amount=_approved_total,
                        linked_icd10=(
                            _covered_diagnoses[0].icd10_code if _covered_diagnoses else None
                        ),
                    ))
            log.info(
                "amounts_computed_deterministically",
                claim_id=str(claim_id),
                total_claimed=_total_cl,
                coverage_pct=_coverage_pct,
                approved_total=_approved_total,
                num_covered=len(_covered_diagnoses),
            )

        # ── Шаг 26: второй проход для неуверенных диагнозов ───────
        # Один дополнительный узконаправленный вызов по самому спорному диагнозу.
        second_pass_applied = False
        uncertain = [
            d for d in diagnoses
            if d.confidence < settings.decision_second_pass_confidence_threshold
        ]
        if uncertain and not raw.get("requires_manual_review"):
            second_pass_applied = await _second_pass_diagnosis(
                client=llm_client,
                target=min(uncertain, key=lambda d: d.confidence),
                enriched=enriched,
                contract_chunks=contract_chunks,
                claim_id=claim_id,
                tenant_id=tenant_id,
                db=db,
            )

        # ── Применить CARVEOUT-отказы (переопределить решение Claude) ────────
        # Если диагноз попал в быстрый CARVEOUT-отказ, переопределяем решение
        for diag in diagnoses:
            if diag.icd10_code in carveout_rejections:
                diag.is_covered = False
                diag.approved_amount = 0.0
                diag.rejection_reason = carveout_rejections[diag.icd10_code]
                # Не обновляем contract_reference — CARVEOUT-причина в rejection_reason

        # ── Применить исключения по вордингу (переопределить решение Claude) ─
        # Жёсткие исключения из wording (Уровень 1) → Claude не может разрешить.
        for diag in diagnoses:
            if diag.icd10_code in wording_exclusions:
                reason, excl = wording_exclusions[diag.icd10_code]
                diag.is_covered = False
                diag.approved_amount = 0.0
                diag.rejection_reason = reason

        # ── POSITIVE LIST результаты ─────────────────────────────
        # Если услуга в POSITIVE LIST → 100% покрыта, переопределяем решение Claude
        for line_item in line_items:
            desc = line_item.description or ""
            if desc in positive_list_match:
                is_in_list, procedure_name = positive_list_match[desc]
                if is_in_list:
                    # POSITIVE LIST процедура → ВСЕГДА покрыта 100%
                    line_item.positive_list_applied = True
                    line_item.approved_amount = line_item.claimed_amount
                    line_item.linked_icd10 = None  # POSITIVE LIST не привязана к диагнозу
                    log.info(
                        "positive_list_coverage_applied",
                        claim_id=str(claim_id),
                        procedure=procedure_name,
                        claimed_amount=line_item.claimed_amount,
                    )

        all_covered = all(d.is_covered for d in diagnoses) if diagnoses else False
        any_covered = any(d.is_covered for d in diagnoses) if diagnoses else False

        # wording_manual_review: CARVEOUT-условие есть, но service_urgency неизвестна
        if wording_manual_review:
            manual_review_codes = ", ".join(wording_manual_review)
            raw["requires_manual_review"] = True
            if not raw.get("manual_review_reason"):
                raw["manual_review_reason"] = (
                    f"wording_carveout_unknown_urgency: {manual_review_codes}"
                )

        if raw.get("requires_manual_review") or fraud_flags or coherence_flags or sublimit_violations:
            status = "manual_review"
        elif all_covered:
            status = "approved"
        elif any_covered:
            status = "partial"
        else:
            status = "rejected"

        # ── Маппинг на справочники кор-системы ───────────────────
        # DiagnosID: приоритет 1 — коды Claude из extraction; приоритет 2 — OCR regex.
        # OCR regex даёт ложные срабатывания (комнаты, номера счетов вида "B30"),
        # поэтому Claude-коды проверяются первыми.
        diagnosid: str | None = None
        found_icd10_code: str | None = None
        for _diag in extraction.event.diagnoses:
            if _diag.icd10_code:
                _d = find_diagnosid(_diag.icd10_code, icd10_list)
                if _d:
                    diagnosid = _d
                    found_icd10_code = _diag.icd10_code
                    log.info(
                        "diagnosid_from_claude_extraction",
                        claim_id=str(claim_id),
                        diagnosid=diagnosid,
                        icd10_code=found_icd10_code,
                    )
                    break
        if diagnosid is None:
            diagnosid, found_icd10_code = find_diagnosid_in_ocr(ocr_texts or [], icd10_list)
            if diagnosid:
                log.info(
                    "diagnosid_from_ocr_regex",
                    claim_id=str(claim_id),
                    diagnosid=diagnosid,
                    icd10_code=found_icd10_code,
                )
        if diagnosid is None:
            log.warning(
                "diagnosid_not_found_in_docs",
                claim_id=str(claim_id),
                claude_codes=[d.icd10_code for d in extraction.event.diagnoses],
            )
            # Диагноз не найден ни в одном документе → ручная проверка обязательна
            raw["requires_manual_review"] = True
            if not raw.get("manual_review_reason"):
                raw["manual_review_reason"] = "diagnosis_not_found_in_docs"
        else:
            log.info(
                "diagnosid_found_in_ocr",
                claim_id=str(claim_id),
                diagnosid=diagnosid,
                icd10_code=found_icd10_code,
            )

        # risks_list: детерминированный матчер рисков (risk_matcher.py)
        # Если есть строки из чеков (doc_source='receipt_*') — используем только их.
        # Позиции формы 100 (анамнез, диагнозы, медикаменты) не должны попасть
        # в кор-систему с большими суммами.
        # Если чеков нет (нет doc_source или все из формы 100) — используем все позиции;
        # match_risks fallback сам вернёт total_claimed если сумм нет.
        _receipt_items = [
            li for li in extraction.event.line_items
            if li.doc_source and li.doc_source.startswith("receipt")
        ]
        _items_for_risk = _receipt_items if _receipt_items else extraction.event.line_items
        risks_list, risk_match_fallback = match_risks(
            line_items=_items_for_risk,
            risks=risks_limits.risks,
            event_date=extraction.event.date,
            form_100_text=form_100_ocr_text,
            config_kind=2,
            total_claimed=extraction.event.total_claimed or 0.0,
        )
        config_kind = 2

        # PersID уже вычислен выше (до всех early-return путей).

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

        # Причина ручной проверки: QA-выборка > ответ Claude > coherence > суб-лимиты > risk fallback
        manual_review_reason = raw.get("manual_review_reason")
        if coherence_flags and not manual_review_reason:
            manual_review_reason = "medical_coherence: " + "; ".join(coherence_flags[:3])
        if sublimit_violations and not manual_review_reason:
            manual_review_reason = "sublimit_exceeded: " + "; ".join(sublimit_violations[:3])
        if risk_match_fallback and not manual_review_reason:
            manual_review_reason = "risk_match_fallback: подобран риск без маркера свободного выбора"
        if qa_sample:
            manual_review_reason = "stochastic_qa_sample"

        decision = ClaimDecision(
            claim_id=claim_id,
            diagnoses=diagnoses,
            line_items=line_items,
            total_approved=raw.get("total_approved", 0.0),
            deductible_applied=raw.get("deductible_applied", 0.0),
            final_payout=raw.get("final_payout", 0.0),
            status=status,
            requires_manual_review=(
                raw.get("requires_manual_review", False)
                or bool(fraud_flags)
                or bool(coherence_flags)
                or bool(sublimit_violations)
                or risk_match_fallback
                or qa_sample
            ),
            manual_review_reason=manual_review_reason,
            fraud_flags=fraud_flags,
            overall_confidence=effective_confidence,
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
            "service_urgency": extraction.event.service_urgency,
            "model": settings.claude_model,
            "max_tokens": settings.claude_decision_max_tokens,
            "temperature": settings.claude_decision_temperature,
            "use_thinking": use_thinking,
            "use_cot": use_cot,
            "system_prompt_chars": len(DECISION_SYSTEM_PROMPT),
            "user_prompt_chars": len(user_prompt),
            # Полный промпт (данные заявки + договор + риски) для отладки
            "user_prompt": user_prompt,
        },
        output_data={
            "status":          decision.status,
            "final_payout":    decision.final_payout,
            "requires_manual_review": decision.requires_manual_review,
            "fraud_flags":     decision.fraud_flags,
            "carveout_rejections_count": len(carveout_rejections),
            "positive_list_matched_count": sum(1 for v in positive_list_match.values() if v[0]),
            "diagnosid":       decision.diagnosid,
            "pers_id":         decision.pers_id,
            "config_kind":     decision.config_kind,
            "summary":         decision.summary[:300],
            "qa_sample":       qa_sample,
            "input_tokens":    claude_input_tokens,
            "output_tokens":   claude_output_tokens,
            # Сырой ответ Claude (tool_use input) — до применения CARVEOUT / POSITIVE LIST / суб-лимитов
            "claude_raw_response": raw,
            # Шаги 21/23/26
            "coherence_flags": coherence_flags,
            "sublimit_violations": sublimit_violations,
            "waiting_period_check": waiting_period_note,
            "reasoning_mode": "thinking" if use_thinking else ("cot" if use_cot else "standard"),
            "reasoning": reasoning,
            "second_pass_applied": second_pass_applied,
        },
        confidence={
            "overall": decision.overall_confidence,
            # raw обязателен: калибровка считается от некалиброванного значения,
            # иначе фактор компаундился бы при каждом обновлении
            "overall_raw": raw_confidence,
            "calibration_factor": calibration_factor,
        },
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
