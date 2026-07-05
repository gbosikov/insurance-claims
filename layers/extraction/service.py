"""
Слой 4 — Extraction Service.

Задача: извлечь структурированные данные из OCR-текста через Claude API.
ТОЛЬКО через tool use (structured output) — никогда не парси свободный текст.

После извлечения — обязательная кросс-валидация между документами.
"""

from __future__ import annotations

import json
from datetime import date
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import CrossValidationError, ExtractionFailedError
from core.llm_client import LLMAPIError, LLMNoToolBlockError, get_active_model_name, get_llm_client
from core.models.claim import ClaimDocument, DocType
from core.schemas.claim import (
    CrossDocumentData,
    DiagnoisItem,
    EventData,
    ExtractionResult,
    InsuredData,
    LineItem,
    ReceiptSummary,
)
from layers.extraction.classifier import reclassify_documents
from layers.ocr.service import OCRResult

log = structlog.get_logger()
settings = get_settings()

# Версия промпта — фиксируется в аудит-логе
# v1.1.0: добавлен cross_document (Шаг 25)
# v1.2.0: doc_source в line_items; нумерация чеков в промпте; services в form_100; line_items в receipt
# v1.3.0: amount_currency (дозировки vs цены); receipt_summaries (total_stated); 7 новых валидаций
PROMPT_VERSION = "extraction/v1.3.1"

# ── Tool definition для Claude API ────────────────────────────────

EXTRACTION_TOOL: dict[str, Any] = {
    "name": "extract_claim_data",
    "description": "Извлечь структурированные данные из OCR-текста страховых документов",
    "input_schema": {
        "type": "object",
        "properties": {
            "insured": {
                "type": "object",
                "description": "Данные застрахованного",
                "properties": {
                    "full_name": {
                        "type": "string",
                        "description": "Полное ФИО застрахованного"
                    },
                    "birth_date": {
                        "type": "string",
                        "description": "Дата рождения в формате YYYY-MM-DD"
                    },
                    "personal_id": {
                        "type": "string",
                        "description": "Личный номер / ID (9–11 цифр в ID-документах)"
                    },
                    "policy_number": {
                        "type": ["string", "null"],
                        "description": "Номер страхового полиса если указан"
                    },
                },
                "required": ["full_name", "birth_date", "personal_id"]
            },
            "event": {
                "type": "object",
                "description": "Данные страхового случая",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Дата события в формате YYYY-MM-DD"
                    },
                    "institution": {
                        "type": ["string", "null"],
                        "description": "Наименование медицинского учреждения"
                    },
                    "diagnoses": {
                        "type": "array",
                        "description": "Список диагнозов",
                        "items": {
                            "type": "object",
                            "properties": {
                                "icd10_code": {
                                    "type": "string",
                                    "description": "Код МКБ-10 (буква + цифры, например J06.9)"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Описание диагноза"
                                },
                            },
                            "required": ["icd10_code", "description"]
                        }
                    },
                    "line_items": {
                        "type": "array",
                        "description": "Все услуги из ВСЕХ чеков в единый список. "
                                       "description — ТОЛЬКО название услуги, "
                                       "БЕЗ суффиксов '— чек #1', '(ЧЕК #2)' и подобных. "
                                       "Используй doc_source для указания источника.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "description": "Чистое название услуги без указания источника"
                                },
                                "amount": {
                                    "type": "number",
                                    "description": "Сумма из конкретного чека (не суммировать)"
                                },
                                "amount_currency": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "Валюта/единица: 'GEL' или '₾' если это платёж за услугу; "
                                        "'IU'/'ერთ'/'ერთეული' если единицы витамина/препарата; "
                                        "'mg'/'мг' если миллиграммы; 'ml'/'мл' если миллилитры; "
                                        "'tab'/'шт'/'კაფს' если количество таблеток/капсул. "
                                        "ОБЯЗАТЕЛЬНО для каждой строки."
                                    )
                                },
                                "doc_source": {
                                    "type": ["string", "null"],
                                    "description": "Источник: 'receipt_1', 'receipt_2'... или 'form_100'"
                                },
                            },
                            "required": ["description", "amount"]
                        }
                    },
                    "total_claimed": {
                        "type": "number",
                        "description": "Итоговая сумма к возмещению"
                    },
                    "service_urgency": {
                        "type": ["string", "null"],
                        "enum": ["urgent", "diagnostic", "planned", None],
                        "description": "Тип услуги: urgent (ургентная/გადაუდებელი) | "
                                      "diagnostic (დიაგნოსტიკა) | "
                                      "planned (გეგმიური/плановая) | null (не указано)"
                    },
                    "receipt_summaries": {
                        "type": "array",
                        "description": (
                            "По одному объекту на каждый чек (RECEIPT-документ) в пакете. "
                            "doc_source должен совпадать с doc_source в line_items."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "doc_source": {
                                    "type": "string",
                                    "description": "Идентификатор: 'receipt_1', 'receipt_2'..."
                                },
                                "total_stated": {
                                    "type": ["number", "null"],
                                    "description": (
                                        "Итоговая строка чека: სულ / სულ ჯამი / გადასახდელი / "
                                        "Итого / Total. null если не найдена."
                                    )
                                },
                                "items_sum": {
                                    "type": ["number", "null"],
                                    "description": "Сумма всех GEL-строк этого чека"
                                },
                                "receipt_date": {
                                    "type": ["string", "null"],
                                    "description": "Дата чека YYYY-MM-DD"
                                },
                                "receipt_institution": {
                                    "type": ["string", "null"],
                                    "description": "Название учреждения на чеке (нужно для склейки страниц одного чека)"
                                },
                            },
                            "required": ["doc_source"]
                        }
                    },
                },
                "required": ["date", "total_claimed"]
            },
            "extraction_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Общий уровень уверенности в извлечённых данных"
            },
            "flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Проблемы: low_confidence_name | missing_date | missing_policy | amount_unclear | icd10_unclear"
            },
            "cross_document": {
                "type": "object",
                "description": "Значения, как они видны В КАЖДОМ документе ПО ОТДЕЛЬНОСТИ "
                               "(для кросс-проверки согласованности). Заполняй только из явно "
                               "присутствующего текста; если документ или поле отсутствует — null.",
                "properties": {
                    "form_100": {
                        "type": ["object", "null"],
                        "properties": {
                            "full_name":   {"type": ["string", "null"]},
                            "birth_date":  {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                            "date":        {"type": ["string", "null"], "description": "Дата события YYYY-MM-DD"},
                            "institution": {"type": ["string", "null"]},
                            "diagnoses":   {"type": "array", "items": {"type": "string"},
                                            "description": "Коды МКБ-10 как в документе"},
                            "services":    {"type": "array", "items": {"type": "string"},
                                            "description": "Услуги/процедуры явно перечисленные в форме 100 (без сумм)"},
                            "total":       {"type": ["number", "null"]},
                        },
                    },
                    "id_document": {
                        "type": ["object", "null"],
                        "properties": {
                            "full_name":   {"type": ["string", "null"]},
                            "birth_date":  {"type": ["string", "null"]},
                            "personal_id": {"type": ["string", "null"]},
                        },
                    },
                    "receipt": {
                        "type": ["object", "null"],
                        "properties": {
                            "date":        {"type": ["string", "null"]},
                            "institution": {"type": ["string", "null"]},
                            "diagnoses":   {"type": "array", "items": {"type": "string"}},
                            "line_items":  {
                                "type": "array",
                                "description": "Детальный список услуг из каждого чека с номером чека",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "description":    {"type": "string"},
                                        "amount":         {"type": "number"},
                                        "receipt_number": {"type": ["integer", "null"],
                                                           "description": "Номер чека в пакете (1, 2, 3...)"},
                                    },
                                    "required": ["description", "amount"]
                                }
                            },
                            "total":       {
                                "type": ["number", "null"],
                                "description": "Итоговая сумма: если чеков несколько — СЛОЖИ суммы всех чеков"
                            },
                        },
                    },
                },
            }
        },
        "required": ["insured", "event", "extraction_confidence"]
    }
}

