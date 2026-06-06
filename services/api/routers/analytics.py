"""
Router: /v1/analytics — статистика и метрики точности.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.models.claim import Claim, ClaimStatus

router = APIRouter()

DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


@router.get("/summary")
async def get_summary(db: AsyncSession = Depends(get_db)):
    """Сводная статистика по заявкам."""
    # Количество заявок по статусам
    status_counts = await db.execute(
        select(Claim.status, func.count(Claim.id))
        .where(Claim.tenant_id == DEFAULT_TENANT_ID)
        .group_by(Claim.status)
    )
    status_data = {str(row[0].value): row[1] for row in status_counts}

    # Средний confidence
    avg_confidence = await db.execute(
        select(func.avg(Claim.overall_confidence))
        .where(
            Claim.tenant_id == DEFAULT_TENANT_ID,
            Claim.overall_confidence.isnot(None),
        )
    )
    avg_conf_value = avg_confidence.scalar()

    # Суммы выплат
    payout_stats = await db.execute(
        select(func.sum(Claim.final_payout), func.avg(Claim.final_payout))
        .where(
            Claim.tenant_id == DEFAULT_TENANT_ID,
            Claim.final_payout.isnot(None),
        )
    )
    payout_row = payout_stats.fetchone()

    total_claims = sum(status_data.values())
    auto_approved = status_data.get("AUTO_APPROVED", 0)
    auto_rate = auto_approved / total_claims if total_claims > 0 else 0.0

    return {
        "total_claims": total_claims,
        "by_status": status_data,
        "auto_approval_rate": round(auto_rate, 3),
        "avg_confidence": round(float(avg_conf_value), 3) if avg_conf_value else None,
        "total_payout": float(payout_row[0]) if payout_row[0] else 0.0,
        "avg_payout": round(float(payout_row[1]), 2) if payout_row[1] else 0.0,
    }


@router.get("/accuracy")
async def get_accuracy_metrics(db: AsyncSession = Depends(get_db)):
    """
    Метрики точности (требуют данных из manual_review_outcomes).
    Доступно после накопления достаточного объёма ручных проверок.
    """
    from core.models.review import ManualReviewOutcome
    from sqlalchemy import cast, Integer

    total_reviews = await db.execute(
        select(func.count(ManualReviewOutcome.id))
        .where(ManualReviewOutcome.tenant_id == DEFAULT_TENANT_ID)
    )
    reviews_count = total_reviews.scalar() or 0

    return {
        "manual_reviews_total": reviews_count,
        "message": "Accuracy metrics available after sufficient manual review data accumulates.",
        "target_metrics": {
            "ocr_accuracy": "≥ 95% (target: ≥ 98%)",
            "extraction_name_accuracy": "≥ 97% (target: ≥ 99%)",
            "extraction_amount_accuracy": "≥ 98% (target: ≥ 99.5%)",
            "auto_decision_accuracy": "≥ 93% (target: ≥ 97%)",
            "fraud_false_positive_rate": "≤ 5% (target: ≤ 2%)",
        },
    }
