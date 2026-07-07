"""Unit тесты: core/doc_type_hint.py — filename-based догадка типа документа (Layer 1)."""

import pytest

from core.doc_type_hint import guess_doc_type_from_filename
from core.models.claim import DocType


@pytest.mark.parametrize("filename,expected", [
    ("form100_scan.pdf", DocType.FORM_100),
    ("Ф100_ivanov.jpg", DocType.FORM_100),
    ("форма.pdf", DocType.FORM_100),
    ("passport.jpg", DocType.ID_DOCUMENT),
    ("паспорт_иванов.png", DocType.ID_DOCUMENT),
    ("id_card.pdf", DocType.ID_DOCUMENT),
    ("receipt_01.pdf", DocType.RECEIPT),
    ("чек.jpg", DocType.RECEIPT),
    ("invoice_2026.pdf", DocType.RECEIPT),
])
def test_guess_doc_type_from_filename_matches(filename: str, expected: DocType) -> None:
    assert guess_doc_type_from_filename(filename) == expected


@pytest.mark.parametrize("filename", ["scan001.jpg", "document.pdf", "", None])
def test_guess_doc_type_from_filename_falls_back_to_other(filename) -> None:
    assert guess_doc_type_from_filename(filename) == DocType.OTHER
