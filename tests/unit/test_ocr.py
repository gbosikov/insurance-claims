"""Unit тесты: Слой 3 — OCR Service (confidence Document AI)."""

from types import SimpleNamespace

from layers.ocr.service import _document_ai_text_confidence


def make_page(confidence: float) -> SimpleNamespace:
    return SimpleNamespace(layout=SimpleNamespace(confidence=confidence))


def make_entity(confidence: float) -> SimpleNamespace:
    return SimpleNamespace(confidence=confidence)


def test_confidence_from_page_layouts():
    """Приоритет: среднее по layout.confidence страниц."""
    document = SimpleNamespace(
        pages=[make_page(0.95), make_page(0.85)],
        entities=[make_entity(0.10)],  # игнорируется при наличии страниц
    )
    assert abs(_document_ai_text_confidence(document) - 0.90) < 1e-9


def test_confidence_falls_back_to_entities():
    """Нет confidence страниц → среднее по entities."""
    document = SimpleNamespace(
        pages=[],
        entities=[make_entity(0.80), make_entity(0.60)],
    )
    assert abs(_document_ai_text_confidence(document) - 0.70) < 1e-9


def test_confidence_zero_when_unavailable():
    """Нет ни страниц, ни entities → 0.0 (не выдуманный хардкод)."""
    document = SimpleNamespace(pages=[], entities=[])
    assert _document_ai_text_confidence(document) == 0.0


def test_confidence_skips_zero_layout_confidence():
    """Страницы с нулевым/отсутствующим confidence не учитываются."""
    document = SimpleNamespace(
        pages=[make_page(0.0), make_page(0.90)],
        entities=[],
    )
    assert _document_ai_text_confidence(document) == 0.90
