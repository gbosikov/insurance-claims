"""
Router: /v1/contracts — загрузка и индексация контрактов.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_tenant_id
from core.database import get_db
from core.storage import get_storage_client

router = APIRouter()


@router.post("", status_code=202)
async def upload_contract(
    policy_number: str = Form(..., description="Номер полиса"),
    valid_from: str = Form(..., description="Дата начала действия (YYYY-MM-DD)"),
    file: UploadFile = File(..., description="PDF контракта"),
    db: AsyncSession = Depends(get_db),
    tenant_id: UUID = Depends(get_tenant_id),
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

    pdf_path = f"tenants/{tenant_id}/contracts/{policy_number}/raw_{valid_from}.pdf"
    await storage.upload(pdf_bytes, pdf_path, content_type="application/pdf")

    # Ставим задачу в очередь
    from services.worker.celery_app import celery_app
    celery_app.send_task(
        "index_contract",
        kwargs={
            "tenant_id": str(tenant_id),
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
    tenant_id: UUID = Depends(get_tenant_id),
):
    """Получить статус индексации контракта."""
    from sqlalchemy import select
    from core.models.contract import ContractVersion

    result = await db.execute(
        select(ContractVersion).where(
            ContractVersion.tenant_id == tenant_id,
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


@router.post("/{policy_number}/reindex", status_code=202)
async def reindex_contract(
    policy_number: str,
    version_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    tenant_id: UUID = Depends(get_tenant_id),
):
    """
    Переиндексировать CARVEOUT и POSITIVE LIST для контракта.

    Повторно парсирует CARVEOUT-условия и POSITIVE LIST процедуры из текущей версии контракта.
    Удаляет старые структурированные данные и создаёт новые.

    Используется при обновлении контракта в кор-системе или для ручного переиндексирования.

    Args:
        policy_number: Номер полиса
        version_id: ID версии (если None → используется последняя версия)

    Returns:
        {
            "status": "queued",
            "policy_number": "...",
            "version_id": "v20240609",
            "message": "..."
        }
    """
    from sqlalchemy import desc, select
    from core.models.contract import ContractVersion
    from services.worker.celery_app import celery_app

    # Найти версию контракта
    query = select(ContractVersion).where(
        ContractVersion.tenant_id == tenant_id,
        ContractVersion.policy_number == policy_number,
    )

    if version_id:
        query = query.where(ContractVersion.version_id == version_id)

    query = query.order_by(desc(ContractVersion.created_at)).limit(1)

    result = await db.execute(query)
    contract_version = result.scalar_one_or_none()

    if not contract_version:
        raise HTTPException(
            status_code=404,
            detail=f"Contract version not found (policy_number={policy_number}, version_id={version_id or 'latest'})",
        )

    # Ставим задачу переиндексирования в очередь
    celery_app.send_task(
        "reindex_contract_structures",
        kwargs={
            "tenant_id": str(tenant_id),
            "policy_number": policy_number,
            "version_id": contract_version.version_id,
            "pdf_storage_path": contract_version.pdf_path,
        },
        queue="contracts",
    )

    return {
        "status": "queued",
        "policy_number": policy_number,
        "version_id": contract_version.version_id,
        "message": "Contract structures reindexing has been queued. Check status in a few minutes.",
    }