SYSTEM_PROMPT = """Ты — система извлечения данных из страховых документов.
Документы могут быть на русском, грузинском или английском языке.
Извлекай данные независимо от языка документа.

ПРАВИЛА:
- Извлекай только то, что явно написано в тексте
- Нормализуй даты в формат YYYY-MM-DD
- Нормализуй суммы в float (без символов валюты, точка как разделитель)
- Личный номер: последовательность цифр 9-11 символов (в ID-документах Грузии — 11 цифр)
- Коды МКБ-10: формат буква+цифры, например J06.9, Z00.0, I10
- Если поле отсутствует — верни null, не придумывай
- Если данные нечёткие — добавь флаг и снизь extraction_confidence
- При неоднозначности снижай confidence, добавляй флаг — не придумывай данные

ПРАВИЛА ДЛЯ LINE_ITEMS (услуги из чеков и формы 100):
══════════════════════════════════════════════════════
- КАЖДАЯ УНИКАЛЬНАЯ УСЛУГА ПОЯВЛЯЕТСЯ В СПИСКЕ ТОЛЬКО ОДИН РАЗ.
  Form 100 и чеки описывают ОДНО ПОСЕЩЕНИЕ — одни и те же услуги.
  Если услуга есть и в форме 100, и в чеке → включи её ОДИН РАЗ:
    description = полное название из формы 100 (оно длиннее и точнее)
    amount      = сумма из чека (он финансовый документ)
    doc_source  = 'receipt_N' (источник суммы)
- Услуги ТОЛЬКО из формы 100 (без чека) — включай если сумма указана,
  doc_source = 'form_100'
- Если та же услуга есть в ДВУХ РАЗНЫХ чеках с разными суммами — это ДВЕ строки
- description — ТОЛЬКО название услуги, без суффиксов '— чек #1', '(ЧЕК #2)'
- doc_source — трекинг источника суммы: 'receipt_1', 'receipt_2', 'form_100'

ПРАВИЛА ДЛЯ AMOUNT_CURRENCY (обязательно для каждой строки):
════════════════════════════════════════════════════════════
- amount_currency = 'GEL' или '₾' → платёж за медицинскую услугу → включается в итог
- Медицинские единицы — это НЕ цена, даже если число большое:
    IU / ед.               — единицы витаминов/препаратов (5600 IU ≠ 5600 GEL)
    mg / мг / г / g        — граммы/миллиграммы дозировки
    ml / мл                — миллилитры
    tab / шт               — количество таблеток/капсул
- Правило: если число стоит рядом с названием препарата без символа GEL/₾ — это дозировка
- Примеры (числа без GEL/₾ = дозировки):
    "Vitamin D 5600 IU"    → amount=5600, amount_currency='IU'  ← дозировка, НЕ цена
    "Препарат 120 мг"      → amount=120,  amount_currency='mg'  ← дозировка, НЕ цена
    "Таблетки 30 шт"       → amount=30,   amount_currency='шт'  ← количество, НЕ цена
    "Физраствор 1000 мл"   → amount=1000, amount_currency='ml'  ← объём, НЕ цена
- Примеры (числа с GEL/₾ = платёжные суммы):
    "Консультация 90 GEL"  → amount=90,   amount_currency='GEL' ← оплата
    "ЭКГ 130 GEL"          → amount=130,  amount_currency='GEL' ← оплата
    "Анализ крови 40.00"   → amount=40,   amount_currency='GEL' ← цена в контексте чека

ПРАВИЛА ДЛЯ RECEIPT_SUMMARIES (по одному объекту на каждый чек):
══════════════════════════════════════════════════════════════════
- Заполни по одному объекту на каждый RECEIPT-документ в пакете
- total_stated: найди итоговую строку чека — სულ / სულ ჯამი / გადასახდელი / Итого / Total
  Это число АВТОРИТЕТНО — оно напечатано отдельной строкой крупным шрифтом,
  читается надёжнее отдельных строк позиций
- items_sum: сложи все GEL-строки данного чека (не включай дозировки IU/mg/etc.)
- receipt_date: дата на чеке YYYY-MM-DD
- receipt_institution: название учреждения как написано на чеке (клиника, лаборатория)
  Это поле критически важно для склейки страниц одного длинного чека:
  если два receipt-документа имеют одинаковые receipt_date + receipt_institution →
  система автоматически объединит их в один чек (страница 1 + страница 2)
- doc_source: 'receipt_1', 'receipt_2'... — должен совпадать с doc_source в line_items

ПРАВИЛА ДЛЯ cross_document.form_100.services:
- Список услуг/процедур как они написаны в форме 100 (назначения врача, выполненные процедуры)
- Заполни даже если сумм нет — для сверки с чеками

ПРАВИЛА ДЛЯ cross_document.receipt.line_items:
- Детализация из каждого чека; receipt_number = порядковый номер чека (1, 2, 3...)
- Позволяет сравнить с form_100.services построчно

КРОСС-ДОКУМЕНТНЫЕ ДАННЫЕ (cross_document):
- Для каждого документа (form_100 / id_document / receipt) заполни значения,
  как они написаны ИМЕННО В ЭТОМ документе — даже если они расходятся между документами
- НЕ нормализуй расхождения и НЕ выбирай «правильное» значение —
  система сама сверит документы между собой
- Если документ отсутствует в наборе — верни null для всего объекта
- ВАЖНО для cross_document.receipt.total:
  если в наборе документов НЕСКОЛЬКО чеков (RECEIPT) — сложи суммы всех чеков
  и запиши ИТОГ в receipt.total. Это позволит сопоставить с form_100.total.

ОПРЕДЕЛЕНИЕ SERVICE_URGENCY (тип услуги):
════════════════════════════════════════════════════════════════════════════

URGENCY MARKERS:
─────────────────────────────────────────────────────────────────────────────
Ищи эти слова в форме 100 или медкарте:

URGENT (სასწრაფო / გადაუდებელი / ჯანმრთელობის მდგომარეობის გაუარესება):
  • სასწრაფო (срочно, неотложно)
  • გადაუდებელი (неотложный, экстренный)
  • უბედური შემთხვევა (чрезвычайное происшествие, ДТП)
  • სიცოცხლისთვის საშიში (угроза жизни)
  • აკუტი (острый)
  • ჰოსპიტალიზაცია 24 საათში (требует госпитализации в течение 24 часов)

DIAGNOSTIC (პირველადი დიაგნოსტიკა / screening / prevention):
  • პირველადი დიაგნოსტიკა (первичная диагностика)
  • სკრინინგი (скрининг)
  • პროფილაქტიკა (профилактика)
  • რუტინული კვლევა (рутинный осмотр)
  • ჯანმრთელობის მდგომარეობის მონიტორინგი (мониторинг здоровья)
  • საჰაერო მოგზაურობის დაზღვევა (страховка для путешествия)
  • სწრაფი ტესტი (быстрый тест)

PLANNED (გეგმიური / плановое / লাভবহ):
  • გეგმიური (плановое, запланированное)
  • ოპერაცია (операция — обычно плановая)
  • საოპერაციო მკურნალობა (хирургическое лечение)
  • დაგეგმილი (запланированное)
  • ელექტიური ოპერაცია (плановая операция)

NULL CASE (Если маркеры НЕ найдены):
  • Врач просто описывает симптомы без явного указания типа
  • Форма содержит только диагноз без контекста
  • ВОЗВРАЩАЙ: null → система использует эвристику (см. ниже)

ЭВРИСТИКА для null-значения (используется decision-слоем):
─────────────────────────────────────────────────────────────────────────────
Если service_urgency = null, то при CARVEOUT исключениях:
  1. Проверь диагноз:
     - Хронические болезни (N18, I10, E10) → скорее всего "planned"
     - Острые болезни (J06, B16, R07) → скорее всего "urgent"
  2. Если всё ещё неясно → требуется manual_review (не одобряй автоматически)

ПРИМЕРЫ:
─────────────────────────────────────────────────────────────────────────────
"პაციენტი დაიკომპლექტა ტროფიკული მდგომარეობით" → urgent
"ორ კვირაში მელაპაროსკოპია დაგეგმილი გამოკვლევა" → planned
"პაციენტი პირველადი დიაგნოსტიკისთვის" → diagnostic
"რუტინული წლიური მოწვევა, სამედიცინო ანკეტა" → diagnostic
"კბილის პრობლემა, ხელმოწერილი ექიმის მიერ" → NULL (დაგვჭირდება heuristic)
"""


