"""
Детерминированный матчер рисков для ClaimParsing_UNI.

Алгоритм:
1. Определить категорию услуг по тексту формы 100 (ამბულატ / სტაციონ / etc.)
2. Отфильтровать допустимые риски (правило Lite GROUP, верифицировано 2026-06-14):
   valid_risks = RiskParentId=0 (корневой) ИЛИ (RiskParentId≠0 И hasChild=1) (промежуточный).
   Листовые риски (RiskParentId≠0 И hasChild=0) НЕ передавать в ClaimParsing_UNI.
3. Для возмещения (config_kind=2) + амбулатория:
   предпочесть риски с маркерами свободного выбора (თავისუფალი არჩევანი / მიმართვის გარეშე)
4. Для каждой услуги назначить наилучший подходящий риск
5. Если точный матч не найден → использовать лучший доступный + флаг needs_manual_review
"""

from __future__ import annotations

import structlog

from core.schemas.claim import LineItem
from core.schemas.core_api import RiskInfo

log = structlog.get_logger()


# ── Маркеры категорий в тексте формы 100 ──────────────────────────

SERVICE_CATEGORY_MARKERS: dict[str, list[str]] = {
    "ambulatory": [
        "ამბულატორ",    # KA: ამბულატორია, ამბულატორიული
        "амбулатор",    # RU
        "outpatient",   # EN
        "ambulat",
    ],
    "inpatient": [
        "სტაციონარ",    # KA: სტაციონარი, სტაციონარული
        "ჰოსპიტალ",    # KA: ჰოსპიტალური
        "стационар",    # RU
        "госпитал",     # RU
        "hospital",     # EN
        "inpatient",
    ],
    "pharmacy": [
        "მედიკამენტ",   # KA
        "ფარმაც",       # KA
        "медикамент",   # RU
        "лекарств",     # RU
        "аптек",        # RU
        "medication",   # EN
        "pharmacy",
    ],
    "dental": [
        "სტომატ",       # KA
        "კბილ",         # KA
        "стоматол",     # RU
        "зуб",          # RU
        "dental",       # EN
    ],
}

# Маркеры свободного выбора / без направления в названии риска
FREE_CHOICE_MARKERS = [
    "თავისუფალი არჩევანი",    # KA: свободный выбор
    "მიმართვის გარეშე",       # KA: без направления
    "free choice",
    "without referral",
]

# Маркеры категорий в названии риска
RISK_CATEGORY_MARKERS: dict[str, list[str]] = {
    "ambulatory": [
        "ამბულატორ",    # KA
        "outpatient",   # EN
        "амбулатор",    # RU
    ],
    "inpatient": [
        "სტაციონარ",    # KA
        "ჰოსპიტალ",    # KA
        "hospital",     # EN
        "стационар",    # RU
        "госпитал",     # RU
    ],
    "pharmacy": [
        "მედიკამენტ",   # KA
        "medication",   # EN
        "медикамент",   # RU
    ],
    "dental": [
        "სტომატ",       # KA
        "dental",       # EN
        "стоматол",     # RU
    ],
}


def detect_service_category(form_100_text: str) -> str:
    """
    Определить категорию по тексту формы 100.

    Подсчитывает совпадения маркеров для каждой категории.
    Победитель — категория с наибольшим числом совпадений.
    Fallback: "ambulatory".
    """
    text_lower = form_100_text.lower()
    scores: dict[str, int] = {}

    for category, markers in SERVICE_CATEGORY_MARKERS.items():
        count = sum(1 for m in markers if m.lower() in text_lower)
        if count > 0:
            scores[category] = count

    if not scores:
        log.info("risk_matcher_category_not_detected", fallback="ambulatory")
        return "ambulatory"

    winner = max(scores, key=lambda k: scores[k])
    log.info("risk_matcher_category_detected", category=winner, scores=scores)
    return winner


def _is_free_choice(risk: RiskInfo) -> bool:
    """Содержит ли название риска маркер свободного выбора / без направления."""
    name_lower = risk.name.lower()
    return any(m.lower() in name_lower for m in FREE_CHOICE_MARKERS)


def _matches_category(risk: RiskInfo, category: str) -> bool:
    """Содержит ли название риска маркер нужной категории."""
    name_lower = risk.name.lower()
    markers = RISK_CATEGORY_MARKERS.get(category, [])
    return any(m.lower() in name_lower for m in markers)


