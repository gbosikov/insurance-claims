"""
Unit тесты: Слой 1 — Intake Service.
"""

import pytest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from fastapi import UploadFile

from core.exceptions import FileTooLargeError, UnsupportedFileTypeError
from layers.intake.service import detect_doc_type, validate_file
from core.models.claim import DocType


def make_upload_file(filename: str, content_type: str, data: bytes = b"x") -> UploadFile:
    file = MagicMock(spec=UploadFile)
    file.filename = filename
    file.content_type = content_type
    file.read = AsyncMock(return_value=data)
    return file


@pytest.mark.asyncio
async def test_validate_file_ok():
    """Допустимый JPEG проходит валидацию."""
    file = make_upload_file("test.jpg", "image/jpeg", b"x" * 100)
    data = await validate_file(file)
    assert data == b"x" * 100


@pytest.mark.asyncio
async def test_validate_file_unsupported_mime():
    """Неподдерживаемый MIME-тип → UnsupportedFileTypeError."""
    file = make_upload_file("test.docx", "application/msword", b"x")
    with pytest.raises(UnsupportedFileTypeError):
        await validate_file(file)


@pytest.mark.asyncio
async def test_validate_file_too_large():
    """Файл > 20 МБ → FileTooLargeError."""
    big_data = b"x" * (21 * 1024 * 1024)
    file = make_upload_file("big.pdf", "application/pdf", big_data)
    with pytest.raises(FileTooLargeError):
        await validate_file(file)


def test_detect_doc_type_form_100():
    assert detect_doc_type("форма100.pdf", "application/pdf") == DocType.FORM_100


def test_detect_doc_type_id_document():
    assert detect_doc_type("passport_scan.jpg", "image/jpeg") == DocType.ID_DOCUMENT


def test_detect_doc_type_receipt():
    assert detect_doc_type("квитанция.pdf", "application/pdf") == DocType.RECEIPT


def test_detect_doc_type_fallback():
    """Нераспознанное имя → form_100 (fallback)."""
    assert detect_doc_type("document.pdf", "application/pdf") == DocType.FORM_100
