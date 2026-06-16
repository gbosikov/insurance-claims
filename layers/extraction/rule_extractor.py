"""
Слой 4 — Rule-based Extraction (альтернатива Claude API).

Детерминированное извлечение структурированных данных из OCR-текста:
  - personal_id: regex 11-digit (Georgian format)
  - icd10_code:  regex [A-Z]\\d{1,2}(\\.\\d{1,2})?
  - icd10_description: lookup в локальной таблице icd10_diagnoses
  - amounts: regex число + GEL/₾/ლ
  - dates: regex DD.MM.YYYY / YYYY-MM-DD
  - full_name: keyword-context (ФИО / სახელი / Name:) + Georgian Unicode fallback
  - institution: keyword-context (კლინიკა / клиника / clinic) + header lines
  - service_urgency: keyword detection

Включается через settings.extraction_use_rules = True.
ExtractionResult возвращается в том же формате что и Claude-версия —
cross_validate() и downstream pipeline не меняются.

Аудит-лог пишется в extract_claim_data() в service.py (не здесь) —
оба пути (Claude и rules) попадают в одну запись.
"""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.models.claim import DocType
from core.schemas.claim import (
    CrossDocForm100,
    CrossDocIdDocument,
    CrossDocReceipt,
    CrossDocReceiptLineItem,
    CrossDocumentData,
    DiagnoisItem,
    EventData,
    ExtractionResult,
    InsuredData,
    LineItem,
)
from layers.ocr.service import OCRResult

log = structlog.get_logger()

PROMPT_VERSION = "extraction/rules/v1.0.0"

# ── Regex-константы ────────────────────────────────────────────────

# Личный номер: 11 цифр (Грузия). Не часть более длинного числа.
_PID_RE = re.compile(r"(?<!\d)(\d{11})(?!\d)", re.UNICODE)
# С явной меткой → более высокий confidence
_PID_CTX_RE = re.compile(
    r"(?:personal\s*(?:id|number|no\.?)|პირადი\s*ნომ(?:ერი)?|личный\s*номер|id\s*number)[:\s#]*(\d{9,11})",
    re.IGNORECASE | re.UNICODE,
)

# Коды МКБ-10: буква + 1-2 цифры + опциональная точка и 1-2 цифры
_ICD10_RE = re.compile(r"\b([A-Z]\d{1,2}(?:\.\d{1,2})?)\b", re.UNICODE)
_ICD10_EXCLUSIONS = frozenset({
    "GEL", "OK", "ID", "RU", "KA", "EN", "PDF", "OCR",
    "No", "Nr", "Tel", "Fax", "Web",
})

# Диагностический контекст: метки диагноза на трёх языках.
# Коды найденные в окне после метки получают conf=0.98 (Уровень 1).
# Коды вне любого контекста — Уровень 2 с пониженным conf.
#
# Захватываемое окно: остаток строки метки + опционально следующая строка.
# Это покрывает два формата:
#   "Диагноз: J06.9 ..."     ← код на той же строке
#   "დიაგნოზი:\nJ06.9 ..."  ← код на следующей строке
_ICD10_CTX_RE = re.compile(
    r"(?:"
    # Georgian
    r"დიაგნოზ[^\n:]{0,20}:|"           # "დიაგნოზი:", "ძირითადი დიაგნოზი:"
    r"ძირითადი\s+დიაგნოზ[^\n:]{0,10}:|"
    # Russian
    r"диагноз[^\n:]{0,30}:|"            # "Диагноз:", "Диагноз 1:", "Диагноз основной:"
    r"МКБ(?:[\s\-]*10)?[^\n:]{0,10}:|"  # "МКБ:", "МКБ-10:"
    # English
    r"ICD(?:[\s\-]*10)?[^\n:]{0,10}:|"  # "ICD:", "ICD-10:"
    r"diagnosis[^\n:]{0,20}:|"          # "Diagnosis:", "Primary Diagnosis:"
    r"codes?\s*:|"                       # "Code:", "Codes:"
    # Georgian for code
    r"კოდ[^\n:]{0,10}:"                 # "კოდი:", "კოდები:"
    r")"
    r"\s*([^\n]{0,200}(?:\n[^\n]{0,100})?)",  # строка метки + опционально следующая
    re.IGNORECASE | re.UNICODE,
)

