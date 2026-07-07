"""
Unit тесты: Слой 4 — Extraction (кросс-валидация, промпт-билдер).
"""

from datetime import date
from uuid import UUID

import pytest

from core.schemas.claim import (
    CrossDocForm100,
    CrossDocIdDocument,
    CrossDocReceipt,
    CrossDocumentData,
    DiagnoisItem,
    EventData,
    ExtractionResult,
    InsuredData,
    LineItem,
)
from layers.extraction.service import _build_user_message, cross_validate


def make_extraction(
    total_claimed: float = 150.0,
    event_date: str = "2026-01-15",
    flags: list = None,
    confidence: float = 0.92,
    cross_document: CrossDocumentData | None = None,
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
        cross_document=cross_document,
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


# ── Кросс-документная согласованность (Шаг 25) ───────────────────


def make_cross_document(
    form_name: str = "Иванов Иван Иванович",
    id_name: str = "Иванов Иван Иванович",
    form_birth: str = "1988-03-15",
    id_birth: str = "1988-03-15",
    form_diagnoses: list[str] | None = None,
    receipt_diagnoses: list[str] | None = None,
    form_date: str = "2026-01-15",
    receipt_date: str = "2026-01-15",
    form_institution: str = "Клиника Медикус",
    receipt_institution: str = "Клиника Медикус",
) -> CrossDocumentData:
    return CrossDocumentData(
        form_100=CrossDocForm100(
            full_name=form_name,
            birth_date=form_birth,
            date=form_date,
            institution=form_institution,
            diagnoses=form_diagnoses if form_diagnoses is not None else ["J06.9"],
            total=150.0,
        ),
        id_document=CrossDocIdDocument(
            full_name=id_name,
            birth_date=id_birth,
            personal_id="12345678901",
        ),
        receipt=CrossDocReceipt(
            date=receipt_date,
            institution=receipt_institution,
            diagnoses=receipt_diagnoses if receipt_diagnoses is not None else ["J06.9"],
            total=150.0,
        ),
    )


def test_cross_validate_consistent_documents_no_flags():
    """Согласованные документы не получают mismatch-флагов."""
    extraction = make_extraction(cross_document=make_cross_document())
    updated, warnings = cross_validate(extraction, [], date(2026, 1, 20))

    for flag in ("name_mismatch", "birth_date_mismatch", "diagnosis_mismatch",
                 "date_mismatch", "institution_mismatch"):
        assert flag not in updated.flags
    assert updated.extraction_confidence == pytest.approx(0.92)


def test_cross_validate_without_cross_document_backward_compatible():
    """Без cross_document (старый ответ Claude) — никаких ложных флагов."""
    extraction = make_extraction(cross_document=None)
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))

    for flag in ("name_mismatch", "birth_date_mismatch", "diagnosis_mismatch",
                 "date_mismatch", "institution_mismatch"):
        assert flag not in updated.flags


def test_cross_validate_name_mismatch():
    """ФИО не сравниваются (транслитерация RU/KA/EN): разные имена не дают флага."""
    extraction = make_extraction(cross_document=make_cross_document(
        form_name="Иванов Иван Иванович",
        id_name="Петросян Арам Гарикович",
    ))
    updated, warnings = cross_validate(extraction, [], date(2026, 1, 20))

    # Имена намеренно не сравниваются: верификация личности по personal_id vs getpolicylist.
    assert "name_mismatch" not in updated.flags
    assert updated.extraction_confidence == pytest.approx(0.92)


def test_cross_validate_birth_date_mismatch():
    """Дата рождения не совпадает точно → birth_date_mismatch."""
    extraction = make_extraction(cross_document=make_cross_document(
        form_birth="1988-03-15",
        id_birth="1989-03-15",
    ))
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))
    assert "birth_date_mismatch" in updated.flags


def test_cross_validate_diagnosis_mismatch_by_prefix():
    """Нет общего префикса МКБ-10 между form_100 и чеком → diagnosis_mismatch."""
    extraction = make_extraction(cross_document=make_cross_document(
        form_diagnoses=["J06.9"],
        receipt_diagnoses=["M54.5"],
    ))
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))
    assert "diagnosis_mismatch" in updated.flags


def test_cross_validate_diagnosis_prefix_match_passes():
    """J06.9 и J06.8 имеют общий префикс J06 → нет флага."""
    extraction = make_extraction(cross_document=make_cross_document(
        form_diagnoses=["J06.9"],
        receipt_diagnoses=["J06.8"],
    ))
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))
    assert "diagnosis_mismatch" not in updated.flags