def _build_user_message(ocr_results: list[OCRResult]) -> str:
    """Собирает промпт из OCR-результатов всех документов.

    Несколько чеков нумеруются (#1, #2...) чтобы Claude не добавлял
    эти номера в description услуг — для этого есть поле doc_source.
    """
    parts = []
    receipt_idx = 0
    for result in ocr_results:
        if result.doc_type == DocType.RECEIPT:
            receipt_idx += 1
            doc_label = f"ЧЕК/КВИТАНЦИЯ #{receipt_idx} (doc_source=receipt_{receipt_idx})"
        else:
            doc_label = {
                DocType.FORM_100:    "ФОРМА 100 (Направление/Акт) (doc_source=form_100)",
                DocType.ID_DOCUMENT: "ДОКУМЕНТ УДОСТОВЕРЯЮЩИЙ ЛИЧНОСТЬ",
            }.get(result.doc_type, result.doc_type.value.upper())

        parts.append(f"=== {doc_label} (OCR confidence: {result.avg_confidence:.2f}) ===\n{result.full_text}")

    return "\n\n".join(parts)


# ── Кросс-валидация ───────────────────────────────────────────────

def _fuzzy_name_match(name1: str, name2: str) -> float:
    """Нечёткое сравнение имён через SequenceMatcher."""
    from difflib import SequenceMatcher
    n1 = name1.lower().strip()
    n2 = name2.lower().strip()
    return SequenceMatcher(None, n1, n2).ratio()