# Даты
_DATE_DMY = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# Контекст даты рождения
_BIRTH_CTX_RE = re.compile(
    r"(?:დაბად(?:ების\s*თარიღი)?|дата\s+рождени(?:я)?|date\s+of\s+birth|born|birth\s+date)[:\s]*",
    re.IGNORECASE | re.UNICODE,
)

# Контекст даты обращения/события
_EVENT_DATE_CTX_RE = re.compile(
    r"(?:მომსახ(?:ურე)?(?:ბის\s*თარ)?|дата\s+(?:обращени(?:я)?|события|визита)|"
    r"date\s+(?:of\s+)?(?:visit|service|admission)|visit\s+date|service\s+date|"
    r"date\s*:)[:\s]*",
    re.IGNORECASE | re.UNICODE,
)

# Контекст ФИО
_NAME_CTX_RE = re.compile(
    r"(?:სახელი(?:\s*/?\s*გვარი)?|ФИО|Фамилия(?:,\s*Имя)?|full\s+name|patient\s+name|"
    r"პაციენტ(?:ის\s*(?:სახელი|გვარი))?|name\s*:)[:\s]*",
    re.IGNORECASE | re.UNICODE,
)

# Грузинские символы (Unicode block Georgian: U+10D0–U+10FF)
_GEORGIAN_WORD_RE = re.compile(r"[ა-ჿ]{2,}", re.UNICODE)

# Суммы
_AMOUNT_TOTAL_RE = re.compile(
    r"(?:სულ(?:\s*გადასახდელი)?|итого|к\s*оплате|total|გადასახდ|amount\s*due)[:\s]*"
    r"(\d+(?:[.,]\d{1,2})?)\s*(?:GEL|₾|ლ(?:არი)?)",
    re.IGNORECASE | re.UNICODE,
)
_AMOUNT_LINE_RE = re.compile(
    r"(\d+(?:[.,]\d{1,2})?)\s*(?:GEL|₾|ლ(?:არი)?)",
    re.UNICODE,
)
# Строки-итоги которые не нужно включать в line_items.
# ВАЖНО: \b (word boundary) обязателен для სულ — без него срабатывает
# внутри "კონსულტაცია" (Georgian: კონ-სულ-ტ → ложный match).
_TOTAL_LINE_RE = re.compile(
    r"(?:\bსულ\b|\bитого\b|к\s*оплате|\btotal\b|\bგადასახდ)",
    re.IGNORECASE | re.UNICODE,
)

# «Числовой мусор» перед суммой — цифры, пробелы, разделители.
# Такая строка не является названием услуги: это столбец количества или единицы.
# Примеры: "1", "2  3", "1×45", "45,"
# НЕ матчит строки с буквами: "კონსულტაცია 1", "Консультация".
_NUMERIC_JUNK_RE = re.compile(r"^[\d\s×xX.,;:]+$")

# Учреждение
_INSTITUTION_CTX_RE = re.compile(
    r"(?:კლინიკ(?:ა|ური)|ჰოსპიტ(?:ალი)?|hospital|clinic|клиник[аи]|"
    r"медицинск(?:ий|ая|ое)\s*(?:центр|учреждение)|medical\s*(?:center|centre))[:\s]*([^\n]{2,60})",
    re.IGNORECASE | re.UNICODE,
)
# Метки учреждения с явным двоеточием (приоритет перед header-fallback).
# ВАЖНО: [ \t:]+ а не [:\s]+ — чтобы не пересекать перевод строки.
# OCR часто ставит метку ("ორგანიზაცია") и значение на разных строках.
_INSTITUTION_LABEL_RE = re.compile(
    r"(?:დაწესებულება|ორგანიზაცია|სამედიცინო\s+დაწესებულება|"
    r"учреждение|организация|наименование\s+учреждения|"
    r"institution|organization|facility)[ \t:]+([^\n]{3,80})",
    re.IGNORECASE | re.UNICODE,
)
# Ключевые слова учреждений для header-fallback
_INSTITUTION_HEADER_RE = re.compile(
    r"(?:medical|clinic|center|centre|hospital|კლინიკა|ჰოსპიტ|შპს|"
    r"медицинск|клиник|центр|больниц)",
    re.IGNORECASE | re.UNICODE,
)
# Строки-заголовки форм — НЕ являются названием клиники
_FORM_TITLE_EXCLUDE_RE = re.compile(
    r"(?:ფორმა\s*(?:№|#|n\b)|form\s*(?:no|№|#|\d)|документац|documentation|"
    r"დოკუმენტაცია|სამედიცინო\s+დოკუმენტ|მინისტრ|ministry|министерств)",
    re.IGNORECASE | re.UNICODE,
)

