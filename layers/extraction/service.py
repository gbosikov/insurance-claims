"""
Слой 4 — Extraction Service.

Задача: извлечь структурированные данные из OCR-текста через Claude API.
ТОЛЬКО через tool use (structured output) — никогда не парси свободный текст.

После извлечения — обязательная кросс-валидация между документами.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

import anthropic
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import CrossValidationError, ExtractionFailedError
from core.models.claim import ClaimDocument, DocType
from core.schemas.claim import (
    CrossDocumentData,
    DiagnoisItem,
    EventData,
    ExtractionResult,
    InsuredData,
    LineItem,
)
from layers.extraction.classifier import reclassify_documents
from layers.ocr.service import OCRResult

log = structlog.get_logger()
settings = get_settings()

# Версия промпта — фиксируется в аудит-логе
# v1.1.0: добавлен cross_document (Шаг 25)
# v1.2.0: doc_source в line_items; нумерация чеков в промпте; services в form_100; line_items в receipt
PROMPT_VERSION = "extraction/v1.2.0"

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
- Извлекай ВСЕ строки услуг из ВСЕХ чеков в единый список event.line_items
- description — ТОЛЬКО название услуги. СТРОГО ЗАПРЕЩЕНО добавлять суффиксы
  '— чек #1', '(ЧЕК #2)', '— receipt 1' и подобные. Чистое название = лучше.
- doc_source — используй для трекинга источника: 'receipt_1', 'receipt_2', 'form_100'
- Если та же услуга повторяется в двух разных чеках с разными суммами — это ДВЕ строки
- Сумма в каждой строке берётся из конкретного чека (не суммируй)
- Услуги из формы 100 включай в line_items ТОЛЬКО если написаны с суммой

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
    Извлечение данных через Claude API (tool use) + кросс-валидация.

    1. Объединить OCR-тексты с метками типа
    2. Вызвать Claude с EXTRACTION_TOOL (tool_choice=required)
    3. Кросс-валидация
    4. Аудит-лог
    """
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    with AuditTimer() as timer:
        # Переклассифицируем типы документов по содержимому OCR-текста.
        # Исправляет ошибки Layer 1 (filename_hint) до того как текст
        # попадает в промпт Claude — Claude получит правильные лейблы.
        ocr_results = await reclassify_documents(
            ocr_results, db, claim_id, tenant_id
        )

        user_message = _build_user_message(ocr_results)

        claude_input_tokens = 0
        claude_output_tokens = 0
        try:
            response = await client.messages.create(
                model=settings.claude_model,
                max_tokens=settings.claude_extraction_max_tokens,
                temperature=settings.claude_extraction_temperature,
                system=SYSTEM_PROMPT,
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "extract_claim_data"},
                messages=[{"role": "user", "content": user_message}],
            )
            claude_input_tokens = response.usage.input_tokens
            claude_output_tokens = response.usage.output_tokens
        except anthropic.APIError as e:
            raise ExtractionFailedError(f"Claude API error: {e}") from e

        # Извлекаем tool_use блок
        tool_use_block = next(
            (block for block in response.content if block.type == "tool_use"),
            None
        )
        if tool_use_block is None:
            raise ExtractionFailedError("Claude did not return tool_use block")

        raw: dict[str, Any] = tool_use_block.input

        # Парсим в Pydantic-схему
        try:
            insured = InsuredData(**raw["insured"])
            event_raw = raw["event"]

            # Извлечение service_urgency с обработкой null-значения
            service_urgency = event_raw.get("service_urgency")
            if service_urgency and service_urgency not in ("urgent", "diagnostic", "planned"):
                log.warning(
                    "invalid_service_urgency",
                    claim_id=str(claim_id),
                    value=service_urgency
                )
                service_urgency = None

            event = EventData(
                date=event_raw["date"],
                institution=event_raw.get("institution"),
                diagnoses=[DiagnoisItem(**d) for d in event_raw.get("diagnoses", [])],
                line_items=[LineItem(**li) for li in event_raw.get("line_items", [])],
                total_claimed=event_raw["total_claimed"],
                service_urgency=service_urgency,
            )
            # Кросс-документные данные (Шаг 25): опциональны, ошибки парсинга не критичны
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

        # Кросс-валидация
        extraction, warnings = cross_validate(extraction, ocr_results, submission_date)

        if warnings:
            log.warning("cross_validation_warnings", claim_id=str(claim_id), warnings=warnings)

        # Персистентность: атрибутируемый срез extraction → ClaimDocument.extracted_data
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
            # Полный текст запроса к Claude — для отладки и аудита
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
            # Сырой ответ Claude (tool_use input) — до Pydantic-парсинга
            "claude_raw_response": raw,
            # Полный результат извлечения — для пост-аудита и fine-tuning датасета (Шаг 35)
            "extraction": extraction.model_dump(),
        },
        confidence={"extraction": extraction.extraction_confidence},
        prompt_version=PROMPT_VERSION,
        model_version=settings.claude_model,
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
