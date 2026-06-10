"""Unit тесты: /v1/reviews — фиксация результатов ручной проверки (Шаг 30)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from services.api.routers.reviews import (
    ReviewOutcomeRequest,
    submit_review_outcome,
)

CLAIM_ID = UUID("11111111-1111-1111-1111-111111111111")
OPERATOR_ID = UUID("22222222-2222-2222-2222-222222222222")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def make_request(correction_type: str = "none", error_reason: str = "correct") -> ReviewOutcomeRequest:
    return ReviewOutcomeRequest(
        expert_decision={"status": "approved", "final_payout": 120.0},
        correction_type=correction_type,
        claude_error_reason=error_reason,
        discrepancy_reason=None,
        operator_id=OPERATOR_ID,
    )


def make_db(claim_found: bool = True, correction_type: str = "none"):
    """Mock AsyncSession с последовательностью db.execute для submit_review_outcome."""
    claim = MagicMock(id=CLAIM_ID)

    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = claim if claim_found else None

    audit_entry = MagicMock()
    audit_entry.output_data = {"status": "approved", "final_payout": 120.0}
    audit_result = MagicMock()
    audit_result.scalar_one_or_none.return_value = audit_entry

    queue_update_result = MagicMock(rowcount=2)
    docs_update_result = MagicMock(rowcount=3)

    effects = [claim_result, audit_result, queue_update_result]
    if correction_type == "none":
        effects.append(docs_update_result)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=effects)
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_submit_outcome_records_correction_and_resolves_queue():
    """Happy path: outcome записан, очередь закрыта, audit-запись создана."""
    db = make_db()
    body = make_request(correction_type="none", error_reason="correct")

    response = await submit_review_outcome(CLAIM_ID, body, db, tenant_id=TENANT_ID)

    assert response.claim_id == CLAIM_ID
    assert response.correction_type == "none"
    assert response.queue_items_resolved == 2

    # outcome + audit-запись добавлены в сессию
    added_types = [type(call.args[0]).__name__ for call in db.add.call_args_list]
    assert "ManualReviewOutcome" in added_types
    assert "AuditLog" in added_types
    db.commit.assert_awaited_once()

    # auto_decision снят из audit_log (оператор не перепечатывает)
    outcome_obj = next(
        call.args[0] for call in db.add.call_args_list
        if type(call.args[0]).__name__ == "ManualReviewOutcome"
    )
    assert outcome_obj.auto_decision == {"status": "approved", "final_payout": 120.0}
    assert outcome_obj.correction_type == "none"
    assert outcome_obj.claude_error_reason == "correct"
    assert outcome_obj.tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_submit_outcome_confirms_doc_types_on_none():
    """correction_type='none' → выполняется UPDATE документов (4-й db.execute)."""
    db = make_db(correction_type="none")
    await submit_review_outcome(CLAIM_ID, make_request("none", "correct"), db, tenant_id=TENANT_ID)
    assert db.execute.await_count == 4  # claim + audit + queue + documents


@pytest.mark.asyncio
async def test_submit_outcome_skips_doc_confirmation_on_correction():
    """correction_type='amount' → документы НЕ помечаются подтверждёнными."""
    db = make_db(correction_type="amount")
    await submit_review_outcome(CLAIM_ID, make_request("amount", "extraction_error"), db, tenant_id=TENANT_ID)
    assert db.execute.await_count == 3  # claim + audit + queue (без documents)


@pytest.mark.asyncio
async def test_submit_outcome_404_when_claim_not_found():
    db = make_db(claim_found=False)
    with pytest.raises(HTTPException) as exc:
        await submit_review_outcome(uuid4(), make_request(), db, tenant_id=TENANT_ID)
    assert exc.value.status_code == 404


def test_request_schema_rejects_invalid_enums():
    """Невалидные correction_type / claude_error_reason отклоняются Pydantic."""
    with pytest.raises(ValueError):
        ReviewOutcomeRequest(
            expert_decision={},
            correction_type="everything",      # не из Literal
            claude_error_reason="correct",
            operator_id=OPERATOR_ID,
        )
    with pytest.raises(ValueError):
        ReviewOutcomeRequest(
            expert_decision={},
            correction_type="none",
            claude_error_reason="bad_luck",    # не из Literal
            operator_id=OPERATOR_ID,
        )