# Срочность
_URGENT_RE = re.compile(
    r"გადაუდ(?:ებელი)?|სასწრაფო|срочн(?:ый|ая|ое)|неотложн|urgent|emergency",
    re.IGNORECASE | re.UNICODE,
)
_DIAGNOSTIC_RE = re.compile(
    r"დიაგნოსტ(?:იკ)?|პირველადი\s+(?:დიაგ|გამ)|სკრინინგ|диагностик|исследован|"
    r"лаборатор|diagnostic|screening",
    re.IGNORECASE | re.UNICODE,
)


# ── Вспомогательные функции ───────────────────────────────────────

def _normalize_amount(raw: str) -> float:
    """'150,50' или '150.50' → 150.5."""
    return float(raw.replace(",", "."))


def _try_parse_date(day: int, month: int, year: int) -> str | None:
    """Попытка разобрать дату, None при невалидных значениях."""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _extract_dates_with_context(text: str) -> list[tuple[str, bool]]:
    """
    Найти все даты в тексте.
    Возвращает [(iso_date, is_birth_date_context), ...]
    """
    results: list[tuple[str, bool]] = []

    # DD.MM.YYYY и DD/MM/YYYY
    for m in _DATE_DMY.finditer(text):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 1900 or year > 2035:
            continue
        iso = _try_parse_date(day, month, year)
        if iso is None:
            continue
        ctx = text[max(0, m.start() - 80):m.start()]
        is_birth = bool(_BIRTH_CTX_RE.search(ctx))
        results.append((iso, is_birth))

    # YYYY-MM-DD
    for m in _DATE_ISO.finditer(text):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 1900 or year > 2035:
            continue
        iso = _try_parse_date(day, month, year)
        if iso is None:
            continue
        ctx = text[max(0, m.start() - 80):m.start()]
        is_birth = bool(_BIRTH_CTX_RE.search(ctx))
        results.append((iso, is_birth))

    return results


def _parse_date_after_pos(text: str, pos: int) -> str | None:
    """Найти и распарсить первую дату в тексте начиная с позиции pos (следующие 20 символов)."""
    window = text[pos:pos + 20]
    m = _DATE_DMY.search(window)
    if m:
        return _try_parse_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _DATE_ISO.search(window)
    if m:
        return _try_parse_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


# ── Публичные извлекающие функции ─────────────────────────────────

def _extract_personal_id(
    ocr_results: list[OCRResult],
) -> tuple[str | None, float]:
    """
    Извлечь личный номер.
    Приоритет: ID-документ > форма 100; контекстный match > простой regex.
    """
    ordered = sorted(ocr_results, key=lambda r: r.doc_type == DocType.ID_DOCUMENT, reverse=True)

    # Поиск с меткой (высший приоритет)
    for ocr in ordered:
        m = _PID_CTX_RE.search(ocr.full_text)
        if m:
            return m.group(1), 0.98

    # Простой 11-значный regex
    for ocr in ordered:
        matches = _PID_RE.findall(ocr.full_text)
        if matches:
            return matches[0], 0.95

    return None, 0.0


