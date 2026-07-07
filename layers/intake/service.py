"""
Слой 1 — Intake Service.

Задача: принять ссылки на документы, создать запись заявки в БД,
поставить задачу в очередь Celery.
Файлы скачиваются в worker (шаг 0) — не здесь.
"""

from __future__ import annotations

from urllib.parse import urlparse
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.doc_type_hint import guess_doc_type_from_filename
from core.models.claim import Claim, ClaimDocument, ClaimStatus
from core.schemas.claim import ClaimCreateRequest, ClaimResponse

log = structlog.get_logger()
settings = get_settings()


def _validate_url(url: str) -> None:
    """Проверить что строка является валидным HTTP/HTTPS URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Недопустимый URL: {url!r}")


async def receive_claim(
    *,
    tenant_id: UUID,
    request: ClaimCreateRequest,
    db: AsyncSession,
    celery_app: object,
) -> ClaimResponse:
    """
    Точка входа для новой заявки.

    1. Валидация policy_number
    2. Валидация URL каждого документа (формат http/https)
    3. Создание записи Claim в БД
    4. Создание записей ClaimDocument (source_url, storage_path=None)
    5. Запись в audit_log: step=intake
    6. Постановка задачи process_claim в Celery
    7. Возврат claim_id клиенту
    """
    if not request.policy_number or not request.policy_number.strip():
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="policy_number is required")

    if not request.documents:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="documents list is empty")

    policy_number = request.policy_number.strip()

    # Валидация URL до записи в БД
    for doc_ref in request.documents:
        try:
            _validate_url(doc_ref.url)
        except ValueError as e:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail=str(e))

    log.info(
        "claim_received",
        tenant_id=str(tenant_id),
        policy_number=policy_number,
        documents_count=len(request.documents),
    )

    with AuditTimer() as timer:
        # Шаг 1: Создание записи заявки
        claim = Claim(
            tenant_id=tenant_id,
            policy_number=policy_number,
            status=ClaimStatus.RECEIVED,
            client_reference=request.client_reference,
        )
        db.add(claim)
        await db.flush()  # получаем claim.id

        # Шаг 2: Создание ClaimDocument для каждой ссылки
        doc_records: list[dict] = []
        for doc_ref in request.documents:
            doc = ClaimDocument(
                claim_id=claim.id,
                tenant_id=tenant_id,
                doc_type=guess_doc_type_from_filename(doc_ref.filename),
                doc_type_source="filename_hint",  # слой 4 переопределит по OCR/LLM
                source_url=doc_ref.url,
                storage_path=None,           # заполнит worker после скачивания
            )
            db.add(doc)
            doc_records.append({
                "filename": doc_ref.filename,
                "source_url": doc_ref.url,
            })

        await db.flush()

    # Шаг 3: Аудит-лог
    await write_audit_entry(
        db,
        claim_id=claim.id,
        tenant_id=tenant_id,
        step="intake",
        input_data={
            "policy_number": policy_number,
            "documents_count": len(request.documents),
            "client_reference": request.client_reference,
        },
        output_data={"documents": doc_records, "claim_id": str(claim.id)},
        duration_ms=timer.duration_ms,
    )

    await db.commit()

    # Шаг 4: Ставим задачу в очередь (после commit — worker видит данные в БД)
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