def _icd10_prefix(code: str) -> str:
    """Префикс кода МКБ-10 для сравнения между документами: J06.9 → J06."""
    return code.upper().split(".")[0].strip()


def cross_validate(
    extraction: ExtractionResult,
    ocr_results: list[OCRResult],
    submission_date: date,
) -> tuple[ExtractionResult, list[str]]:
    """
    Кросс-валидация между документами (Шаг 25).

    Правила:
    1. event_date ≤ submission_date
    2. Сумма строк ≈ total_claimed (± extraction_amount_mismatch_pct)
    По cross_document (если Claude его заполнил):
    3. birth_date form_100 vs id_document — точное совпадение
       (ФИО не сравниваем: транслитерация RU/KA/EN, верификация — по personal_id)
    4. form_100.total vs receipt.total (сумма всех чеков, ± extraction_amount_mismatch_pct)
    5. Диагнозы form_100 vs receipt — совпадение по префиксу МКБ-10
    6. Даты form_100 vs receipt — расхождение ≤ extraction_date_mismatch_max_days
    7. Учреждение form_100 vs receipt (fuzzy ≥ extraction_institution_match_threshold)
       → при расхождении confidence *= extraction_institution_mismatch_penalty

    Возвращает (обновлённый ExtractionResult, список предупреждений).
    При несоответствии — снижаем confidence и добавляем флаг (не отказ: manual_review решит).
    """
    warnings: list[str] = []
    flags = list(extraction.flags)

    # 0. Нечитаемые документы (OCR < 100 символов — нельзя классифицировать надёжно)
    unreadable_docs = [r for r in ocr_results if len(r.full_text.strip()) < 100]
    if unreadable_docs:
        flags.append("unreadable_document")
        warnings.append(
            f"{len(unreadable_docs)} document(s) returned fewer than 100 OCR characters — "
            "possibly unreadable, routing to manual review"
        )

    # 1. Дата события не позже даты подачи
    try:
        event_date = date.fromisoformat(extraction.event.date)
        if event_date > submission_date:
            flags.append("event_date_after_submission")
            warnings.append(f"Event date {event_date} is after submission {submission_date}")
    except ValueError:
        flags.append("invalid_event_date")

    # 2. Сумма строк vs общая сумма
    if extraction.event.line_items:
        items_total = sum(item.amount for item in extraction.event.line_items)
        total_claimed = extraction.event.total_claimed
        if total_claimed > 0:
            diff_pct = abs(items_total - total_claimed) / total_claimed
            if diff_pct > settings.extraction_amount_mismatch_pct:
                flags.append("amount_mismatch")
                warnings.append(f"Line items total {items_total:.2f} vs claimed {total_claimed:.2f} ({diff_pct:.1%} diff)")

    # ── Кросс-документные проверки (по данным cross_document) ──────
    cross = extraction.cross_document
    form = cross.form_100 if cross else None
    id_doc = cross.id_document if cross else None
    receipt = cross.receipt if cross else None

    # 3. Дата рождения: form_100 vs id_document — точное совпадение
    # (имена не сравниваем — транслитерация RU/KA/EN даёт ложные срабатывания;
    #  верификация личности — по personal_id vs getpolicylist в tasks.py)
    if form and id_doc and form.birth_date and id_doc.birth_date:
        try:
            bd_form = date.fromisoformat(form.birth_date)
            bd_id = date.fromisoformat(id_doc.birth_date)
            if bd_form != bd_id:
                flags.append("birth_date_mismatch")
                warnings.append(
                    f"Birth date mismatch: form_100={form.birth_date} vs id_document={id_doc.birth_date}"
                )
        except ValueError:
            flags.append("invalid_birth_date")

    # 4. Суммы: form_100 vs receipt (сумма всех чеков — Claude суммирует при извлечении)
    if form and receipt and form.total is not None and receipt.total is not None and form.total > 0:
        diff_pct = abs(receipt.total - form.total) / form.total
        if diff_pct > settings.extraction_amount_mismatch_pct:
            flags.append("receipt_total_mismatch")
            warnings.append(
                f"Receipt total {receipt.total:.2f} vs form_100 total {form.total:.2f} "
                f"({diff_pct:.1%} diff)"
            )

    # 5. Диагнозы: form_100 vs receipt по префиксу МКБ-10
    if form and receipt and form.diagnoses and receipt.diagnoses:
        form_prefixes = {_icd10_prefix(c) for c in form.diagnoses}
        receipt_prefixes = {_icd10_prefix(c) for c in receipt.diagnoses}
        if not (form_prefixes & receipt_prefixes):
            flags.append("diagnosis_mismatch")
            warnings.append(
                f"Diagnosis mismatch: form_100={sorted(form_prefixes)} vs "
                f"receipt={sorted(receipt_prefixes)} (no common ICD-10 prefix)"
            )

    # 6. Даты: form_100 vs receipt
    if form and receipt and form.date and receipt.date:
        try:
            delta_days = abs((date.fromisoformat(form.date) - date.fromisoformat(receipt.date)).days)
            if delta_days > settings.extraction_date_mismatch_max_days:
                flags.append("date_mismatch")
                warnings.append(
                    f"Date mismatch: form_100={form.date} vs receipt={receipt.date} ({delta_days} days)"
                )
        except ValueError:
            flags.append("invalid_cross_document_date")

    # 7. Услуги формы 100 без чеков (form_100.services vs receipt.line_items)
    # Предупреждение: если форма 100 содержит услугу, для которой нет ни одного
    # чека с похожим названием — возможно чек не приложен или будет позже.
    if form and form.services and receipt and receipt.line_items:
        receipt_descs = [li.description.lower() for li in receipt.line_items]

        def _service_has_receipt(svc: str) -> bool:
            svc_lower = svc.lower()
            # Первые 20 символов как ключ — устойчиво к кодам (ZYZX90) и вариациям
            svc_key = svc_lower[:20].strip()
            return any(svc_key in rd or rd[:20] in svc_lower for rd in receipt_descs)

        unmatched = [s for s in form.services if not _service_has_receipt(s)]
        if unmatched:
            flags.append("form_100_services_without_receipts")
            warnings.append(
                f"Form 100 lists {len(unmatched)} service(s) without matching receipts: "
                + "; ".join(unmatched[:3])
            )

    # 8. Учреждение: form_100 vs receipt
    institution_mismatch = False
    if form and receipt and form.institution and receipt.institution:
        ratio = _fuzzy_name_match(form.institution, receipt.institution)
        if ratio < settings.extraction_institution_match_threshold:
            institution_mismatch = True
            flags.append("institution_mismatch")
            warnings.append(
                f"Institution mismatch between form_100 and receipt (ratio={ratio:.2f})"
            )

    # 9. Дублирование чеков (одинаковый stated_total + дата → один чек подан дважды)
    summaries = extraction.event.receipt_summaries
    if len(summaries) > 1:
        seen_receipt_keys: set[tuple] = set()
        for rs in summaries:
            if rs.total_stated is not None and rs.receipt_date is not None:
                key = (rs.total_stated, rs.receipt_date)
                if key in seen_receipt_keys:
                    flags.append("duplicate_receipt")
                    warnings.append(
                        f"Duplicate receipt: {rs.doc_source} has same total "
                        f"({rs.total_stated} GEL) and date ({rs.receipt_date}) as another receipt"
                    )
                    break
                seen_receipt_keys.add(key)

    # 10. Дата чека vs дата события (расхождение > extraction_date_mismatch_max_days)
    try:
        event_dt = date.fromisoformat(extraction.event.date)
        for rs in summaries:
            if rs.receipt_date:
                try:
                    receipt_dt = date.fromisoformat(rs.receipt_date)
                    delta = abs((receipt_dt - event_dt).days)
                    if delta > settings.extraction_date_mismatch_max_days:
                        flags.append("receipt_date_mismatch")
                        warnings.append(
                            f"{rs.doc_source} date {rs.receipt_date} differs from event date "
                            f"{extraction.event.date} by {delta} days"
                        )
                        break
                except ValueError:
                    pass
    except ValueError:
        pass

    # 11. Расхождение stated_total vs items_sum > 10% (OCR пропустил строки позиций)
    for rs in summaries:
        if rs.discrepancy:
            flags.append("receipt_items_incomplete")
            warnings.append(
                f"{rs.doc_source}: stated {rs.total_stated} GEL vs items_sum "
                f"{rs.items_sum} GEL (>10% discrepancy — OCR may have missed lines, "
                "total_claimed uses stated total)"
            )

    # Обновляем extraction с новыми флагами
    updated_confidence = extraction.extraction_confidence
    if flags != extraction.flags:
        # Снижаем confidence за каждый новый флаг
        new_flags_count = len(flags) - len(extraction.flags)
        updated_confidence = max(0.0, extraction.extraction_confidence - new_flags_count * 0.05)
    if institution_mismatch:
        updated_confidence = max(0.0, updated_confidence * settings.extraction_institution_mismatch_penalty)

    updated = ExtractionResult(
        insured=extraction.insured,
        event=extraction.event,
        extraction_confidence=updated_confidence,
        flags=flags,
        cross_document=extraction.cross_document,
    )

    return updated, warnings