def _extract_birth_date(ocr_results: list[OCRResult]) -> tuple[str | None, float]:
    """Извлечь дату рождения из ID-документов."""
    id_docs = [r for r in ocr_results if r.doc_type == DocType.ID_DOCUMENT]
    sources = id_docs or ocr_results

    for ocr in sources:
        # Контекстный поиск — явная метка
        m = _BIRTH_CTX_RE.search(ocr.full_text)
        if m:
            iso = _parse_date_after_pos(ocr.full_text, m.end())
            if iso:
                year = int(iso[:4])
                if 1900 <= year <= 2010:
                    return iso, 0.92

        # Fallback: дата с is_birth=True из любого места документа
        for iso, is_birth in _extract_dates_with_context(ocr.full_text):
            if is_birth:
                year = int(iso[:4])
                if 1900 <= year <= 2010:
                    return iso, 0.88

    return None, 0.0


def _extract_full_name(ocr_results: list[OCRResult]) -> tuple[str | None, float]:
    """
    Извлечь ФИО.
    1. Явная метка (ФИО / სახელი / Name) → строка после метки (conf=0.85)
    2. Fallback для ID-документов: Georgian Unicode слова (conf=0.60)
    """
    ordered = sorted(ocr_results, key=lambda r: r.doc_type == DocType.ID_DOCUMENT, reverse=True)

    for ocr in ordered:
        m = _NAME_CTX_RE.search(ocr.full_text)
        if m:
            rest = ocr.full_text[m.end():m.end() + 100].strip()
            line = rest.split("\n")[0].strip()
            line = re.sub(r"^[\d\s\-_:,./]+", "", line).strip()
            if line and len(line) >= 3:
                return line, 0.85

    # Fallback: Georgian Unicode в ID-документе
    id_docs = [r for r in ocr_results if r.doc_type == DocType.ID_DOCUMENT]
    for ocr in id_docs:
        words = _GEORGIAN_WORD_RE.findall(ocr.full_text[:300])
        if len(words) >= 2:
            return " ".join(words[:3]), 0.60

    return None, 0.0


def _extract_event_date(ocr_results: list[OCRResult]) -> tuple[str | None, float]:
    """
    Извлечь дату события (обращения/услуги).
    Предпочитает форму 100, затем чеки. Исключает birth_date контексты.
    """
    ordered = sorted(
        ocr_results,
        key=lambda r: (r.doc_type == DocType.FORM_100, r.doc_type == DocType.RECEIPT),
        reverse=True,
    )

    # Попытка 1: явный контекст события
    for ocr in ordered:
        m_ctx = _EVENT_DATE_CTX_RE.search(ocr.full_text)
        if m_ctx:
            iso = _parse_date_after_pos(ocr.full_text, m_ctx.end())
            if iso:
                year = int(iso[:4])
                if 2015 <= year <= 2035:
                    return iso, 0.95

    # Попытка 2: любая дата без birth_date контекста, год 2015-2035
    for ocr in ordered:
        for iso, is_birth in _extract_dates_with_context(ocr.full_text):
            if not is_birth:
                year = int(iso[:4])
                if 2015 <= year <= 2035:
                    return iso, 0.88

    return None, 0.0


