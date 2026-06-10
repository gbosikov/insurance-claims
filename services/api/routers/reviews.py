"""
Router: /v1/reviews — рабочий список и результаты ручной проверки.

Замыкает петлю обучения (Шаги 29–30): оператор фиксирует что он исправил
(correction_type) и почему Claude ошибся (claude_error_reason). Эти данные
читает ежедневный job calibrate_confidence (services/worker/tasks_analytics.py).

Пока Portal (Шаг 17) не реализован, операторы работают через Swagger UI (/docs).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import write_audit_entry
from core.auth import get_tenant_id
from core.database import get_db
from core.models.audit import AuditLog
from core.models.claim import Claim, ClaimDocument
from core.models.review import ManualReviewOutcome, ManualReviewQueue

log = structlog.get_logger()
router = APIRouter()


# ── Схемы ─────────────────────────────────────────────────────────

class ReviewOutcomeRequest(BaseModel):
    """Результат ручной проверки, заполняется оператором."""
    expert_decision: dict = Field(
        description="Финальное решение оператора (та же структура что auto_decision)"
    )
    correction_type: Literal["amount", "diagnosis", "coverage", "none"] = Field(
        description="Что исправлено: amount | diagnosis | coverage | none (подтверждено без изменений)"
    )
    claude_error_reason: Literal[
        "ocr_quality", "contract_gap", "extraction_error", "fraud_missed", "correct"
    ] = Field(description="Почему Claude ошибся (correct = был прав)")
    discrepancy_reason: str | None = Field(
        default=None, description="Свободный комментарий оператора"
    )
    operator_id: UUID = Field(description="ID оператора")


class ReviewOutcomeResponse(BaseModel):
    outcome_id: UUID
    claim_id: UUID
    correction_type: str
    queue_items_resolved: int


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("")
async def list_open_reviews(
    priority: str | None = None,
    reason: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    tenant_id: UUID = Depends(get_tenant_id),
):
    """Открытые элементы очереди ручной проверки + сводка заявки."""
    stmt = (
        select(ManualReviewQueue, Claim)
        .join(Claim, Claim.id == ManualReviewQueue.claim_id)
        .where(
            ManualReviewQueue.tenant_id == tenant_id,
            ManualReviewQueue.resolved_at.is_(None),
        )
        .order_by(ManualReviewQueue.created_at)
        .limit(limit)
    )
    if priority:
        stmt = stmt.where(ManualReviewQueue.priority == priority)
    if reason:
        stmt = stmt.where(ManualReviewQueue.reason == reason)

    rows = (await db.execute(stmt)).all()

    return {
        "items": [
            {
                "queue_id": str(queue.id),
                "claim_id": str(queue.claim_id),
                "priority": queue.priority,
                "reason": queue.reason,
                "created_at": queue.created_at,
                "claim_status": claim.status.value if claim.status else None,
                "policy_number": claim.policy_number,
                "total_claimed": float(claim.total_claimed) if claim.total_claimed else None,
                "final_payout": float(claim.final_payout) if claim.final_payout else None,
                "overall_confidence": float(claim.overall_confidence) if claim.overall_confidence else None,
                "routing_reason": claim.routing_reason,
            }
            for queue, claim in rows
        ],
        "count": len(rows),
    }


@router.post("/{claim_id}/outcome", response_model=ReviewOutcomeResponse)
async def submit_review_outcome(
    claim_id: UUID,
    body: ReviewOutcomeRequest,
    db: AsyncSession = Depends(get_db),
    tenant_id: UUID = Depends(get_tenant_id),
):
    """
    Зафиксировать результат ручной проверки.

    1. auto_decision снимается из последней audit-записи step='decision'
       (оператор не перепечатывает решение системы)
    2. INSERT в manual_review_outcomes (+ correction_type, claude_error_reason)
    3. Открытые элементы manual_review_queue по заявке → resolved
    4. При correction_type='none' документы заявки помечаются
       doc_type_confirmed=True, source='operator' (обучающая выборка, Шаги 34-35)
    5. audit_log: step=manual_review
    """
    claim = (await db.execute(
        select(Claim).where(
            Claim.id == claim_id,
            Claim.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    # Снапшот решения системы из audit_log (последняя запись step='decision')
    decision_entry = (await db.execute(
        select(AuditLog)
        .where(
            AuditLog.claim_id == claim_id,
            AuditLog.tenant_id == tenant_id,
            AuditLog.step == "decision",
        )
        .order_by(AuditLog.timestamp.desc())
        .limit(1)
    )).scalar_one_or_none()
    auto_decision = decision_entry.output_data if decision_entry else None

    outcome = ManualReviewOutcome(
        id=uuid4(),
        claim_id=claim_id,
        tenant_id=tenant_id,
        auto_decision=auto_decision,
        expert_decision=body.expert_decision,
        discrepancy_reason=body.discrepancy_reason,
        correction_type=body.correction_type,
        claude_error_reason=body.claude_error_reason,
        operator_id=body.operator_id,
    )
    db.add(outcome)

    # Закрываем открытые элементы очереди по этой заявке
    resolved = await db.execute(
        update(ManualReviewQueue)
        .where(
            ManualReviewQueue.claim_id == claim_id,
            ManualReviewQueue.tenant_id == tenant_id,
            ManualReviewQueue.resolved_at.is_(None),
        )
        .values(resolved_at=datetime.now(timezone.utc))
    )

    # Оператор подтвердил решение без изменений → типы документов верифицированы
    if body.correction_type == "none":
        await db.execute(
            update(ClaimDocument)
            .where(
                ClaimDocument.claim_id == claim_id,
                ClaimDocument.tenant_id == tenant_id,
            )
            .values(doc_type_confirmed=True, doc_type_source="operator")
        )

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="manual_review",
        input_data={"correction_type": body.correction_type,
                    "claude_error_reason": body.claude_error_reason},
        output_data={"expert_decision": body.expert_decision},
        operator_id=body.operator_id,
        override_reason=body.discrepancy_reason,
    )

    await db.commit()

    log.info(
        "manual_review_outcome_recorded",
        claim_id=str(claim_id),
        correction_type=body.correction_type,
        claude_error_reason=body.claude_error_reason,
        operator_id=str(body.operator_id),
    )

    return ReviewOutcomeResponse(
        outcome_id=outcome.id,
        claim_id=claim_id,
        correction_type=body.correction_type,
        queue_items_resolved=resolved.rowcount or 0,
    )
