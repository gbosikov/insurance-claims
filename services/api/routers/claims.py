"""
Router: /v1/claims — приём заявок и статус.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.exceptions import FileTooLargeError, UnsupportedFileTypeError
from core.models.audit import AuditLog
from core.models.claim import Claim
from core.schemas.claim import ClaimResponse, ClaimStatusResponse
from core.storage import get_storage_client
from layers.intake.service import receive_claim
from services.worker.celery_app import celery_app

router = APIRouter()

DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


@router.post("", response_model=ClaimResponse, status_code=201)
async def create_claim(
    policy_number: str = Form(..., description="Номер медицинской карточки (обязательно)"),
    files: list[UploadFile] = File(..., description="Документы заявки (form_100, id_document)"),
    client_reference: str | None = Form(None, description="Внешний ID клиента"),
    db: AsyncSession = Depends(get_db),
):
    """
    Принять новую страховую заявку.

    Обязательные параметры:
    - policy_number — номер медицинской карточки
    - files — документы: Форма 100 и документ удостоверяющий личность

    Форматы файлов: JPEG, PNG, PDF. Максимальный размер: 20 МБ.
    """
    storage = get_storage_client()
    try:
        return await receive_claim(
            tenant_id=DEFAULT_TENANT_ID,
            policy_number=policy_number,
            files=files,
            client_reference=client_reference,
            db=db,
            storage=storage,
            celery_app=celery_app,
        )
    except UnsupportedFileTypeError as e:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {e.mime_type}")
    except FileTooLargeError as e:
        raise HTTPException(status_code=413, detail=f"File too large: {e.size_mb:.1f} MB > {e.max_mb} MB")


@router.get("/{claim_id}", response_model=ClaimStatusResponse)
async def get_claim_status(
    claim_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Получить текущий статус заявки."""
    result = await db.execute(
        select(Claim).where(
            Claim.id == claim_id,
            Claim.tenant_id == DEFAULT_TENANT_ID,
        )
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    return ClaimStatusResponse.model_validate(claim)


@router.get("/{claim_id}/audit")
async def get_claim_audit(
    claim_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Получить аудит-лог заявки (для операторов и аудиторов)."""
    result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.claim_id == claim_id,
            AuditLog.tenant_id == DEFAULT_TENANT_ID,
        )
        .order_by(AuditLog.timestamp)
    )
    entries = result.scalars().all()

    return {
        "claim_id": str(claim_id),
        "entries": [
            {
                "id": entry.id,
                "step": entry.step,
                "timestamp": entry.timestamp.isoformat(),
                "confidence": entry.confidence,
                "rag_chunks": entry.rag_chunks,
                "prompt_version": entry.prompt_version,
                "model_version": entry.model_version,
                "duration_ms": entry.duration_ms,
                "operator_id": str(entry.operator_id) if entry.operator_id else None,
                "override_reason": entry.override_reason,
            }
            for entry in entries
        ],
    }