def test_cross_validate_date_mismatch_over_threshold():
    """Расхождение дат между документами > 3 дней → date_mismatch."""
    extraction = make_extraction(cross_document=make_cross_document(
        form_date="2026-01-15",
        receipt_date="2026-01-19",  # 4 дня
    ))
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))
    assert "date_mismatch" in updated.flags


def test_cross_validate_date_within_threshold_passes():
    """Расхождение дат ≤ 3 дней допустимо."""
    extraction = make_extraction(cross_document=make_cross_document(
        form_date="2026-01-15",
        receipt_date="2026-01-17",  # 2 дня
    ))
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))
    assert "date_mismatch" not in updated.flags


def test_cross_validate_institution_mismatch_applies_penalty():
    """Учреждения не совпадают → institution_mismatch + confidence *= 0.85."""
    extraction = make_extraction(confidence=0.90, cross_document=make_cross_document(
        form_institution="Клиника Медикус",
        receipt_institution="Диагностический центр Авангард",
    ))
    updated, _ = cross_validate(extraction, [], date(2026, 1, 20))

    assert "institution_mismatch" in updated.flags
    # -0.05 за новый флаг, затем *0.85 (extraction_institution_mismatch_penalty)
    assert updated.extraction_confidence == pytest.approx((0.90 - 0.05) * 0.85)


# ── Персистентность extraction → ClaimDocument.extracted_data ────


