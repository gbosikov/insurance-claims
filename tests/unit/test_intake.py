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


# ── receive_claim: проверка комплектности документов ──────────────

def _make_files(*names_and_types: tuple[str, str]) -> list:
    """Создаёт список UploadFile-заглушек."""
    files = []
    for filename, content_type in names_and_types:
        f = MagicMock(spec=UploadFile)
        f.filename = filename
        f.content_type = content_type
        f.read = AsyncMock(return_value=b"fake_content")
        files.append(f)
    return files


@pytest.mark.asyncio
async def test_receive_claim_missing_documents_raises_422():
    """Заявка без всех трёх типов документов → HTTPException 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim

    # Только форма 100 — нет id_document и receipt
    files = _make_files(("форма100.pdf", "application/pdf"))

    db = AsyncMock()
    storage = AsyncMock()
    celery = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="DMC-TEST-001",
            files=files,
            client_reference=None,
            db=db,
            storage=storage,
            celery_app=celery,
        )

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error"] == "missing_required_documents"
    assert "id_document" in detail["missing"]
    assert "receipt" in detail["missing"]


@pytest.mark.asyncio
async def test_receive_claim_missing_one_document_raises_422():
    """Заявка с двумя типами (нет receipt) → 422, missing содержит только receipt."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim

    files = _make_files(
        ("форма100.pdf", "application/pdf"),
        ("passport.jpg", "image/jpeg"),
    )

    db = AsyncMock()
    storage = AsyncMock()
    celery = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="DMC-TEST-001",
            files=files,
            client_reference=None,
            db=db,
            storage=storage,
            celery_app=celery,
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["missing"] == ["receipt"]


@pytest.mark.asyncio
async def test_receive_claim_all_documents_proceeds():
    """Заявка со всеми тремя типами документов проходит валидацию комплектности."""
    from layers.intake.service import receive_claim

    files = _make_files(
        ("форма100.pdf",  "application/pdf"),
        ("passport.jpg",  "image/jpeg"),
        ("квитанция.pdf", "application/pdf"),
    )

    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()

    storage = AsyncMock()
    storage.generate_path = MagicMock(return_value="tenants/x/claims/y/file.pdf")
    storage.upload = AsyncMock()

    celery = MagicMock()

    mock_claim = MagicMock()
    mock_claim.id = UUID("11111111-1111-1111-1111-111111111111")
    mock_claim.status = MagicMock(value="RECEIVED")

    with patch("layers.intake.service.Claim", return_value=mock_claim) as mock_claim_cls, \
         patch("layers.intake.service.ClaimDocument") as mock_doc_cls, \
         patch("layers.intake.service.write_audit_entry", AsyncMock()):
        mock_doc_cls.return_value = MagicMock()

        result = await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="DMC-TEST-001",
            files=files,
            client_reference=None,
            db=db,
            storage=storage,
            celery_app=celery,
        )

    # Дошли до конца без HTTPException — клейм создан, задача поставлена
    celery.send_task.assert_called_once()
    assert result is not None
