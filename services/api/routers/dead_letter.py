"""
Router: /v1/dead-letter — задачи из Dead Letter Queue.

Celery-задачи, исчерпавшие max_retries, попадают в platform.dead_letter_queue.
Операторы могут:
  GET  /v1/dead-letter             — просмотреть неразрешённые элементы
  POST /v1/dead-letter/{id}/requeue — перезапустить задачу заново
  POST /v1/dead-letter/{id}/dismiss — закрыть без перезапуска
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_tenant_id
from core.database import get_db
from core.models.platform import DeadLetterItem

log = structlog.get_logger()
router = APIRouter()


# ── Схемы ─────────────────────────────────────────────────────────

class DLQItemResponse(BaseModel):
    id: UUID
    task_name: str
    task_id: str
    claim_id: UUID | None
    exception_type: str | None
    exception_msg: str | None
    retries: int
    failed_at: datetime
    resolution: str | None

    model_config = {"from_attributes": True}


class DismissRequest(BaseModel):
    operator_id: UUID


class RequeueResponse(BaseModel):
    dlq_id: UUID
    new_task_id: str
    claim_id: UUID | None


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("", response_model=list[DLQItemResponse])
async def list_dead_letter(
    tenant_id: UUID = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    include_resolved: bool = False,
):
    """Список задач из DLQ. По умолчанию только неразрешённые."""
    stmt = (
        select(DeadLetterItem)
        .where(DeadLetterItem.tenant_id == tenant_id)
        .order_by(DeadLetterItem.failed_at.desc())
        .limit(limit)
    )
    if not include_resolved:
        stmt = stmt.where(DeadLetterItem.resolved_at.is_(None))

    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/{dlq_id}/requeue", response_model=RequeueResponse)
async def requeue_dead_letter(
    dlq_id: UUID,
    request: DismissRequest,
    tenant_id: UUID = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Перезапускает задачу из DLQ.

    Для process_claim: запускает новую Celery-задачу с теми же claim_id/tenant_id.
    Идемпотентность ClaimParsing_UNI защищает от дублирования убытка.
    """
    item = await _get_item(dlq_id, tenant_id, db)

    if item.resolved_at is not None:
        raise HTTPException(status_code=409, detail="Элемент уже разрешён")

    new_task_id = ""
    if item.task_name == "process_claim" and item.task_args:
        from services.worker.celery_app import celery_app
        args = list(item.task_args)
        result = celery_app.send_task("process_claim", args=args, queue="claims")
        new_task_id = result.id
        log.info(
            "dlq_requeued",
            dlq_id=str(dlq_id),
            claim_id=str(item.claim_id),
            new_task_id=new_task_id,
            operator_id=str(request.operator_id),
        )
    else:
        log.warning(
            "dlq_requeue_unknown_task",
            dlq_id=str(dlq_id),
            task_name=item.task_name,
        )

    await db.execute(
        update(DeadLetterItem)
        .where(DeadLetterItem.id == dlq_id)
        .values(
            resolved_at=datetime.now(timezone.utc),
            resolved_by=request.operator_id,
            resolution="requeued",
        )
    )
    await db.commit()

    return RequeueResponse(
        dlq_id=dlq_id,
        new_task_id=new_task_id,
        claim_id=item.claim_id,
    )


@router.post("/{dlq_id}/dismiss")
async def dismiss_dead_letter(
    dlq_id: UUID,
    request: DismissRequest,
    tenant_id: UUID = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Закрывает элемент DLQ без перезапуска задачи."""
    item = await _get_item(dlq_id, tenant_id, db)

    if item.resolved_at is not None:
        raise HTTPException(status_code=409, detail="Элемент уже разрешён")

    await db.execute(
        update(DeadLetterItem)
        .where(DeadLetterItem.id == dlq_id)
        .values(
            resolved_at=datetime.now(timezone.utc),
            resolved_by=request.operator_id,
            resolution="dismissed",
        )
    )
    await db.commit()

    log.info(
        "dlq_dismissed",
        dlq_id=str(dlq_id),
        claim_id=str(item.claim_id),
        operator_id=str(request.operator_id),
    )
    return {"status": "dismissed", "dlq_id": str(dlq_id)}


# ── Helpers ────────────────────────────────────────────────────────

async def _get_item(dlq_id: UUID, tenant_id: UUID, db: AsyncSession) -> DeadLetterItem:
    result = await db.execute(
        select(DeadLetterItem).where(
            DeadLetterItem.id == dlq_id,
            DeadLetterItem.tenant_id == tenant_id,
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Элемент DLQ не найден")
    return item
