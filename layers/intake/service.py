"""
Слой 1 — Intake Service.

Задача: принять документы, проверить форматы, сохранить оригиналы,
создать запись заявки в БД, поставить задачу в очередь Celery.
"""

from __future__ import annotations

import hashlib
import mimetypes
import time
from uuid import UUID

import structlog
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import FileTooLargeError, UnsupportedFileTypeError
from core.models.claim import Claim, ClaimDocument, ClaimStatus, DocType
from core.schemas.claim import ClaimResponse
from core.storage import StorageClient

log = structlog.get_logger()
settings = get_settings()

# ── Константы ─────────────────────────────────────────────────────

ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "application/pdf"}
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB

# Ключевые слова в имени файла → тип документа
DOC_TYPE_HINTS: dict[str, list[str]] = {
    "form_100":    ["form100", "form-100", "форма", "направление", "act", "акт"],
    "id_document": ["passport", "id", "паспорт", "удостоверение", "license"],
    "receipt":     ["receipt", "check", "чек", "квитанция", "invoice"],
}

# Обязательные типы документов для заявки
REQUIRED_DOC_TYPES = {DocType.FORM_100, DocType.ID_DOCUMENT, DocType.RECEIPT}


def detect_doc_type(filename: str, mime_type: str) -> DocType:
    """Определить тип документа по имени файла."""
    lower = filename.lower()
    for doc_type_str, hints in DOC_TYPE_HINTS.items():
        if any(hint in lower for hint in hints):
            return DocType(doc_type_str)
    # Fallback: если не определили — form_100 (наиболее частый)
    return DocType.FORM_100


async def validate_file(file: UploadFile) -> bytes:
    """
    Проверить файл на допустимость.
    Возвращает содержимое файла или бросает исключение.
    """
    # Определяем MIME-тип
    mime_type = file.content_type or ""
    if not mime_type:
        # Пытаемся определить по имени файла
        guessed, _ = mimetypes.guess_type(file.filename or "")
        mime_type = guessed or ""

    if mime_type not in ALLOWED_MIME_TYPES:
        raise UnsupportedFileTypeError(mime_type)

    data = await file.read()

    if len(data) > MAX_FILE_SIZE_BYTES:
        size_mb = len(data) / (1024 * 1024)
        raise FileTooLargeError(size_mb, MAX_FILE_SIZE_BYTES / (1024 * 1024))

    return data


async def receive_claim(
    *,
    tenant_id: UUID,
    policy_number: str,
    files: list[UploadFile],
    client_reference: str | None,
    db: AsyncSession,
    storage: StorageClient,
    celery_app: object,  # Celery app — передаём как Any чтобы избежать circular import
) -> ClaimResponse:
    """
    Точка входа для новой заявки.

    1. Валидация policy_number
    2. Валидация каждого файла (формат, размер)
    3. Определение типа документа
    4. Сохранение оригиналов в storage
    5. Создание записи Claim в БД (policy_number сохраняется сразу)
    6. Создание записей ClaimDocument
    7. Постановка задачи process_claim в Celery
    8. Запись в audit_log: step=intake
    9. Возврат claim_id клиенту
    """
    if not policy_number or not policy_number.strip():
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="policy_number is required")

    policy_number = policy_number.strip()
    log.info("claim_received", tenant_id=str(tenant_id), policy_number=policy_number, files_count=len(files))

    with AuditTimer() as timer:
        # Шаг 1: Валидация и чтение файлов
        validated_files: list[tuple[UploadFile, bytes, DocType]] = []
        for file in files:
            data = await validate_file(file)
            doc_type = detect_doc_type(file.filename or "", file.content_type or "")
            validated_files.append((file, data, doc_type))

        # Проверка комплектности документов
        detected_types = {doc_type for _, _, doc_type in validated_files}
        missing_types = REQUIRED_DOC_TYPES - detected_types
        if missing_types:
            from fastapi import HTTPException
            missing_names = sorted(t.value for t in missing_types)
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "missing_required_documents",
                    "missing": missing_names,
                    "message": f"Отсутствуют обязательные документы: {', '.join(missing_names)}",
                },
            )

        # Шаг 2: Создание записи заявки
        claim = Claim(
            tenant_id=tenant_id,
            policy_number=policy_number,      # сохраняем сразу — не из extraction
            status=ClaimStatus.RECEIVED,
            client_reference=client_reference,
        )
        db.add(claim)
        await db.flush()  # получаем claim.id

        # Шаг 3 & 4: Сохранение файлов + создание ClaimDocument
        doc_records: list[dict] = []
        for file, data, doc_type in validated_files:
            # Генерируем путь и сохраняем в storage
            storage_path = storage.generate_path(
                tenant_id=str(tenant_id),
                claim_id=str(claim.id),
                filename=file.filename or f"{doc_type.value}.bin",
            )
            await storage.upload(data, storage_path, content_type=file.content_type or "application/octet-stream")

            doc = ClaimDocument(
                claim_id=claim.id,
                tenant_id=tenant_id,
                doc_type=doc_type,
                storage_path=storage_path,
            )
            db.add(doc)
            doc_records.append({
                "doc_type": doc_type.value,
                "filename": file.filename,
                "size_bytes": len(data),
                "storage_path": storage_path,
            })

        await db.flush()

    # Шаг 5: Аудит-лог
    await write_audit_entry(
        db,
        claim_id=claim.id,
        tenant_id=tenant_id,
        step="intake",
        input_data={"policy_number": policy_number, "files_count": len(files), "client_reference": client_reference},
        output_data={"documents": doc_records, "claim_id": str(claim.id)},
        duration_ms=timer.duration_ms,
    )

    await db.commit()

    # Шаг 6: Ставим задачу в очередь (после commit — чтобы worker видел данные в БД)
    celery_app.send_task(
        "process_claim",
        kwargs={"claim_id": str(claim.id), "tenant_id": str(tenant_id)},
    )

    log.info("claim_queued", claim_id=str(claim.id))

    return ClaimResponse(
        claim_id=claim.id,
        status=claim.status.value,
        estimated_completion_sec=300,
    )
