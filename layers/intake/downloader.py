"""
Слой 1 — Document Downloader.

Запускается в Celery worker (шаг 0) перед preprocessing.
Скачивает файлы по source_url, валидирует формат и размер,
загружает в наш storage, обновляет ClaimDocument.storage_path.
"""

from __future__ import annotations

import mimetypes
from urllib.parse import urlparse
from uuid import UUID

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import write_audit_entry
from core.config import get_settings
from core.exceptions import DocumentQualityError, FileTooLargeError, UnsupportedFileTypeError
from core.models.claim import ClaimDocument
from core.storage import StorageClient

log = structlog.get_logger()
settings = get_settings()

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "application/pdf"}
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
DOWNLOAD_TIMEOUT_SEC = 30


def _check_trusted_host(url: str, allowed_hosts: list[str]) -> None:
    """Проверить hostname URL против whitelist. Пустой список = разрешить всё (только dev)."""
    if not allowed_hosts:
        if settings.environment == "production":
            raise DocumentQualityError(
                reason="untrusted_source",
                detail="Whitelist доменов не настроен. Обратитесь к администратору.",
            )
        log.warning("download_host_whitelist_empty_dev_mode", url=url)
        return

    hostname = urlparse(url).hostname or ""
    if hostname not in allowed_hosts:
        raise DocumentQualityError(
            reason="untrusted_source",
            detail=f"Домен {hostname!r} не входит в список разрешённых источников.",
        )


async def _download_one(
    doc: ClaimDocument,
    allowed_hosts: list[str],
    storage: StorageClient,
    tenant_id: UUID,
) -> None:
    """Скачать один документ и сохранить в storage."""
    _check_trusted_host(doc.source_url, allowed_hosts)

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
        response = await client.get(doc.source_url)
        response.raise_for_status()

    data = response.content

    # Определяем MIME-тип из заголовка ответа, затем fallback по имени файла из URL
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in ALLOWED_MIME_TYPES:
        filename = urlparse(doc.source_url).path.split("/")[-1]
        guessed, _ = mimetypes.guess_type(filename)
        content_type = guessed or ""

    if content_type not in ALLOWED_MIME_TYPES:
        raise UnsupportedFileTypeError(content_type)

    if len(data) > MAX_FILE_SIZE_BYTES:
        raise FileTooLargeError(len(data) / (1024 * 1024), MAX_FILE_SIZE_BYTES / (1024 * 1024))

    # Имя файла: из URL path или дефолт
    filename = urlparse(doc.source_url).path.split("/")[-1] or "upload.bin"

    storage_path = storage.generate_path(
        tenant_id=str(tenant_id),
        claim_id=str(doc.claim_id),
        filename=filename,
    )
    await storage.upload(data, storage_path, content_type=content_type)

    doc.storage_path = storage_path
    log.info("document_downloaded", doc_id=str(doc.id), storage_path=storage_path)


async def download_all_documents(
    documents: list[ClaimDocument],
    allowed_hosts: list[str],
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
    claim_id: UUID,
) -> None:
    """
    Шаг 0 pipeline: скачать все документы заявки.

    allowed_hosts берётся из platform.tenant_configs['allowed_download_hosts'].
    Каждый документ скачивается последовательно — при ошибке pipeline останавливается.
    После успешного скачивания всех документов делаем db.flush().
    """
    for doc in documents:
        if not doc.source_url:
            continue  # документ уже в storage (не должно быть на шаге 0, но защита)

        await _download_one(doc, allowed_hosts, storage, tenant_id)

    await db.flush()

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="download",
        output_data={"downloaded": [str(d.id) for d in documents if d.storage_path]},
    )
