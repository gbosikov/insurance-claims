"""
Unit тесты: Слой 7 — Decision Engine (детерминированные проверки уровня 1).

Тестируем только детерминированные ветки — без реального вызова Claude API.
"""

from datetime import date
from uuid import UUID

import pytest

from core.exceptions import PolicyLimitExhaustedError
from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
from core.schemas.core_api import RiskInfo, RisksAndLimits
from layers.decision.service import (
    check_claim_filed_in_time,
    check_remaining_limit,
    find_diagnosid,
    find_pers_id,
)


POLICY_NUMBER = "DMC-2024-005521"
CLAIM_ID = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def make_risks(remaining: float = 1000.0) -> RisksAndLimits:
    return RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[RiskInfo(
            risk_id=1,
            name="Амбулаторное лечение",
            coverage_pct=80.0,
            total_limit=2000.0,
            remaining_limit=remaining,
            currency="GEL",
        )],
        annual_limit=5000.0,
        remaining=remaining,
        currency="GEL",
    )


def make_extraction(event_date: str = "2026-01-15") -> ExtractionResult:
    return ExtractionResult(
        insured=InsuredData(
            full_name="Иванов Иван Иванович",
            birth_date="1988-03-15",
            personal_id="12345678901",
        ),
        event=EventData(
            date=event_date,
            institution="Клиника Медикус",
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация", amount=150.0)],
            total_claimed=150.0,
        ),
        extraction_confidence=0.92,
        flags=[],
    )


# ── check_remaining_limit ─────────────────────────────────────────


def test_check_remaining_limit_passes_when_positive():
    """Остаток > 0 — проверка проходит без исключения."""
    check_remaining_limit(make_risks(remaining=100.0))  # не должно бросать


def test_check_remaining_limit_raises_when_zero():
    """Остаток = 0 → PolicyLimitExhaustedError."""
    with pytest.raises(PolicyLimitExhaustedError) as exc_info:
        check_remaining_limit(make_risks(remaining=0.0))
    assert exc_info.value.remaining == 0.0
    assert exc_info.value.currency == "GEL"


def test_check_remaining_limit_raises_when_negative():
    """Отрицательный остаток → PolicyLimitExhaustedError."""
    with pytest.raises(PolicyLimitExhaustedError):
        check_remaining_limit(make_risks(remaining=-50.0))


# ── check_claim_filed_in_time ─────────────────────────────────────


def test_filed_in_time_same_day():
    event = date(2026, 1, 15)
    submission = date(2026, 1, 15)
    assert check_claim_filed_in_time(submission, event) is True


def test_filed_in_time_within_90_days():
    event = date(2026, 1, 1)
    submission = date(2026, 3, 31)  # 89 дней
    assert check_claim_filed_in_time(submission, event) is True


def test_filed_in_time_exactly_90_days():
    event = date(2026, 1, 1)
    submission = date(2026, 4, 1)  # 90 дней
    assert check_claim_filed_in_time(submission, event) is True


def test_filed_too_late():
    event = date(2026, 1, 1)
    submission = date(2026, 4, 2)  # 91 день — уже поздно
    assert check_claim_filed_in_time(submission, event) is False


def test_filed_before_event():
    """Дата подачи раньше события — некорректно."""
    event = date(2026, 1, 15)
    submission = date(2026, 1, 10)
    assert check_claim_filed_in_time(submission, event) is False


# ── find_diagnosid ────────────────────────────────────────────────


def test_find_diagnosid_exact_match():
    from core.schemas.core_api import ICD10Item
    icd10_list = [
        ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ"),
        ICD10Item(diagnosid=102, code="Z00.0", name="Осмотр"),
    ]
    assert find_diagnosid("J06.9", icd10_list) == 101


def test_find_diagnosid_case_insensitive():
    from core.schemas.core_api import ICD10Item
    icd10_list = [ICD10Item(diagnosid=101, code="j06.9", name="ОРВИ")]
    assert find_diagnosid("J06.9", icd10_list) == 101