def _select_best_risk(
    risks: list[RiskInfo],
    category: str,
    prefer_free_choice: bool,
) -> tuple[RiskInfo | None, bool]:
    """
    Выбрать допустимый риск для категории.

    Правило Lite GROUP (верифицировано 2026-06-14):
      valid = RiskParentId=0 (корневой) ИЛИ (RiskParentId≠0 И hasChild=1) (промежуточный).

    Приоритет выбора:
      1. Допустимый + категория + свободный выбор (если prefer_free_choice)
      2. Допустимый + категория (без маркера свободного выбора) → needs_manual_review
      3. Любой допустимый с ненулевым остатком → needs_manual_review

    Returns:
        (risk | None, is_exact_match)
        is_exact_match=False → fallback → нужен manual_review
    """
    # Правило: корневые (parent_risk_id=None ≡ RiskParentId=0) ИЛИ промежуточные (has_child=1)
    valid_risks = [
        r for r in risks
        if r.parent_risk_id is None   # RiskParentId=0 (корневой)
        or r.has_child == 1           # RiskParentId≠0 И hasChild=1 (промежуточный)
    ]

    category_risks = [r for r in valid_risks if _matches_category(r, category)]

    if prefer_free_choice and category_risks:
        # Приоритет 1: категория + свободный выбор
        free_choice_risks = [r for r in category_risks if _is_free_choice(r)]
        if free_choice_risks:
            with_balance = [r for r in free_choice_risks if r.remaining_limit > 0]
            chosen = (with_balance or free_choice_risks)[0]
            return chosen, True

        # Приоритет 2: категория без маркера свободного выбора
        with_balance = [r for r in category_risks if r.remaining_limit > 0]
        chosen = (with_balance or category_risks)[0]
        log.warning(
            "risk_matcher_no_free_choice_risk",
            category=category,
            fallback_risk_id=chosen.risk_id,
            fallback_risk_name=chosen.name,
        )
        return chosen, False

    if not prefer_free_choice and category_risks:
        with_balance = [r for r in category_risks if r.remaining_limit > 0]
        return (with_balance or category_risks)[0], True

    # Нет рисков нужной категории — берём любой допустимый с остатком
    with_balance = [r for r in valid_risks if r.remaining_limit > 0]
    if with_balance:
        log.warning(
            "risk_matcher_no_category_risk",
            category=category,
            fallback_risk_id=with_balance[0].risk_id,
        )
        return with_balance[0], False

    log.error("risk_matcher_no_valid_risks_available", category=category)
    return None, False


def match_risks(
    line_items: list[LineItem],
    risks: list[RiskInfo],
    event_date: str,
    form_100_text: str,
    config_kind: int = 2,
    total_claimed: float = 0.0,
) -> tuple[list[dict], bool]:
    """
    Подобрать риск для каждой услуги и построить RisksList для ClaimParsing_UNI.

    Args:
        line_items:    услуги из ExtractionResult (claimed amounts)
        risks:         список рисков из getpolicylist
        event_date:    дата события YYYY-MM-DD
        form_100_text: OCR-текст формы 100 (для определения категории)
        config_kind:   2 = акт возмещения (предпочесть свободный выбор)
        total_claimed: сумма из заявки — используется если нет детализированных line_items

    Returns:
        (risks_list, needs_manual_review)
        risks_list — список словарей для XML_DATA.RisksList (никогда не пустой если есть риски)
        needs_manual_review — True если хотя бы один риск подобран по fallback
    """
    category = detect_service_category(form_100_text)
    prefer_free_choice = (config_kind == 2)

    selected_risk, is_exact = _select_best_risk(risks, category, prefer_free_choice)
    needs_manual_review = not is_exact

    items = [li for li in line_items if li.amount > 0]

    # RisksList не должен быть пустым в ClaimParsing_UNI.
    # Если нет детализированных строк — одна запись с total_claimed.
    if not items:
        if selected_risk is None or total_claimed <= 0:
            return [], needs_manual_review
        log.info(
            "risk_matcher_fallback_total_claimed",
            risk_id=selected_risk.risk_id,
            risk_name=selected_risk.name[:60],
            total_claimed=total_claimed,
        )
        return [{
            "RiskID":      selected_risk.risk_id,
            "FinalAmount": total_claimed,
            "ServDate":    event_date,
            "serviceid":   "",
            "ServName":    "Медицинские услуги",
        }], True  # needs_manual_review=True — нет детализации

    risks_list: list[dict] = []

    for li in items:
        if selected_risk is None:
            log.warning(
                "risk_matcher_no_risk_for_service",
                description=li.description,
                category=category,
            )
            needs_manual_review = True
            continue

        risks_list.append({
            "RiskID":      selected_risk.risk_id,
            "FinalAmount": li.amount,
            "ServDate":    event_date,
            "serviceid":   "",
            "ServName":    li.description,
        })

        log.info(
            "risk_matched",
            risk_id=selected_risk.risk_id,
            risk_name=selected_risk.name[:60],
            service=li.description[:60],
            amount=li.amount,
            category=category,
            is_exact=is_exact,
            prefer_free_choice=prefer_free_choice,
        )

    return risks_list, needs_manual_review
