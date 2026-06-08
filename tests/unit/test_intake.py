"""
Unit тесты: Слой 1 — Intake Service + Downloader.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from core.exceptions import DocumentQualityError, FileTooLargeError, UnsupportedFileTypeError
from core.schemas.claim import ClaimCreateRequest, DocumentRef


# ── Хелперы ───────────────────────────────────────────────────────

def _make_request(
    policy_number: str = "DMC-TEST-001",
    urls: list[tuple[str, str]] | None = None,
    client_reference: str | None = None,
) -> ClaimCreateRequest:
    if urls is None:
        urls = [
            ("https://medsystem.example.com/scan001.jpg", "scan001.jpg"),
            ("https://medsystem.example.com/scan002.pdf", "scan002.pdf"),
        ]
    return ClaimCreateRequest(
        policy_number=policy_number,
        client_reference=client_reference,
        documents=[DocumentRef(url=u, filename=f) for u, f in urls],
    )


def _make_db_mock() -> AsyncMock:
    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


# ── receive_claim ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_receive_claim_empty_policy_number_raises_422():
    """Пустой policy_number → HTTPException 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim

    with pytest.raises(HTTPException) as exc_info:
        await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            request=_make_request(policy_number="   "),
            db=_make_db_mock(),
            celery_app=MagicMock(),
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_receive_claim_empty_documents_raises_422():
    """Пустой список documents → HTTPException 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim

    with pytest.raises(HTTPException) as exc_info:
        await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            request=ClaimCreateRequest(policy_number="DMC-001", documents=[]),
            db=_make_db_mock(),
            celery_app=MagicMock(),
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_receive_claim_invalid_url_raises_422():
    """Невалидный URL → HTTPException 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim

    request = _make_request(urls=[("not-a-url", "file.pdf")])

    with pytest.raises(HTTPException) as exc_info:
        await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            request=request,
            db=_make_db_mock(),
            celery_app=MagicMock(),
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_receive_claim_success():
    """Валидный запрос — заявка создана, задача поставлена в очередь."""
    from layers.intake.service import receive_claim

    db = _make_db_mock()
    celery = MagicMock()

    mock_claim = MagicMock()
    mock_claim.id = UUID("11111111-1111-1111-1111-111111111111")
    mock_claim.status = MagicMock(value="RECEIVED")

    with patch("layers.intake.service.Claim", return_value=mock_claim), \
         patch("layers.intake.service.ClaimDocument") as mock_doc_cls, \
         patch("layers.intake.service.write_audit_entry", AsyncMock()):
        mock_doc_cls.return_value = MagicMock()

        result = await receive_claim(
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            request=_make_request(),
            db=db,
            celery_app=celery,
        )

    celery.send_task.assert_called_once_with(
        "process_claim",
        kwargs={
            "claim_id": "11111111-1111-1111-1111-111111111111",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert result is not None


# ── downloader: _check_trusted_host ──────────────────────────────

def test_trusted_host_ok():
    """URL с разрешённым доменом проходит без ошибки."""
    from layers.intake.downloader import _check_trusted_host
    _check_trusted_host("https://medsystem.example.com/file.pdf", ["medsystem.example.com"])


def test_trusted_host_blocked():
    """URL с неизвестным доменом → DocumentQualityError."""
    from layers.intake.downloader import _check_trusted_host
    with pytest.raises(DocumentQualityError):
        _check_trusted_host("https://evil.com/file.pdf", ["medsystem.example.com"])


def test_trusted_host_empty_whitelist_dev(monkeypatch):
    """Пустой whitelist в dev-режиме — разрешить (с warning)."""
    from layers.intake import downloader
    monkeypatch.setattr(downloader.settings, "environment", "development")
    # Не должно кидать исключение
    downloader._check_trusted_host("https://any-host.com/file.pdf", [])


def test_trusted_host_empty_whitelist_prod(monkeypatch):
    """Пустой whitelist в production → DocumentQualityError."""
    from layers.intake import downloader
    monkeypatch.setattr(downloader.settings, "environment", "production")
    with pytest.raises(DocumentQualityError):
        downloader._check_trusted_host("https://any-host.com/file.pdf", [])


# ── downloader: _download_one ─────────────────────────────────────

@pytest.mark.asyncio
async def test_download_one_unsupported_mime():
    """Файл с неподдерживаемым MIME-типом → UnsupportedFileTypeError."""
    from layers.intake.downloader import _download_one
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_response = MagicMock()
    mock_response.content = b"fake"
    mock_response.headers = {"content-type": "application/zip"}
    mock_response.raise_for_status = MagicMock()

    doc = MagicMock()
    doc.source_url = "https://medsystem.example.com/archive.zip"
    doc.claim_id = UUID("22222222-2222-2222-2222-222222222222")
    doc.id = UUID("33333333-3333-3333-3333-333333333333")

    with patch("layers.intake.downloader.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        with pytest.raises(UnsupportedFileTypeError):
            await _download_one(doc, ["medsystem.example.com"], MagicMock(), UUID("00000000-0000-0000-0000-000000000001"))


@pytest.mark.asyncio
async def test_download_one_too_large():
    """Файл > 20 МБ → FileTooLargeError."""
    from layers.intake.downloader import _download_one

    mock_response = MagicMock()
    mock_response.content = b"x" * (21 * 1024 * 1024)
    mock_response.headers = {"content-type": "image/jpeg"}
    mock_response.raise_for_status = MagicMock()

    doc = MagicMock()
    doc.source_url = "https://medsystem.example.com/huge.jpg"
    doc.claim_id = UUID("22222222-2222-2222-2222-222222222222")
    doc.id = UUID("33333333-3333-3333-3333-333333333333")

    with patch("layers.intake.downloader.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        with pytest.raises(FileTooLargeError):
            await _download_one(doc, ["medsystem.example.com"], MagicMock(), UUID("00000000-0000-0000-0000-000000000001"))
