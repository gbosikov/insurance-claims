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
        # Только hostname: полный pre-signed URL содержит действующий токен доступа
        log.warning("download_host_whitelist_empty_dev_mode", host=urlparse(url).hostname)
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
    record: dict | None = None,
) -> None:
    """Скачать один документ и сохранить в storage.

    record (если передан) заполняется деталями для audit_log:
    http_status, resolved_mime, size_bytes, duration_ms, ok, error.
    """
    import time

    if record is None:
        record = {}
    started = time.monotonic()

    try:
        _check_trusted_host(doc.source_url, allowed_hosts)

        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
            response = await client.get(doc.source_url)
            record["http_status"] = response.status_code
            response.raise_for_status()

        data = response.content
        record["size_bytes"] = len(data)

        # Определяем MIME-тип из заголовка ответа, затем fallback по имени файла из URL
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type not in ALLOWED_MIME_TYPES:
            filename = urlparse(doc.source_url).path.split("/")[-1]
            guessed, _ = mimetypes.guess_type(filename)
            content_type = guessed or ""
        record["resolved_mime"] = content_type or None

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
        record["ok"] = True
        log.info("document_downloaded", doc_id=str(doc.id), storage_path=storage_path)
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        record["duration_ms"] = int((time.monotonic() - started) * 1000)


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
    Каждый документ скачивается последовательно — при ошибке pipeline останавливается,
    но per-file результаты фиксируются в audit_log до пропагирования ошибки
    (audit-first, как в preprocessing).
    """
    records: list[dict] = []

    async def _write_download_audit() -> None:
        await write_audit_entry(
            db,
            claim_id=claim_id,
            tenant_id=tenant_id,
            step="download",
            output_data={
                "files": records,
                "downloaded_count": sum(1 for r in records if r.get("ok")),
                "total_count": len(records),
            },
        )

    try:
        for doc in documents:
            if not doc.source_url:
                continue  # документ уже в storage (не должно быть на шаге 0, но защита)

            record: dict = {
                "doc_id": str(doc.id),
                "host": urlparse(doc.source_url).hostname or "",
                "ok": False,
            }
            records.append(record)
            await _download_one(doc, allowed_hosts, storage, tenant_id, record)
    except Exception:
        await db.flush()
        await _write_download_audit()
        raise

    await db.flush()
    await _write_download_audit()
