"""
Router: /v1/appeals — апелляции по отклонённым заявкам.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_tenant_id
from core.config import get_settings
from core.database import get_db
from core.models.appeal import Appeal
from core.models.claim import Claim, ClaimStatus

router = APIRouter()
settings = get_settings()


class AppealCreate(BaseModel):
    claim_id: UUID
    client_reason: str


@router.post("", status_code=201)
async def create_appeal(
    body: AppealCreate,
    db: AsyncSession = Depends(get_db),
    tenant_id: UUID = Depends(get_tenant_id),
):
    """
    Подать апелляцию на решение по заявке.
    Доступно в течение {appeal_window_days} дней с даты решения.
    """
    result = await db.execute(
        select(Claim).where(
            Claim.id == body.claim_id,
            Claim.tenant_id == tenant_id,
        )
    )
    claim = result.scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    if claim.status not in (ClaimStatus.REJECTED, ClaimStatus.AUTO_APPROVED):
        raise HTTPException(
            status_code=400,
            detail=f"Appeals are only allowed for REJECTED or AUTO_APPROVED claims. Current: {claim.status.value}"
        )

    # Проверяем срок апелляции
    if claim.processed_at:
        deadline = claim.processed_at + timedelta(days=settings.appeal_window_days)
        if datetime.utcnow() > deadline:
            raise HTTPException(
                status_code=400,
                detail=f"Appeal window ({settings.appeal_window_days} days) has expired"
            )

    appeal = Appeal(
        claim_id=body.claim_id,
        tenant_id=tenant_id,
        status="RECEIVED",
        client_reason=body.client_reason,
        deadline_at=datetime.utcnow() + timedelta(days=settings.appeal_review_sla_days),
    )
    db.add(appeal)
    await db.commit()
    await db.refresh(appeal)

    return {
        "appeal_id": str(appeal.id),
        "status": "RECEIVED",
        "deadline_at": appeal.deadline_at.isoformat() if appeal.deadline_at else None,
        "message": f"Appeal received. Review SLA: {settings.appeal_review_sla_days} business days.",
    }