# ── Персистентность результата извлечения ─────────────────────────

async def _persist_extracted_data(
    extraction: ExtractionResult,
    ocr_results: list[OCRResult],
    db: AsyncSession,
    tenant_id: UUID,
) -> None:
    """
    Сохранить атрибутируемый срез extraction в ClaimDocument.extracted_data.

    form_100    → insured + event (диагнозы, даты, услуги)
    receipt     → line_items + total_claimed
    id_document → insured
    + as_seen_in_document: значения из cross_document для этого типа документа.
    """
    from sqlalchemy import select

    doc_ids = [r.doc_id for r in ocr_results]
    if not doc_ids:
        return

    result = await db.execute(
        select(ClaimDocument).where(
            ClaimDocument.id.in_(doc_ids),
            ClaimDocument.tenant_id == tenant_id,
        )
    )
    docs = {doc.id: doc for doc in result.scalars().all()}
    cross = extraction.cross_document

    for ocr in ocr_results:
        doc = docs.get(ocr.doc_id)
        if doc is None:
            continue

        if ocr.doc_type == DocType.FORM_100:
            data: dict[str, Any] = {
                "insured": extraction.insured.model_dump(),
                "event": extraction.event.model_dump(),
            }
            if cross and cross.form_100:
                data["as_seen_in_document"] = cross.form_100.model_dump()
        elif ocr.doc_type == DocType.RECEIPT:
            data = {
                "line_items": [li.model_dump() for li in extraction.event.line_items],
                "total_claimed": extraction.event.total_claimed,
                "receipt_summaries": [rs.model_dump() for rs in extraction.event.receipt_summaries],
            }
            if cross and cross.receipt:
                data["as_seen_in_document"] = cross.receipt.model_dump()
        else:  # ID_DOCUMENT
            data = {"insured": extraction.insured.model_dump()}
            if cross and cross.id_document:
                data["as_seen_in_document"] = cross.id_document.model_dump()

        doc.extracted_data = data

    await db.flush()


# ── Основная функция ──────────────────────────────────────────────

async def extract_claim_data(
    ocr_results: list[OCRResult],
    claim_id: UUID,
    tenant_id: UUID,
    submission_date: date,
    db: AsyncSession,
) -> ExtractionResult:
    """
    Извлечение данных из OCR-текста + кросс-валидация.

    Путь зависит от settings.extraction_use_rules:
      True  → детерминированные regex/keyword rules (без Claude, дешевле и быстрее)
      False → Claude API с EXTRACTION_TOOL (tool_choice=required, точнее для сложных случаев)

    Оба пути возвращают одинаковый ExtractionResult.
    """
    # Переклассификация запускается независимо от пути извлечения:
    # исправляет ошибки filename_hint до того как текст обрабатывается.
    ocr_results = await reclassify_documents(ocr_results, db, claim_id, tenant_id)

    if settings.extraction_use_rules:
        return await _extract_with_rules(
            ocr_results, claim_id, tenant_id, submission_date, db
        )

    return await _extract_with_claude(
        ocr_results, claim_id, tenant_id, submission_date, db
    )