@pytest.mark.asyncio
async def test_persist_extracted_data_sets_per_doc_slices():
    """form_100 получает insured+event, receipt — line_items+total, оба — as_seen_in_document."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.models.claim import DocType
    from layers.extraction.service import _persist_extracted_data
    from layers.ocr.service import OCRResult

    form_id, receipt_id = uuid4(), uuid4()
    form_doc = MagicMock(id=form_id)
    receipt_doc = MagicMock(id=receipt_id)

    scalars = MagicMock()
    scalars.all.return_value = [form_doc, receipt_doc]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    extraction = make_extraction(cross_document=make_cross_document())
    ocr_results = [
        OCRResult(doc_id=form_id, doc_type=DocType.FORM_100, full_text=""),
        OCRResult(doc_id=receipt_id, doc_type=DocType.RECEIPT, full_text=""),
    ]

    await _persist_extracted_data(
        extraction, ocr_results, db,
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert form_doc.extracted_data["insured"]["full_name"] == "Иванов Иван Иванович"
    assert form_doc.extracted_data["event"]["total_claimed"] == 150.0
    assert form_doc.extracted_data["as_seen_in_document"]["diagnoses"] == ["J06.9"]

    assert receipt_doc.extracted_data["total_claimed"] == 150.0
    assert receipt_doc.extracted_data["line_items"][0]["description"] == "Консультация"
    assert receipt_doc.extracted_data["as_seen_in_document"]["institution"] == "Клиника Медикус"

    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_persist_extracted_data_new_doc_types():
    """discharge_summary/lab_result/prescription получают insured+event (как form_100);
    other получает {} — неуверенная классификация, нечего атрибутировать."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.models.claim import DocType
    from layers.extraction.service import _persist_extracted_data
    from layers.ocr.service import OCRResult

    discharge_id, lab_id, rx_id, other_id = uuid4(), uuid4(), uuid4(), uuid4()
    docs = {
        discharge_id: MagicMock(id=discharge_id),
        lab_id: MagicMock(id=lab_id),
        rx_id: MagicMock(id=rx_id),
        other_id: MagicMock(id=other_id),
    }

    scalars = MagicMock()
    scalars.all.return_value = list(docs.values())
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    extraction = make_extraction()
    ocr_results = [
        OCRResult(doc_id=discharge_id, doc_type=DocType.DISCHARGE_SUMMARY, full_text=""),
        OCRResult(doc_id=lab_id, doc_type=DocType.LAB_RESULT, full_text=""),
        OCRResult(doc_id=rx_id, doc_type=DocType.PRESCRIPTION, full_text=""),
        OCRResult(doc_id=other_id, doc_type=DocType.OTHER, full_text=""),
    ]

    await _persist_extracted_data(
        extraction, ocr_results, db,
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    for doc_id in (discharge_id, lab_id, rx_id):
        assert docs[doc_id].extracted_data["insured"]["full_name"] == "Иванов Иван Иванович"
        assert docs[doc_id].extracted_data["event"]["total_claimed"] == 150.0

    assert docs[other_id].extracted_data == {}


# ── LLM-классификация типов документов (_apply_llm_doc_classification) ──


@pytest.mark.asyncio
async def test_apply_llm_doc_classification_writes_doc_type_and_source():
    """Каждый документ получает doc_type/doc_type_source='llm' по своему doc_index."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.models.claim import DocType
    from layers.extraction.service import _apply_llm_doc_classification
    from layers.ocr.service import OCRResult

    form_id, receipt_id = uuid4(), uuid4()
    form_doc = MagicMock(id=form_id)
    receipt_doc = MagicMock(id=receipt_id)

    scalars = MagicMock()
    scalars.all.return_value = [form_doc, receipt_doc]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    ocr_results = [
        OCRResult(doc_id=form_id, doc_type=DocType.OTHER, full_text="form text"),
        OCRResult(doc_id=receipt_id, doc_type=DocType.OTHER, full_text="receipt text"),
    ]
    raw_documents = [
        {"doc_index": 1, "doc_type": "form_100", "doc_type_confidence": 0.95},
        {"doc_index": 2, "doc_type": "receipt", "doc_type_confidence": 0.90},
    ]

    updated, flags = await _apply_llm_doc_classification(
        raw_documents, ocr_results, db,
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert form_doc.doc_type == DocType.FORM_100
    assert form_doc.doc_type_source == "llm"
    assert receipt_doc.doc_type == DocType.RECEIPT
    assert receipt_doc.doc_type_source == "llm"
    assert [r.doc_type for r in updated] == [DocType.FORM_100, DocType.RECEIPT]
    assert flags == []
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_apply_llm_doc_classification_missing_index_flags_unclassified():
    """Отсутствующий doc_index в ответе LLM → флаг unclassified_document, не падение."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.models.claim import DocType
    from layers.extraction.service import _apply_llm_doc_classification
    from layers.ocr.service import OCRResult

    doc_id = uuid4()
    scalars = MagicMock()
    scalars.all.return_value = [MagicMock(id=doc_id)]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    ocr_results = [OCRResult(doc_id=doc_id, doc_type=DocType.FORM_100, full_text="text")]

    updated, flags = await _apply_llm_doc_classification(
        [], ocr_results, db,
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert flags == ["unclassified_document"]
    assert updated[0].doc_type == DocType.FORM_100  # без изменений


@pytest.mark.asyncio
async def test_apply_llm_doc_classification_low_confidence_flag():
    """doc_type_confidence ниже порога → флаг low_confidence_doc_type."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.models.claim import DocType
    from layers.extraction.service import _apply_llm_doc_classification
    from layers.ocr.service import OCRResult

    doc_id = uuid4()
    scalars = MagicMock()
    scalars.all.return_value = [MagicMock(id=doc_id)]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    ocr_results = [OCRResult(doc_id=doc_id, doc_type=DocType.OTHER, full_text="text")]
    raw_documents = [{"doc_index": 1, "doc_type": "lab_result", "doc_type_confidence": 0.30}]

    updated, flags = await _apply_llm_doc_classification(
        raw_documents, ocr_results, db,
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert flags == ["low_confidence_doc_type"]
    assert updated[0].doc_type == DocType.LAB_RESULT


@pytest.mark.asyncio
async def test_apply_llm_doc_classification_other_flags_unclassified():
    """doc_type='other' → флаг unclassified_document."""
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.models.claim import DocType
    from layers.extraction.service import _apply_llm_doc_classification
    from layers.ocr.service import OCRResult

    doc_id = uuid4()
    scalars = MagicMock()
    scalars.all.return_value = [MagicMock(id=doc_id)]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=execute_result)

    ocr_results = [OCRResult(doc_id=doc_id, doc_type=DocType.FORM_100, full_text="illegible")]
    raw_documents = [{"doc_index": 1, "doc_type": "other", "doc_type_confidence": 0.40}]

    updated, flags = await _apply_llm_doc_classification(
        raw_documents, ocr_results, db,
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert flags == ["unclassified_document"]
    assert updated[0].doc_type == DocType.OTHER


def test_build_user_message_includes_all_docs(sample_ocr_result):
    """Промпт содержит текст всех документов, нейтрально пронумерованных
    (тип документа больше не предполагается лейблом — его определяет LLM)."""
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
    assert "ДОКУМЕНТ #1" in message
    assert "ДОКУМЕНТ #2" in message
    assert "ФОРМА 100" not in message
    assert "ДОКУМЕНТ УДОСТОВЕРЯЮЩИЙ ЛИЧНОСТЬ" not in message
    assert "J06.9" in message
    assert "12345678901" in message