def _extract_institution(ocr_results: list[OCRResult]) -> str | None:
    """
    Извлечь название медицинского учреждения.
    1. Явная метка "დაწესებულება:" / "учреждение:" → текст после метки
    2. Явная метка "კლინიკა:" / "clinic:" → текст после метки
    3. Строки со словами შპს/კლინიკა/hospital без заголовков форм
    4. Первые строки формы 100 (с исключением заголовков форм)
    """
    ordered = sorted(
        ocr_results,
        key=lambda r: (r.doc_type == DocType.FORM_100, r.doc_type == DocType.RECEIPT),
        reverse=True,
    )

    # Шаг 1: явные метки типа "დაწესებულება: Название"
    for ocr in ordered:
        m = _INSTITUTION_LABEL_RE.search(ocr.full_text)
        if m:
            name = m.group(1).strip().rstrip(".,;:")
            if len(name) >= 3 and not _FORM_TITLE_EXCLUDE_RE.search(name):
                return name[:80]

    # Шаг 2: явные метки типа "კლინიკა: Название"
    for ocr in ordered:
        lines = ocr.full_text.split("\n")
        for line in lines:
            stripped = line.strip()
            m = re.match(
                r"(?:კლინიკ(?:ა|ური)|hospital|clinic|клиник[аи])[:\s]+(.+)",
                stripped, re.IGNORECASE | re.UNICODE,
            )
            if m:
                name = m.group(1).strip().rstrip(".,;:")
                if len(name) >= 3 and not _FORM_TITLE_EXCLUDE_RE.search(name):
                    return name[:80]

    # Шаг 3: строки с ключевыми словами учреждений (первые 15 строк), исключая заголовки форм.
    # Собираем ВСЕ кандидаты из всех документов и возвращаем САМЫЙ ДЛИННЫЙ —
    # усечённые строки (напр. 'შпს " თბი') не должны побеждать полные имена.
    _header_candidates: list[str] = []
    for ocr in ordered:
        lines = [l.strip() for l in ocr.full_text.split("\n") if l.strip()]
        for line in lines[:15]:
            if (_INSTITUTION_HEADER_RE.search(line)
                    and len(line) >= 5
                    and not _FORM_TITLE_EXCLUDE_RE.search(line)):
                _header_candidates.append(line[:80])
    if _header_candidates:
        return max(_header_candidates, key=len)

    # Шаг 4: первые строки формы 100 (строго без заголовков форм)
    form_100 = [r for r in ocr_results if r.doc_type == DocType.FORM_100]
    for ocr in form_100:
        lines = [l.strip() for l in ocr.full_text.split("\n") if l.strip()]
        for line in lines[:5]:
            if (len(line) >= 5
                    and re.search(r"[A-Za-zა-ჿА-яЁё]", line, re.UNICODE)
                    and not _DATE_DMY.fullmatch(line)
                    and not _PID_RE.fullmatch(line)
                    and not _FORM_TITLE_EXCLUDE_RE.search(line)):
                return line[:80]

    return None


def _extract_icd10_codes(ocr_results: list[OCRResult]) -> list[tuple[str, float]]:
    """
    Извлечь коды МКБ-10. Двухуровневый поиск:

    Уровень 1 — диагностический контекст (_ICD10_CTX_RE):
      Коды найденные в окне после метки "Диагноз:" / "დიაგნოზი:" / "ICD-10:" → conf=0.98.
      Это настоящие диагнозы, подтверждённые структурой документа.

    Уровень 2 — полный документ:
      Коды вне любого диагностического контекста → conf=0.90/0.80/0.65.
      Пониженный conf отражает неопределённость: "A4", "B6", "G1" — частые
      ложные совпадения с форматом [A-Z][0-9] вне диагностического раздела.
      Код уже найденный в Уровне 1 не перезаписывается.

    Источник — форма 100 (приоритет), затем остальные документы.
    """
    seen: dict[str, float] = {}

    ordered = sorted(
        ocr_results,
        key=lambda r: r.doc_type == DocType.FORM_100,
        reverse=True,
    )

    for ocr in ordered:
        text = ocr.full_text

        # Уровень 1: коды внутри диагностического окна → высокое доверие
        for ctx_match in _ICD10_CTX_RE.finditer(text):
            window = ctx_match.group(1).upper()
            for m in _ICD10_RE.finditer(window):
                code = m.group(1)
                if code not in _ICD10_EXCLUSIONS:
                    if seen.get(code, 0.0) < 0.98:
                        seen[code] = 0.98

        # Уровень 2: весь документ, но не перезаписывать коды из контекста
        text_upper = text.upper()
        for m in _ICD10_RE.finditer(text_upper):
            code = m.group(1)
            if code in _ICD10_EXCLUSIONS or code in seen:
                continue
            conf = 0.90 if "." in code else (0.80 if len(code) >= 3 else 0.65)
            seen[code] = conf

    return list(seen.items())


