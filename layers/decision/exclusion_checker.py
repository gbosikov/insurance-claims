"""
Детерминированная проверка исключений по вордингу страховых условий.

Исключения загружаются из Excel в таблицу exclusion_rules.
Проверка выполняется в Уровне 1 (до Claude) в decision/service.py.

Два скоупа исключений:
  'all'    — действуют для всех застрахованных
  'family' — дополнительно для членов семьи (CardNumber суффикс /2, /3, /4)

Тип застрахованного определяется из CardNumber:
  "UNI 700003/1" → 'employee'
  "UNI 700003/2" → 'family'

CARVEOUT: если exclusion_rule.carveout_conditions непустой,
исключение НЕ применяется когда service_urgency из заявки
входит в список условий. При неизвестном service_urgency → manual_review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.exclusion import ExclusionRule

log = structlog.get_logger()

# Паттерн: letter + 2+ цифры + опционально .subcategory
_ICD10_RE = re.compile(r"^([A-Za-z])(\d{1,3})(?:\.(.*))?$")


# ── Тип застрахованного из CardNumber ─────────────────────────────


def get_insured_type(card_number: str) -> str:
    """
    Определить тип застрахованного по суффиксу CardNumber.

    '/1' или нет суффикса → 'employee'
    '/2', '/3', '/4'       → 'family'
    """
    if not card_number:
        return "employee"
    parts = card_number.rsplit("/", 1)
    if len(parts) == 2 and parts[1].strip() in ("2", "3", "4"):
        return "family"
    return "employee"


# ── ICD-10 range matcher ───────────────────────────────────────────


def _parse_icd10(code: str) -> tuple[str, int, str] | None:
    """Разобрать ICD-10 код в (letter, number, subcategory). None если некорректный."""
    m = _ICD10_RE.match(code.strip())
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2)), (m.group(3) or "")


def icd10_matches(code: str, range_spec: str) -> bool:
    """
    Проверить, входит ли ICD-10 код в диапазон/паттерн.

    Примеры:
      icd10_matches("F10.3", "F00-F99") → True
      icd10_matches("N18.5", "N18.0-N18.9") → True
      icd10_matches("N18",   "N18")          → True  (точное совпадение)
      icd10_matches("N18.0", "N18")          → True  (подкатегория N18)
      icd10_matches("N19",   "N18")          → False
      icd10_matches("C34.1", "C00-C97")      → True
    """
    # Нормализация: en-dash/em-dash → ASCII дефис
    spec = range_spec.strip().replace("–", "-").replace("—", "-")
    code_up = code.strip().upper()
    spec_up = spec.upper()

    if "-" not in spec_up:
        # Префикс-матч: code == spec ИЛИ code начинается с spec + "."
        return code_up == spec_up or code_up.startswith(spec_up + ".")

    # Диапазон: разбить на start и end
    raw_start, raw_end = spec_up.split("-", 1)
    start = _parse_icd10(raw_start)
    end = _parse_icd10(raw_end)
    target = _parse_icd10(code_up)

    if target is None or start is None or end is None:
        # Fallback: лексикографическое сравнение
        return raw_start <= code_up <= raw_end

    c_let, c_num, c_sub = target
    s_let, s_num, s_sub = start
    e_let, e_num, e_sub = end

    # Буква вне диапазона — не совпадает
    if not (s_let <= c_let <= e_let):
        return False

    # Один и тот же диапазон букв (типичный случай: F00-F99, N18.0-N18.9)
    if s_let == e_let == c_let:
        if c_num < s_num or c_num > e_num:
            return False
        # Если на границе — проверяем подкатегорию
        if c_num == s_num and s_sub and c_sub < s_sub:
            return False
        if c_num == e_num and e_sub and c_sub > e_sub:
            return False
        return True

    # Разные буквы: начальная граница
    if c_let == s_let and c_num < s_num:
        return False
    # Конечная граница
    if c_let == e_let and c_num > e_num:
        return False
    return True


# ── ExclusionResult ───────────────────────────────────────────────


@dataclass
class ExclusionResult:
    """Результат проверки исключения."""
    rule_id: UUID
    description: str
    scope: str                         # 'all' | 'family'
    carveout_conditions: list[str]     # [] = нет условий → безусловное исключение


def apply_wording_carveout(
    excl: ExclusionResult,
    service_urgency: str | None,
) -> tuple[bool, str | None]:
    """
    Проверить применяется ли carveout из правила вординга.

    Возвращает:
      (is_excluded, reason_or_none)

    True  = исключение применяется (диагноз не покрыт)
    False = carveout снимает исключение (диагноз покрыт)
    """
    if not excl.carveout_conditions:
        # Безусловное исключение
        return True, f"Исключено по условиям страхования: «{excl.description}»"

    if service_urgency is None:
        # CARVEOUT зависит от срочности, но срочность неизвестна → manual_review
        # Возвращаем False — не отказываем детерминированно, решение даст оператор
        return False, None

    if service_urgency in excl.carveout_conditions:
        # Carveout применяется — не исключаем
        return False, None

    # Срочность не совпала с условием carveout → исключение применяется
    conditions_str = ", ".join(excl.carveout_conditions)
    return True, (
        f"Исключено по условиям страхования: «{excl.description}». "
        f"Исключение не снимается (carveout: {conditions_str}; "
        f"фактически: service_urgency={service_urgency})"
    )


# ── Основная функция проверки ─────────────────────────────────────


async def check_exclusions(
    icd10_code: str,
    insured_type: str,
    tenant_id: UUID,
    db: AsyncSession,
) -> ExclusionResult | None:
    """
    Проверить ICD-10 код по таблице exclusion_rules.

    Возвращает первое совпавшее правило или None если исключений нет.
    Скоупы: всегда проверяется 'all'; для члена семьи добавляется 'family'.
    """
    scopes = ["all"]
    if insured_type == "family":
        scopes.append("family")

    stmt = select(ExclusionRule).where(
        ExclusionRule.tenant_id == tenant_id,
        ExclusionRule.scope.in_(scopes),
    )
    result = await db.execute(stmt)
    rules = result.scalars().all()

    for rule in rules:
        codes = rule.icd10_codes or []
        if any(icd10_matches(icd10_code, pat) for pat in codes):
            log.info(
                "wording_exclusion_matched",
                icd10_code=icd10_code,
                rule_id=str(rule.id),
                scope=rule.scope,
                carveout_conditions=rule.carveout_conditions,
            )
            return ExclusionResult(
                rule_id=rule.id,
                description=rule.description,
                scope=rule.scope,
                carveout_conditions=list(rule.carveout_conditions or []),
            )

    return None
