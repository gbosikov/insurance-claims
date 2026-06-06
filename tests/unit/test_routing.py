"""
Unit тесты: Слой 8 — Routing (все 4 маршрута).
"""

from datetime import datetime
from uuid import UUID

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.models.claim import Claim, ClaimStatus
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema


def make_decision(
    is_covered: bool = True,
    confidence: float = 0.90,
    final_payout: float = 150.0,
    requires_manual_review: bool = False,
    fraud_flags: list = None,
) -> ClaimDecision:
    claim_id = UUID("11111111-1111-1111-1111-111111111111")
    return ClaimDecision(
        claim_id=claim_id,
        diagnoses=[DiagnosisDecisionSchema(
            icd10_code="J06.9",
            is_covered=is_covered,
            approved_amount=final_payout if is_covered else 0.0,
            confidence=confidence,
        )],
        total_approved=final_payout if is_covered else 0.0,
        deductible_applied=0.0,
        final_payout=final_payout if is_covered else 0.0,
        status="approved" if is_covered else "rejected",
        requires_manual_review=requires_manual_review,
        fraud_flags=fraud_flags or [],
        overall_confidence=confidence,
        prompt_version="decision/v1.0.0",
        model_version="claude-sonnet-4-20250514",
    )


def make_claim() -> Claim:
    claim = MagicMock(spec=Claim)
    claim.id = UUID("11111111-1111-1111-1111-111111111111")
    claim.tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    claim.status = ClaimStatus.DECISION_PENDING
    return claim


@pytest.mark.asyncio
async def test_route_auto_approve(mock_db):
    """Высокий confidence + покрыто → AUTO_APPROVED."""
    from layers.routing.service import route_claim

    claim = make_claim()
    decision = make_decision(is_covered=True, confidence=0.92, final_payout=150.0)

    result = await route_claim(claim=claim, decision=decision, db=mock_db)
    assert result.route == "auto_approved"
    assert claim.status == ClaimStatus.AUTO_APPROVED


@pytest.mark.asyncio
async def test_route_manual_review_low_confidence(mock_db):
    """Низкий confidence → MANUAL_REVIEW."""
    from layers.routing.service import route_claim

    claim = make_claim()
    decision = make_decision(is_covered=True, confidence=0.75)  # < 0.80

    result = await route_claim(claim=claim, decision=decision, db=mock_db)
    assert result.route == "manual_review"
    assert claim.status == ClaimStatus.MANUAL_REVIEW


@pytest.mark.asyncio
async def test_route_manual_review_requires_flag(mock_db):
    """requires_manual_review=True → MANUAL_REVIEW."""
    from layers.routing.service import route_claim

    claim = make_claim()
    decision = make_decision(is_covered=True, confidence=0.95, requires_manual_review=True)

    result = await route_claim(claim=claim, decision=decision, db=mock_db)
    assert result.route == "manual_review"


@pytest.mark.asyncio
async def test_route_rejected(mock_db):
    """Все диагнозы не покрыты → REJECTED."""
    from layers.routing.service import route_claim

    claim = make_claim()
    decision = make_decision(is_covered=False, confidence=0.95, final_payout=0.0)

    result = await route_claim(claim=claim, decision=decision, db=mock_db)
    assert result.route == "rejected"
    assert claim.status == ClaimStatus.REJECTED


@pytest.mark.asyncio
async def test_route_fraud_flag(mock_db):
    """Флаги фрода → FRAUD_FLAG (срочная проверка)."""
    from layers.routing.service import route_claim

    claim = make_claim()
    decision = make_decision(fraud_flags=["duplicate_claim"])

    result = await route_claim(claim=claim, decision=decision, db=mock_db)
    assert result.route == "fraud_flag"
    assert result.priority == "urgent"
    assert claim.status == ClaimStatus.FRAUD_FLAG


@pytest.mark.asyncio
async def test_route_manual_review_high_amount(mock_db):
    """Сумма > порога → MANUAL_REVIEW (высокий приоритет)."""
    from layers.routing.service import route_claim
    from core.config import get_settings
    settings = get_settings()

    claim = make_claim()
    big_payout = settings.manual_review_amount_threshold + 100
    decision = make_decision(is_covered=True, confidence=0.92, final_payout=big_payout)

    result = await route_claim(claim=claim, decision=decision, db=mock_db)
    assert result.route == "manual_review"
    assert result.priority == "high"
