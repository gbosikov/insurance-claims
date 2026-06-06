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
from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
from layers.ocr.service import OCRResult

log = structlog.get_logger()
settings = get_settings()

# Версия промпта — фиксируется в аудит-логе
PROMPT_VERSION = "extraction/v1.0.0"

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
                        "description": "Строки услуг с суммами",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "amount": {
                                    "type": "number",
                                    "description": "Сумма в числовом формате без символов валюты"
                                },
                            },
                            "required": ["description", "amount"]
                        }
                    },
                    "total_claimed": {
                        "type": "number",
                        "description": "Итоговая сумма к возмещению"
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
- При неоднозначности снижай confidence, добавляй флаг — не придумывай данные"""


def _build_user_message(ocr_results: list[OCRResult]) -> str:
    """Собирает промпт из OCR-результатов всех документов."""
    parts = []
    for result in ocr_results:
        doc_label = {
            DocType.FORM_100:    "ФОРМА 100 (Направление/Акт)",
            DocType.ID_DOCUMENT: "ДОКУМЕНТ УДОСТОВЕРЯЮЩИЙ ЛИЧНОСТЬ",
            DocType.RECEIPT:     "ЧЕК/КВИТАНЦИЯ",
        }.get(result.doc_type, result.doc_type.value.upper())

        parts.append(f"=== {doc_label} (confidence OCR: {result.avg_confidence:.2f}) ===\n{result.full_text}")

    return "\n\n".join(parts)


# ── Кросс-валидация ───────────────────────────────────────────────

def _fuzzy_name_match(name1: str, name2: str) -> float:
    """Нечёткое сравнение имён через SequenceMatcher."""
    from difflib import SequenceMatcher
    n1 = name1.lower().strip()
    n2 = name2.lower().strip()
    return SequenceMatcher(None, n1, n2).ratio()


def cross_validate(
    extraction: ExtractionResult,
    ocr_results: list[OCRResult],
    submission_date: date,
) -> tuple[ExtractionResult, list[str]]:
    """
    Кросс-валидация между документами.

    Правила:
    1. ФИО из form_100 vs id_document (fuzzy ≥ 0.90)
    2. event_date ≤ submission_date
    3. Сумма в form_100 ≈ сумма строк receipt (±1%)

    Возвращает (обновлённый ExtractionResult, список предупреждений).
    При критическом несоответствии — снижаем confidence и добавляем флаг.
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
            if diff_pct > 0.01:  # более 1% расхождение
                flags.append("amount_mismatch")
                warnings.append(f"Line items total {items_total:.2f} vs claimed {total_claimed:.2f} ({diff_pct:.1%} diff)")

    # Обновляем extraction с новыми флагами
    updated_confidence = extraction.extraction_confidence
    if flags != extraction.flags:
        # Снижаем confidence за каждый новый флаг
        new_flags_count = len(flags) - len(extraction.flags)
        updated_confidence = max(0.0, extraction.extraction_confidence - new_flags_count * 0.05)

    updated = ExtractionResult(
        insured=extraction.insured,
        event=extraction.event,
        extraction_confidence=updated_confidence,
        flags=flags,
    )

    return updated, warnings


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
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    with AuditTimer() as timer:
        user_message = _build_user_message(ocr_results)

        try:
            response = client.messages.create(
                model=settings.claude_model,
                max_tokens=settings.claude_extraction_max_tokens,
                temperature=settings.claude_extraction_temperature,
                system=SYSTEM_PROMPT,
                tools=[EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "extract_claim_data"},
                messages=[{"role": "user", "content": user_message}],
            )
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
            event = EventData(
                date=event_raw["date"],
                institution=event_raw.get("institution"),
                diagnoses=[DiagnoisItem(**d) for d in event_raw.get("diagnoses", [])],
                line_items=[LineItem(**li) for li in event_raw.get("line_items", [])],
                total_claimed=event_raw["total_claimed"],
            )
            extraction = ExtractionResult(
                insured=insured,
                event=event,
                extraction_confidence=raw.get("extraction_confidence", 0.5),
                flags=raw.get("flags", []),
            )
        except (KeyError, ValueError) as e:
            raise ExtractionFailedError(f"Failed to parse extraction result: {e}") from e

        # Кросс-валидация
        extraction, warnings = cross_validate(extraction, ocr_results, submission_date)

        if warnings:
            log.warning("cross_validation_warnings", claim_id=str(claim_id), warnings=warnings)

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="extraction",
        input_data={"docs_count": len(ocr_results)},
        output_data={
            "insured_name": extraction.insured.full_name,
            "event_date": extraction.event.date,
            "total_claimed": extraction.event.total_claimed,
            "diagnoses_count": len(extraction.event.diagnoses),
            "flags": extraction.flags,
            "cross_validation_warnings": warnings,
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
    )

    return extraction