async def _extract_diagnoses(
    ocr_results: list[OCRResult],
    db: AsyncSession,
) -> tuple[list[DiagnoisItem], list[str]]:
    """
    Извлечь диагнозы: коды через regex + трёхступенчатая проверка по локальной БД.

    Возвращает (diagnoses, validation_flags).

    Шаги для каждого кода:
    1. Точное совпадение через enrich_diagnosis() — если name_r/name_e/name_g заполнены.
    2. Prefix-поиск: J06.9 → SELECT WHERE extcod LIKE 'J06%'.
       Покрывает случай когда OCR потерял десятичную часть кода или добавил лишний суффикс.
    3. Не найден нигде → флаг "icd10_not_in_db:{code}", код НЕ добавляется в результат.

    Если все найденные коды оказались ложными → флаг "no_valid_icd10_codes".
    """
    from layers.decision.icd10_enricher import enrich_diagnosis
    from sqlalchemy import select as sa_select
    from core.models.icd10 import ICD10Diagnosis

    codes = _extract_icd10_codes(ocr_results)
    result: list[DiagnoisItem] = []
    validation_flags: list[str] = []

    for code, _conf in codes:
        # Шаг 1: точное совпадение
        try:
            enriched = await enrich_diagnosis(code, db)
        except Exception:
            enriched = None

        if enriched and (enriched.name_r or enriched.name_e or enriched.name_g):
            description = enriched.name_r or enriched.name_e or enriched.name_g
            result.append(DiagnoisItem(icd10_code=code, description=description))
            continue

        # Шаг 2: prefix-поиск (J06.9 → J06%)
        prefix = code.split(".")[0]
        try:
            prefix_res = await db.execute(
                sa_select(ICD10Diagnosis)
                .where(
                    ICD10Diagnosis.extcod.like(f"{prefix}%"),
                    ICD10Diagnosis.is_available.is_(True),
                )
                .limit(1)
            )
            row = prefix_res.scalar_one_or_none()
        except Exception:
            row = None

        if row:
            description = row.name_r or row.name_e or row.name_g or code
            result.append(DiagnoisItem(icd10_code=code, description=description))
        else:
            # Шаг 3: код не найден — ложный позитив из regex
            validation_flags.append(f"icd10_not_in_db:{code}")
            log.warning("icd10_code_not_in_local_db", code=code, prefix=prefix)

    if not result and codes:
        validation_flags.append("no_valid_icd10_codes")

    return result, validation_flags


def _extract_line_items(ocr_results: list[OCRResult]) -> list[LineItem]:
    """
    Извлечь строки услуг из чеков.
    Каждая строка с суммой (GEL/₾/ლ) → LineItem, кроме строк-итогов.

    Двухстрочное окно (Улучшение #2):
    OCR из PDF-чека часто разбивает запись на две строки:
      "კონსულტაცია პირველადი"   ← строка i-1
      "45.00 GEL"                ← строка i (сумма без описания)
    Также фильтрует «числовой мусор»: если перед суммой стоит только
    цифра/количество (столбец qty), берём описание из предыдущей строки.

    Защита от двойного использования: used_as_desc отслеживает
    индексы строк уже взятых как description, чтобы не атрибутировать
    одно название нескольким суммам подряд.
    """
    items: list[LineItem] = []
    receipt_idx = 0

    for ocr in ocr_results:
        if ocr.doc_type != DocType.RECEIPT:
            continue
        receipt_idx += 1
        doc_source = f"receipt_{receipt_idx}"

        lines = [ln.strip() for ln in ocr.full_text.split("\n")]
        used_as_desc: set[int] = set()  # индексы строк, уже использованных как description

        for i, line in enumerate(lines):
            if not line or _TOTAL_LINE_RE.search(line):
                continue

            m = _AMOUNT_LINE_RE.search(line)
            if not m:
                continue

            amount = _normalize_amount(m.group(1))
            description = line[:m.start()].strip().rstrip("-–|:,. ")

            if description and not _NUMERIC_JUNK_RE.match(description):
                # Обычный случай: описание и сумма на одной строке
                items.append(LineItem(
                    description=description,
                    amount=amount,
                    doc_source=doc_source,
                ))
            else:
                # Описание пустое или числовой мусор — смотрим назад (до 2 строк)
                prev_idx = next(
                    (
                        j for j in range(i - 1, max(i - 3, -1), -1)
                        if lines[j]
                        and j not in used_as_desc
                        and not _AMOUNT_LINE_RE.search(lines[j])
                        and not _TOTAL_LINE_RE.search(lines[j])
                        and not _NUMERIC_JUNK_RE.match(lines[j])
                        and len(lines[j]) >= 3
                    ),
                    None,
                )
                if prev_idx is not None:
                    used_as_desc.add(prev_idx)
                    items.append(LineItem(
                        description=lines[prev_idx].rstrip("-–|:,. "),
                        amount=amount,
                        doc_source=doc_source,
                    ))

    return items


