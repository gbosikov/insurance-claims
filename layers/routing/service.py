"""
Слой 8 — Routing Service.

Маршрутизирует заявку в одно из четырёх состояний (в порядке приоритета):
1. FRAUD_FLAG    — fraud_flags непустой
2. REJECTED      — все диагнозы не покрыты И requires_manual_review=False
3. MANUAL_REVIEW — requires_manual_review=True OR low confidence OR high amount
4. AUTO_APPROVED — всё остальное при confidence ≥ threshold
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.models.claim import Claim, ClaimDocument, ClaimStatus
from core.models.review import ManualReviewQueue
from core.schemas.decision import ClaimDecision

log = structlog.get_logger()
settings = get_settings()


@dataclass
class RoutingResult:
    route: str                # auto_approved | manual_review | fraud_flag | rejected
    reason: str
    priority: str = "normal"  # urgent | high | normal


async def route_claim(
    *,
    claim: Claim,
    decision: ClaimDecision,
    db: AsyncSession,
) -> RoutingResult:
    """
    Определяет маршрут и обновляет статус заявки в БД.
    """
    with AuditTimer() as timer:
        # ── 1. FRAUD_FLAG (высший приоритет) ──────────────────────
        if decision.fraud_flags:
            result = RoutingResult(
                route="fraud_flag",
                reason=f"Fraud flags: {', '.join(decision.fraud_flags)}",
                priority="urgent",
            )
            claim.status = ClaimStatus.FRAUD_FLAG
            claim.decision_type = "fraud_flag"
            claim.routing_reason = result.reason

            # Запись в очередь ручной проверки (срочно)
            queue_entry = ManualReviewQueue(
                claim_id=claim.id,
                tenant_id=claim.tenant_id,
                priority="urgent",
                reason="fraud_flag",
                operator_note=result.reason,
            )
            db.add(queue_entry)
            await db.flush()

            log.warning("claim_fraud_flagged", claim_id=str(claim.id), flags=decision.fraud_flags)

        # ── 2. ОТКАЗ ──────────────────────────────────────────────
        elif (
            decision.diagnoses
            and all(not d.is_covered for d in decision.diagnoses)
            and not decision.requires_manual_review
        ):
            result = RoutingResult(
                route="rejected",
                reason="All diagnoses not covered by contract",
            )
            claim.status = ClaimStatus.REJECTED
            claim.decision_type = "rejected"
            claim.total_approved = 0.0
            claim.final_payout = 0.0
            claim.overall_confidence = decision.overall_confidence
            claim.routing_reason = result.reason
            claim.processed_at = datetime.now(timezone.utc)

        # ── 3. РУЧНАЯ ПРОВЕРКА ────────────────────────────────────
        elif (
            decision.requires_manual_review
            or decision.overall_confidence < settings.confidence_manual_review
            or decision.final_payout > settings.manual_review_amount_threshold
        ):
            priority = "high" if decision.final_payout > settings.manual_review_amount_threshold else "normal"
            if decision.fraud_flags:
                priority = "urgent"

            reasons = []
            if decision.requires_manual_review:
                reasons.append(decision.manual_review_reason or "requires_manual_review=true")
            if decision.overall_confidence < settings.confidence_manual_review:
                reasons.append(f"low_confidence={decision.overall_confidence:.2f}")
            if decision.final_payout > settings.manual_review_amount_threshold:
                reasons.append(f"high_amount={decision.final_payout} {settings.manual_review_currency}")

            reason_str = "; ".join(reasons)
            result = RoutingResult(route="manual_review", reason=reason_str, priority=priority)

            claim.status = ClaimStatus.MANUAL_REVIEW
            claim.decision_type = "manual"
            claim.total_approved = decision.total_approved
            claim.deductible_applied = decision.deductible_applied
            claim.final_payout = decision.final_payout
            claim.overall_confidence = decision.overall_confidence
            claim.routing_reason = reason_str

            queue_entry = ManualReviewQueue(
                claim_id=claim.id,
                tenant_id=claim.tenant_id,
                priority=priority,
                reason=reason_str[:100],
            )
            db.add(queue_entry)
            await db.flush()

        # ── 4. АВТО-АПРУВ ─────────────────────────────────────────
        else:
            result = RoutingResult(
                route="auto_approved",
                reason=f"confidence={decision.overall_confidence:.2f}, amount={decision.final_payout}",
            )
            claim.status = ClaimStatus.AUTO_APPROVED
            claim.decision_type = "auto_approved"
            claim.total_approved = decision.total_approved
            claim.deductible_applied = decision.deductible_applied
            claim.final_payout = decision.final_payout
            claim.overall_confidence = decision.overall_confidence
            claim.routing_reason = result.reason
            claim.processed_at = datetime.now(timezone.utc)

            # Подтверждаем типы документов — высокая уверенность системы
            # означает что документы распознаны корректно → годятся для обучения
            docs_result = await db.execute(
                select(ClaimDocument).where(
                    ClaimDocument.claim_id == claim.id,
                    ClaimDocument.tenant_id == claim.tenant_id,
                )
            )
            for doc in docs_result.scalars().all():
                doc.doc_type_confirmed = True
            await db.flush()

            log.info(
                "claim_auto_approved",
                claim_id=str(claim.id),
                final_payout=decision.final_payout,
                confidence=decision.overall_confidence,
            )

        await db.flush()

    await write_audit_entry(
        db,
        claim_id=claim.id,
        tenant_id=claim.tenant_id,
        step="routing",
        input_data={
            "requires_manual_review": decision.requires_manual_review,
            "overall_confidence": decision.overall_confidence,
            "final_payout": decision.final_payout,
            "fraud_flags": decision.fraud_flags,
        },
        output_data={
            "route": result.route,
            "reason": result.reason,
            "priority": result.priority,
            "new_status": claim.status.value,
        },
        duration_ms=timer.duration_ms,
    )

    return result
