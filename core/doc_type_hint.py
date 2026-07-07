"""
Определение типа документа по имени файла (Layer 1 — intake).

Грубая эвристика для первичной метки до OCR/LLM-классификации: используется
только как отправная точка (doc_type_source="filename_hint"), позже
переопределяется LLM (Layer 4, doc_type_source="llm") или regex-классификатором
в rule-based режиме (doc_type_source="ocr_rules").

Паттерны синхронизированы вручную с client-side JS в
services/api/routers/devtools.py (переиспользовать оттуда нельзя — это JS в HTML,
не Python).
"""

from __future__ import annotations

import re

from core.models.claim import DocType

_FILENAME_PATTERNS: list[tuple[DocType, re.Pattern]] = [
    (DocType.FORM_100, re.compile(r"form.?100|ф100|форм", re.IGNORECASE)),
    (DocType.ID_DOCUMENT, re.compile(r"id|passport|паспорт|personal", re.IGNORECASE)),
    (DocType.RECEIPT, re.compile(r"receipt|чек|invoice|bill|kvit", re.IGNORECASE)),
]


def guess_doc_type_from_filename(filename: str | None) -> DocType:
    """Best-effort догадка по имени файла. DocType.OTHER, если ничего не совпало."""
    name = filename or ""
    for doc_type, pattern in _FILENAME_PATTERNS:
        if pattern.search(name):
            return doc_type
    return DocType.OTHER