async def _extract_with_rules(
    ocr_results: list[OCRResult],
    claim_id: UUID,
    tenant_id: UUID,
    submission_date: date,
    db: AsyncSession,
) -> ExtractionResult:
    """Rule-based extraction path (без Claude API)."""
    from layers.extraction.rule_extractor import extract_by_rules

    with AuditTimer() as timer:
        extraction = await extract_by_rules(ocr_results, claim_id, db)
        extraction, warnings = cross_validate(extraction, ocr_results, submission_date)
        if warnings:
            log.warning("cross_validation_warnings", claim_id=str(claim_id), warnings=warnings)
        await _persist_extracted_data(extraction, ocr_results, db, tenant_id)

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="extraction",
        input_data={
            "docs_count": len(ocr_results),
            "method": "rules",
        },
        output_data={
            "insured_name": extraction.insured.full_name,
            "event_date": extraction.event.date,
            "total_claimed": extraction.event.total_claimed,
            "diagnoses_count": len(extraction.event.diagnoses),
            "service_urgency": extraction.event.service_urgency,
            "flags": extraction.flags,
            "cross_validation_warnings": warnings,
            "extraction": extraction.model_dump(),
        },
        confidence={"extraction": extraction.extraction_confidence},
        prompt_version="extraction/rules/v1.0.0",
        model_version="rules/v1.0",
        duration_ms=timer.duration_ms,
    )

    log.info(
        "extraction_completed",
        claim_id=str(claim_id),
        confidence=extraction.extraction_confidence,
        flags=extraction.flags,
        service_urgency=extraction.event.service_urgency,
        method="rules",
    )

    return extraction


def _merge_multipage_receipts(summaries: list[ReceiptSummary]) -> list[ReceiptSummary]:
    """Склейка страниц одного длинного чека.

    Условие слияния: два receipt_summaries с одинаковыми
    (receipt_date + receipt_institution) И ровно у одного есть total_stated.

    Логика:
    - Страница без итога (total_stated=None) + страница с итогом = один чек.
    - Два разных total_stated от одной клиники в один день = два разных чека, не сливать.
    - items_sum после слияния = сумма items_sum всех страниц.
    """
    if len(summaries) <= 1:
        return summaries

    groups: dict[tuple, list[ReceiptSummary]] = {}
    ungrouped: list[ReceiptSummary] = []

    for rs in summaries:
        if rs.receipt_date and rs.receipt_institution:
            key = (rs.receipt_date, rs.receipt_institution.lower().strip())
            groups.setdefault(key, []).append(rs)
        else:
            ungrouped.append(rs)

    result: list[ReceiptSummary] = list(ungrouped)

    for group_items in groups.values():
        if len(group_items) == 1:
            result.append(group_items[0])
            continue

        with_total = [rs for rs in group_items if rs.total_stated is not None]

        if len(with_total) == 1:
            # Ровно одна страница с итогом — сливаем
            anchor = with_total[0]
            combined_items = sum(rs.items_sum for rs in group_items if rs.items_sum is not None)
            discrepancy = (
                anchor.total_stated is not None
                and combined_items > 0
                and abs(anchor.total_stated - combined_items) / combined_items > 0.10
            )
            merged_source = "+".join(rs.doc_source for rs in group_items)
            result.append(ReceiptSummary(
                doc_source=merged_source,
                total_stated=anchor.total_stated,
                items_sum=combined_items if combined_items > 0 else None,
                receipt_date=anchor.receipt_date,
                receipt_institution=anchor.receipt_institution,
                discrepancy=discrepancy,
            ))
            log.info(
                "multipage_receipt_merged",
                merged_source=merged_source,
                pages=[rs.doc_source for rs in group_items],
                total_stated=anchor.total_stated,
            )
        else:
            # 0 или 2+ страниц с total_stated → разные чеки, не сливаем
            result.extend(group_items)

    return result


_DEDUP_FUZZY_THRESHOLD = 0.75  # минимальное сходство описаний для fuzzy-дедупа


def _dedup_same_receipts(summaries: list[ReceiptSummary]) -> list[ReceiptSummary]:
    """Удалить дублирующиеся чеки по ключу (total_stated, receipt_date).

    Один чек может прийти дважды: файл загружен дважды, или один чек
    сфотографирован с разными языковыми надписями на бланке (ГР + EN).
    При дедупликации оставляем первую встреченную запись, остальные дропаем.
    Флаг duplicate_receipt всё равно ставится в cross_validate() → manual_review.
    """
    seen: set[tuple] = set()
    result: list[ReceiptSummary] = []
    for rs in summaries:
        if rs.total_stated is not None and rs.receipt_date is not None:
            key = (rs.total_stated, rs.receipt_date)
            if key in seen:
                log.warning(
                    "duplicate_receipt_deduplicated",
                    removed_source=rs.doc_source,
                    total_stated=rs.total_stated,
                    receipt_date=rs.receipt_date,
                    institution=rs.receipt_institution,
                )
                continue
            seen.add(key)
        result.append(rs)
    return result

# Валюты платежей. Строки с другими единицами (IU, mg, ml) — дозировки, не цены.
_GEL_CURRENCIES: frozenset[str] = frozenset({"GEL", "₾", "ლარი", "LARI", "ЛАРИ"})


def _is_gel_amount(li: LineItem) -> bool:
    """True если строка является платёжной (GEL), а не дозировочной (IU/mg/ml/etc.)."""
    if li.amount_currency is None:
        return True  # нет инфо → считаем GEL (backward compat с данными до v1.3.0)
    return li.amount_currency.upper().strip() in _GEL_CURRENCIES


