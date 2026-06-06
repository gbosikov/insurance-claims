"""
Unit тесты: Слой 4 — Extraction (кросс-валидация, промпт-билдер).
"""

from datetime import date
from uuid import UUID

import pytest

from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
from layers.extraction.service import _build_user_message, cross_validate


def make_extraction(
    total_claimed: float = 150.0,
    event_date: str = "2026-01-15",
    flags: list = None,
    confidence: float = 0.92,
) -> ExtractionResult:
    return ExtractionResult(
        insured=InsuredData(
            full_name="Иванов Иван Иванович",
            birth_date="1988-03-15",
            personal_id="12345678901",
            policy_number="DMC-2024-005521",
        ),
        event=EventData(
            date=event_date,
            institution="Клиника",
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация", amount=total_claimed)],
            total_claimed=total_claimed,
        ),
        extraction_confidence=confidence,
        flags=flags or [],
    )


def test_cross_validate_passes_valid_data():
    """Корректные данные проходят кросс-валидацию без предупреждений."""
    extraction = make_extraction()
    submission_date = date(2026, 1, 20)

    updated, warnings = cross_validate(extraction, [], submission_date)
    assert "event_date_after_submission" not in updated.flags
    assert "amount_mismatch" not in updated.flags


def test_cross_validate_event_after_submission():
    """Дата события позже даты подачи → флаг."""
    extraction = make_extraction(event_date="2026-01-25")
    submission_date = date(2026, 1, 20)  # раньше события

    updated, warnings = cross_validate(extraction, [], submission_date)
    assert "event_date_after_submission" in updated.flags
    assert len(warnings) > 0


def test_cross_validate_amount_mismatch():
    """Расхождение суммы > 1% → флаг."""
    extraction = make_extraction(total_claimed=200.0)
    # Меняем line_items чтобы они не совпадали
    extraction.event.line_items = [LineItem(description="Услуга", amount=150.0)]  # 150 vs 200

    submission_date = date(2026, 1, 20)
    updated, warnings = cross_validate(extraction, [], submission_date)
    assert "amount_mismatch" in updated.flags


def test_cross_validate_reduces_confidence_on_flags():
    """Кросс-валидация снижает confidence при обнаружении проблем."""
    extraction = make_extraction(event_date="2026-01-25", confidence=0.90)
    submission_date = date(2026, 1, 20)

    updated, _ = cross_validate(extraction, [], submission_date)
    assert updated.extraction_confidence < 0.90


def test_build_user_message_includes_all_docs(sample_ocr_result):
    """Промпт содержит текст всех документов."""
    from layers.ocr.service import OCRResult, TextBlock
    from core.models.claim import DocType
    from uuid import uuid4

    id_doc_result = OCRResult(
        doc_id=uuid4(),
        doc_type=DocType.ID_DOCUMENT,
        full_text="ФИО: Иванов Иван\nID: 12345678901",
        avg_confidence=0.95,
        strategy_used="vision_text_detection",
    )

    message = _build_user_message([sample_ocr_result, id_doc_result])
    assert "ФОРМА 100" in message
    assert "ДОКУМЕНТ УДОСТОВЕРЯЮЩИЙ ЛИЧНОСТЬ" in message
    assert "J06.9" in message
    assert "12345678901" in message
