"""
Router: /v1/contracts — загрузка и индексация контрактов.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.storage import get_storage_client

router = APIRouter()

DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


@router.post("", status_code=202)
async def upload_contract(
    policy_number: str = Form(..., description="Номер полиса"),
    valid_from: str = Form(..., description="Дата начала действия (YYYY-MM-DD)"),
    file: UploadFile = File(..., description="PDF контракта"),
    db: AsyncSession = Depends(get_db),
):
    """
    Загрузить контракт и поставить в очередь индексации.

    Индексация выполняется асинхронно (Celery).
    Проверьте статус через GET /v1/contracts/{policy_number}.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="Only PDF files are accepted")

    pdf_bytes = await file.read()
    storage = get_storage_client()

    pdf_path = f"tenants/{DEFAULT_TENANT_ID}/contracts/{policy_number}/raw_{valid_from}.pdf"
    await storage.upload(pdf_bytes, pdf_path, content_type="application/pdf")

    # Ставим задачу в очередь
    from services.worker.celery_app import celery_app
    celery_app.send_task(
        "index_contract",
        kwargs={
            "tenant_id": str(DEFAULT_TENANT_ID),
            "policy_number": policy_number,
            "pdf_storage_path": pdf_path,
            "valid_from": valid_from,
        },
        queue="contracts",
    )

    return {
        "status": "queued",
        "policy_number": policy_number,
        "message": "Contract indexing has been queued. Check status in a few minutes.",
    }


@router.get("/{policy_number}")
async def get_contract_status(
    policy_number: str,
    db: AsyncSession = Depends(get_db),
):
    """Получить статус индексации контракта."""
    from sqlalchemy import select
    from core.models.contract import ContractVersion

    result = await db.execute(
        select(ContractVersion).where(
            ContractVersion.tenant_id == DEFAULT_TENANT_ID,
            ContractVersion.policy_number == policy_number,
        ).order_by(ContractVersion.created_at.desc())
    )
    versions = result.scalars().all()

    if not versions:
        raise HTTPException(status_code=404, detail="Contract not found")

    return {
        "policy_number": policy_number,
        "versions": [
            {
                "version_id": v.version_id,
                "valid_from": v.valid_from.isoformat(),
                "valid_to": v.valid_to.isoformat() if v.valid_to else None,
                "content_hash": v.content_hash[:16] if v.content_hash else None,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
    }