def test_find_diagnosid_prefix_fallback():
    """J06 совпадает с J06.9 по префиксу."""
    from core.schemas.core_api import ICD10Item
    icd10_list = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]
    assert find_diagnosid("J06", icd10_list) == 101


def test_find_diagnosid_not_found():
    from core.schemas.core_api import ICD10Item
    icd10_list = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]
    assert find_diagnosid("Z99.9", icd10_list) is None


def test_find_diagnosid_empty_list():
    assert find_diagnosid("J06.9", []) is None


# ── find_pers_id ──────────────────────────────────────────────────


def test_find_pers_id_exact_match():
    from core.schemas.core_api import ProviderInfo
    providers = [
        ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789"),
        ProviderInfo(pers_id=2, name="МЦ Мединтер", inn="987654321"),
    ]
    assert find_pers_id("Клиника Аврора", providers) == 1


def test_find_pers_id_case_insensitive():
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id("клиника аврора", providers) == 1


def test_find_pers_id_partial_match():
    """Частичное совпадение: «МЦ Мединтер» найдёт «Медицинский Центр Мединтер»."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=2, name="МЦ Мединтер", inn="987654321")]
    assert find_pers_id("Медицинский центр МЦ Мединтер", providers) == 2


def test_find_pers_id_not_found():
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id("Неизвестная больница", providers) == 0


def test_find_pers_id_none_institution():
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id(None, providers) == 0


def test_find_pers_id_empty_providers():
    assert find_pers_id("Клиника Аврора", []) == 0


@pytest.mark.asyncio
async def test_fraud_task_is_asyncio_task():
    """fraud_task должен быть asyncio.Task, а не корутиной — для параллельного исполнения с Claude."""
    import asyncio
    import inspect
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_check_fraud = AsyncMock(return_value=[])

    with patch("layers.decision.service.check_fraud", mock_check_fraud), \
         patch("asyncio.create_task", wraps=asyncio.create_task) as spy_create_task:

        # Создаём минимальный контекст для вызова make_decision
        # Нас интересует только что asyncio.create_task вызван (не просто корутина)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        mock_client = AsyncMock()
        mock_response = MagicMock()
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.input = {
            "diagnoses": [{"icd10_code": "J06.9", "is_covered": True, "approved_amount": 120.0, "confidence": 0.95}],
            "line_items": [{"description": "Консультация", "claimed_amount": 150.0, "approved_amount": 120.0}],
            "total_approved": 120.0,
            "deductible_applied": 0.0,
            "final_payout": 120.0,
            "requires_manual_review": False,
            "manual_review_reason": None,
            "overall_confidence": 0.95,
            "summary": "Одобрено: J06.9, покрытие 80%.",
        }
        mock_response.content = [tool_block]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
        from core.schemas.core_api import ICD10Item, ProviderInfo, RiskInfo, RisksAndLimits

        extraction = ExtractionResult(
            insured=InsuredData(full_name="Иванов И.И.", birth_date="1985-01-01", personal_id="12345678901"),
            event=EventData(date="2026-01-15", institution=None, diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")], line_items=[LineItem(description="Консультация", amount=150.0)], total_claimed=150.0),
            extraction_confidence=0.9,
        )
        risks = RisksAndLimits(policy_number=POLICY_NUMBER, risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0, total_limit=2000.0, remaining_limit=1500.0, currency="GEL")], annual_limit=5000.0, remaining=1500.0, currency="GEL")
        icd10 = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]

        with patch("layers.decision.service.anthropic.AsyncAnthropic", return_value=mock_client), \
             patch("layers.decision.service.write_audit_entry", AsyncMock()):
            from layers.decision.service import make_decision
            await make_decision(
                claim_id=CLAIM_ID, tenant_id=TENANT_ID,
                extraction=extraction, risks_limits=risks,
                icd10_list=icd10, providers=[],
                contract_chunks=[], submission_date=date(2026, 1, 20),
                db=db,
            )

        spy_create_task.assert_called_once()
        assert mock_check_fraud.called