def _dedup_line_items(items: list) -> list:
    """Убрать дублирующиеся услуги из разных документов одного посещения.

    Три прохода:
    1. Дедуп внутри каждого источника: (description+amount) — убирает точные дубли.
    2. Межисточниковый дедуп по сумме: form_100 приоритетен (полные названия).
       Receipt добавляет только услуги с суммами, которых нет в form_100.
    3. Fuzzy-дедуп: если две оставшихся строки имеют одинаковую сумму и
       схожесть описаний ≥ 0.75 — оставляем более длинное описание.
    """
    if not items:
        return items

    # Проход 1: дедуп внутри источника по (description.lower(), amount)
    per_source: dict[str, dict[tuple, object]] = {}
    for item in items:
        src = (item.doc_source or "other")
        key = (item.description.strip().lower(), item.amount)
        per_source.setdefault(src, {}).setdefault(key, item)

    # Проход 2: form_100 как основа, receipt дополняет только новыми суммами.
    # form100_amounts содержит ТОЛЬКО GEL-суммы: дозировки (5600 IU) не должны
    # блокировать добавление реальных GEL-позиций из чеков с той же цифрой.
    form100 = list((per_source.get("form_100") or {}).values())
    form100_amounts = {li.amount for li in form100 if _is_gel_amount(li)}
    merged: list = list(form100)
    for src_key, src_map in per_source.items():
        if src_key == "form_100":
            continue
        for item in src_map.values():
            if item.amount not in form100_amounts:
                merged.append(item)

    # Проход 3: fuzzy-дедуп — одинаковая сумма + похожее описание → длиннее побеждает
    result: list = []
    for candidate in merged:
        is_dup = False
        for i, existing in enumerate(result):
            if existing.amount != candidate.amount:
                continue
            similarity = SequenceMatcher(
                None,
                existing.description.strip().lower(),
                candidate.description.strip().lower(),
            ).ratio()
            if similarity >= _DEDUP_FUZZY_THRESHOLD:
                # Оставляем более длинное (полное) описание
                if len(candidate.description) > len(existing.description):
                    result[i] = candidate
                is_dup = True
                break
        if not is_dup:
            result.append(candidate)

    return result


async def _extract_with_claude(
    ocr_results: list[OCRResult],
    claim_id: UUID,
    tenant_id: UUID,
    submission_date: date,
    db: AsyncSession,
) -> ExtractionResult:
    """LLM extraction path (Anthropic или Gemini в зависимости от LLM_PROVIDER)."""
    llm_client = get_llm_client()

    with AuditTimer() as timer:
        # NOTE: reclassify_documents уже вызван в extract_claim_data()
        user_message = _build_user_message(ocr_results)

        claude_input_tokens = 0
        claude_output_tokens = 0
        try:
            result = await llm_client.call_tool(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
                tool=EXTRACTION_TOOL,
                tool_name="extract_claim_data",
                max_tokens=settings.claude_extraction_max_tokens,
                temperature=settings.claude_extraction_temperature,
            )
            claude_input_tokens = result.input_tokens
            claude_output_tokens = result.output_tokens
        except (LLMAPIError, LLMNoToolBlockError) as e:
            raise ExtractionFailedError(f"LLM API error: {e}") from e

        raw: dict[str, Any] = result.tool_input  # type: ignore[assignment]

        # Парсим в Pydantic-схему
        try:
            insured = InsuredData(**raw["insured"])
            event_raw = raw["event"]

            service_urgency = event_raw.get("service_urgency")
            if service_urgency and service_urgency not in ("urgent", "diagnostic", "planned"):
                log.warning(
                    "invalid_service_urgency",
                    claim_id=str(claim_id),
                    value=service_urgency
                )
                service_urgency = None

            raw_line_items = [LineItem(**li) for li in event_raw.get("line_items", [])]
            deduped_line_items = _dedup_line_items(raw_line_items)

            # Парсим receipt_summaries — итоги по каждому чеку отдельно.
            # discrepancy вычисляем сразу: |total_stated - items_sum| > 10%.
            receipt_summaries: list[ReceiptSummary] = []
            for rs_raw in event_raw.get("receipt_summaries", []):
                total_stated = rs_raw.get("total_stated")
                items_sum = rs_raw.get("items_sum")
                discrepancy = False
                if total_stated is not None and items_sum is not None and items_sum > 0:
                    discrepancy = abs(total_stated - items_sum) / items_sum > 0.10
                receipt_summaries.append(ReceiptSummary(
                    doc_source=rs_raw.get("doc_source", "receipt"),
                    total_stated=total_stated,
                    items_sum=items_sum,
                    receipt_date=rs_raw.get("receipt_date"),
                    receipt_institution=rs_raw.get("receipt_institution"),
                    discrepancy=discrepancy,
                ))

            # Склейка страниц многостраничного чека
            receipt_summaries = _merge_multipage_receipts(receipt_summaries)
            # Дедупликация одинаковых чеков (один файл загружен дважды или два языка на бланке)
            receipt_summaries = _dedup_same_receipts(receipt_summaries)

            # Есть ли в пакете хотя бы один чек?
            has_receipts = bool(receipt_summaries) or any(
                li.doc_source and li.doc_source.startswith("receipt")
                for li in deduped_line_items
            )

            # total_claimed — три уровня приоритета:
            # 1. Сумма stated_total всех чеков (итоговая строка — авторитетный источник).
            # 2. GEL-строки из чеков (если нет stated_total).
            # 3. GEL-строки из всех документов (если чеков нет вообще).
            # form_100 GEL-строки ИГНОРИРУЮТСЯ при наличии хотя бы одного чека —
            # чтобы не было двойного подсчёта одной услуги.
            stated_totals = [
                rs.total_stated for rs in receipt_summaries if rs.total_stated is not None
            ]
            if stated_totals:
                total_claimed = round(sum(stated_totals), 2)
            elif has_receipts:
                _receipt_gel = [
                    li for li in deduped_line_items
                    if li.doc_source and li.doc_source.startswith("receipt")
                    and _is_gel_amount(li)
                ]
                total_claimed = (
                    round(sum(li.amount for li in _receipt_gel), 2)
                    if _receipt_gel else float(event_raw["total_claimed"])
                )
            else:
                _all_gel = [li for li in deduped_line_items if _is_gel_amount(li)]
                total_claimed = (
                    round(sum(li.amount for li in _all_gel), 2)
                    if _all_gel else float(event_raw["total_claimed"])
                )

            event = EventData(
                date=event_raw["date"],
                institution=event_raw.get("institution"),
                diagnoses=[DiagnoisItem(**d) for d in event_raw.get("diagnoses", [])],
                line_items=deduped_line_items,
                total_claimed=total_claimed,
                service_urgency=service_urgency,
                receipt_summaries=receipt_summaries,
            )
            cross_raw = raw.get("cross_document")
            cross_document: CrossDocumentData | None = None
            if cross_raw:
                try:
                    cross_document = CrossDocumentData(**cross_raw)
                except ValueError:
                    log.warning("cross_document_parse_failed", claim_id=str(claim_id))

            extraction = ExtractionResult(
                insured=insured,
                event=event,
                extraction_confidence=raw.get("extraction_confidence", 0.5),
                flags=raw.get("flags", []),
                cross_document=cross_document,
            )
        except (KeyError, ValueError) as e:
            raise ExtractionFailedError(f"Failed to parse extraction result: {e}") from e

        extraction, warnings = cross_validate(extraction, ocr_results, submission_date)

        if warnings:
            log.warning("cross_validation_warnings", claim_id=str(claim_id), warnings=warnings)

        await _persist_extracted_data(extraction, ocr_results, db, tenant_id)

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="extraction",
        input_data={
            "docs_count": len(ocr_results),
            "model": settings.claude_model,
            "max_tokens": settings.claude_extraction_max_tokens,
            "temperature": settings.claude_extraction_temperature,
            "system_prompt_chars": len(SYSTEM_PROMPT),
            "user_message_chars": len(user_message),
            "user_message": user_message,
        },
        output_data={
            "insured_name": extraction.insured.full_name,
            "event_date": extraction.event.date,
            "total_claimed": extraction.event.total_claimed,
            "diagnoses_count": len(extraction.event.diagnoses),
            "service_urgency": extraction.event.service_urgency,
            "flags": extraction.flags,
            "cross_validation_warnings": warnings,
            "input_tokens": claude_input_tokens,
            "output_tokens": claude_output_tokens,
            "claude_raw_response": raw,
            "extraction": extraction.model_dump(),
        },
        confidence={"extraction": extraction.extraction_confidence},
        prompt_version=PROMPT_VERSION,
        model_version=get_active_model_name(),
        duration_ms=timer.duration_ms,
    )

    log.info(
        "extraction_completed",
        claim_id=str(claim_id),
        confidence=extraction.extraction_confidence,
        flags=extraction.flags,
        service_urgency=extraction.event.service_urgency,
    )

    return extraction