def _extract_total(
    ocr_results: list[OCRResult],
    line_items: list[LineItem],
) -> tuple[float, float]:
    """
    Извлечь итоговую сумму.
    1. Ключевое слово (სულ/итого/total) + число + GEL → conf=0.95
    2. Сумма line_items → conf=0.80
    3. Любая сумма с GEL → conf=0.60
    """
    ordered = sorted(ocr_results, key=lambda r: r.doc_type == DocType.RECEIPT, reverse=True)

    for ocr in ordered:
        m = _AMOUNT_TOTAL_RE.search(ocr.full_text)
        if m:
            return _normalize_amount(m.group(1)), 0.95

    if line_items:
        return sum(li.amount for li in line_items), 0.80

    for ocr in ordered:
        m = _AMOUNT_LINE_RE.search(ocr.full_text)
        if m:
            return _normalize_amount(m.group(1)), 0.60

    return 0.0, 0.0


def _detect_urgency(ocr_results: list[OCRResult]) -> str | None:
    """Определить тип услуги по ключевым словам. Urgent > Diagnostic."""
    has_urgent = False
    has_diagnostic = False

    for ocr in ocr_results:
        if _URGENT_RE.search(ocr.full_text):
            has_urgent = True
        if _DIAGNOSTIC_RE.search(ocr.full_text):
            has_diagnostic = True

    if has_urgent:
        return "urgent"
    if has_diagnostic:
        return "diagnostic"
    return None


def _compute_confidence(
    fields: dict,
) -> tuple[float, list[str]]:
    """
    Вычислить итоговый confidence и список флагов.

    fields: dict с ключами personal_id, full_name, event_date, institution,
    diagnoses, total. Каждое значение — (value | None, conf_float).
    diagnoses может быть list[tuple[str, float]] или list[DiagnoisItem].
    """
    confidence = 1.0
    flags: list[str] = []

    pid, _pid_conf = fields.get("personal_id", (None, 0.0))
    if pid is None:
        confidence -= 0.25
        flags.append("missing_personal_id")

    name, name_conf = fields.get("full_name", (None, 0.0))
    if name is None:
        confidence -= 0.15
        flags.append("missing_name")
    elif name_conf < 0.70:
        confidence -= 0.05
        flags.append("low_confidence_name")

    event_date, _ = fields.get("event_date", (None, 0.0))
    if event_date is None:
        confidence -= 0.20
        flags.append("missing_date")

    diagnoses = fields.get("diagnoses", [])
    if not diagnoses:
        confidence -= 0.20
        flags.append("missing_diagnosis")

    total, _total_conf = fields.get("total", (0.0, 0.0))
    if not total:
        confidence -= 0.10
        flags.append("missing_amount")

    institution, _ = fields.get("institution", (None, 0.0))
    if institution is None:
        confidence -= 0.05

    return max(0.0, round(confidence, 3)), flags


def _build_cross_document(
    form_100: list[OCRResult],
    id_docs: list[OCRResult],
    receipts: list[OCRResult],
    diagnoses: list[DiagnoisItem],
    institution: str | None,
) -> CrossDocumentData:
    """Построить CrossDocumentData из правило-извлечённых данных."""

    cross_form: CrossDocForm100 | None = None
    if form_100:
        f = form_100[0]
        name, _ = _extract_full_name([f])
        bd, _ = _extract_birth_date([f])
        ed, _ = _extract_event_date([f])
        total, _ = _extract_total([f], [])
        cross_form = CrossDocForm100(
            full_name=name,
            birth_date=bd,
            date=ed,
            institution=institution,
            diagnoses=[d.icd10_code for d in diagnoses],
            services=[],
            total=total if total else None,
        )

    cross_id: CrossDocIdDocument | None = None
    if id_docs:
        d = id_docs[0]
        name, _ = _extract_full_name([d])
        bd, _ = _extract_birth_date([d])
        pid, _ = _extract_personal_id([d])
        cross_id = CrossDocIdDocument(
            full_name=name,
            birth_date=bd,
            personal_id=pid,
        )

    cross_receipt: CrossDocReceipt | None = None
    if receipts:
        r0 = receipts[0]
        rd, _ = _extract_event_date([r0])
        ri = _extract_institution([r0])
        ri_total, _ = _extract_total(receipts, [])
        r_items: list[CrossDocReceiptLineItem] = []
        for r_idx, rec in enumerate(receipts, start=1):
            for li in _extract_line_items([rec]):
                r_items.append(CrossDocReceiptLineItem(
                    description=li.description,
                    amount=li.amount,
                    receipt_number=r_idx,
                ))
        cross_receipt = CrossDocReceipt(
            date=rd,
            institution=ri,
            diagnoses=[d.icd10_code for d in diagnoses],
            line_items=r_items,
            total=ri_total if ri_total else None,
        )

    return CrossDocumentData(
        form_100=cross_form,
        id_document=cross_id,
        receipt=cross_receipt,
    )