# ── Вспомогательные функции для service_urgency ────────────────────────────

def resolve_service_urgency(
    service_urgency: str | None,
    diagnoses: list[DiagnoisItem],
) -> str | None:
    """
    Применить эвристику для определения service_urgency если врач не указал.

    Args:
        service_urgency: значение из extraction ("urgent"|"diagnostic"|"planned"|None)
        diagnoses: список диагнозов (для heuristic)

    Returns:
        Резолвленное значение ("urgent"|"diagnostic"|"planned"|None)

    Логика:
      1. Если явно указано → верни как есть
      2. Если None → используй ICD10-префикс диагнозов:
         - Хронические (N18, I10, E10, E11) → скорее planned
         - Острые (J06, B16, R07, R06) → скорее urgent
         - Если смешанно → None (требуется manual_review)
    """
    if service_urgency is not None:
        return service_urgency

    if not diagnoses:
        return None

    # ICD10 префиксы
    CHRONIC_PREFIXES = {"N1", "I1", "E1", "M5", "J4"}  # хронические
    ACUTE_PREFIXES = {"J0", "B1", "B2", "R0", "R5", "G0", "I6"}  # острые

    urgency_votes = {"chronic": 0, "acute": 0}

    for diag in diagnoses:
        code = diag.icd10_code.upper()
        prefix = code[:2]

        if prefix in CHRONIC_PREFIXES:
            urgency_votes["chronic"] += 1
        elif prefix in ACUTE_PREFIXES:
            urgency_votes["acute"] += 1

    # Решение на основе большинства
    if urgency_votes["acute"] > urgency_votes["chronic"]:
        return "urgent"
    elif urgency_votes["chronic"] > urgency_votes["acute"]:
        return "planned"

    # Ничья или нет данных
    return None


def should_require_manual_review_for_unknown_urgency(
    service_urgency: str | None,
    diagnoses: list[DiagnoisItem],
    has_carveout_exclusions: bool,
) -> tuple[bool, str | None]:
    """
    Проверить нужна ли manual_review из-за неизвестного service_urgency.

    Args:
        service_urgency: значение из extraction или после resolve_service_urgency()
        diagnoses: список диагнозов
        has_carveout_exclusions: есть ли в контракте CARVEOUT-исключения для этого диагноза

    Returns:
        (требуется ли manual_review, причина если требуется)

    Логика:
      - Если service_urgency известна → OK, не требуется manual_review
      - Если service_urgency = None И есть CARVEOUT-исключения → требуется manual_review
        (потому что не можем правильно применить CARVEOUT условия)
      - Если service_urgency = None И нет CARVEOUT-исключений → OK
    """
    if service_urgency is not None:
        # Urgency известна, можем применить любые исключения
        return False, None

    if has_carveout_exclusions:
        # Urgency неизвестна, а исключение зависит от urgency
        return True, (
            "unknown_service_urgency_with_carveout_exclusion: "
            "врач не указал тип услуги (ургентная/плановая), "
            "а в контракте есть условные исключения"
        )

    # Urgency неизвестна, но исключений нет — можем продолжить
    return False, None