# ── Главная функция ───────────────────────────────────────────────

async def extract_by_rules(
    ocr_results: list[OCRResult],
    claim_id: UUID,
    db: AsyncSession,
) -> ExtractionResult:
    """
    Детерминированное извлечение из OCR-текста (без Claude API).

    Возвращает ExtractionResult в том же формате что и Claude-версия.
    При confidence < settings.extraction_rules_min_confidence добавляет
    флаг 'rules_extraction_low_confidence' → decision engine → manual_review.

    Аудит-лог пишется вызывающим кодом (extract_claim_data в service.py).
    """
    settings = get_settings()

    form_100 = [r for r in ocr_results if r.doc_type == DocType.FORM_100]
    id_docs  = [r for r in ocr_results if r.doc_type == DocType.ID_DOCUMENT]
    receipts = [r for r in ocr_results if r.doc_type == DocType.RECEIPT]

    personal_id, pid_conf  = _extract_personal_id(id_docs or form_100 or ocr_results)
    birth_date,  bd_conf   = _extract_birth_date(id_docs or ocr_results)
    full_name,   name_conf = _extract_full_name(id_docs or form_100 or ocr_results)
    event_date,  ed_conf   = _extract_event_date(form_100 or receipts or ocr_results)
    institution            = _extract_institution(form_100 or receipts or ocr_results)
    diagnoses, icd10_flags = await _extract_diagnoses(form_100 or ocr_results, db)
    line_items             = _extract_line_items(receipts)
    total, total_conf      = _extract_total(receipts or form_100 or ocr_results, line_items)
    urgency                = _detect_urgency(form_100 or ocr_results)

    confidence, flags = _compute_confidence({
        "personal_id": (personal_id, pid_conf),
        "birth_date":  (birth_date, bd_conf),
        "full_name":   (full_name, name_conf),
        "event_date":  (event_date, ed_conf),
        "institution": (institution, 0.80 if institution else 0.0),
        "diagnoses":   [(d.icd10_code, 0.95) for d in diagnoses],
        "total":       (total if total else None, total_conf),
    })

    flags.extend(icd10_flags)

    if confidence < settings.extraction_rules_min_confidence:
        flags.append("rules_extraction_low_confidence")

    cross_doc = _build_cross_document(form_100, id_docs, receipts, diagnoses, institution)

    extraction = ExtractionResult(
        insured=InsuredData(
            full_name=full_name or "",
            birth_date=birth_date or "",
            personal_id=personal_id or "",
            policy_number=None,
        ),
        event=EventData(
            date=event_date or "",
            institution=institution,
            diagnoses=diagnoses,
            line_items=line_items,
            total_claimed=total,
            service_urgency=urgency,
        ),
        extraction_confidence=confidence,
        flags=flags,
        cross_document=cross_doc,
    )

    log.info(
        "rule_extraction_completed",
        claim_id=str(claim_id),
        confidence=extraction.extraction_confidence,
        flags=extraction.flags,
        diagnoses=[d.icd10_code for d in extraction.event.diagnoses],
        service_urgency=extraction.event.service_urgency,
        method="rules",
    )

    return extraction
