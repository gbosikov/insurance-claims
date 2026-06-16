"""
Unit тесты: качество решений (Шаги 21, 22, 23, 26).

- coherence_flags → штраф confidence + manual_review (НЕ fraud-роутинг)
- секция исключений + предки МКБ-10 в промпте
- check_waiting_period / check_sublimits (детерминированные, уровень 1)
- extended thinking: kwargs Claude-вызова
- второй проход для неуверенных диагнозов
"""

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core.llm_client import LLMResult
from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
from core.schemas.contract import ContractChunkSchema
from core.schemas.core_api import ICD10Item, RiskInfo, RisksAndLimits
from layers.decision.icd10_enricher import AncestorNode, EnrichedDiagnosis
from layers.decision.service import (
    build_decision_prompt,
    check_sublimits,
    check_waiting_period,
    make_decision,
)

POLICY_NUMBER = "DMC-2024-005521"
CLAIM_ID = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def make_extraction(
    total_claimed: float = 120.0,
    service_urgency: str | None = None,
    extraction_confidence: float = 0.95,
) -> ExtractionResult:
    return ExtractionResult(
        insured=InsuredData(full_name="Иванов И.И.", birth_date="1985-01-01", personal_id="12345678901"),
        event=EventData(
            date="2026-01-15", institution=None,
            service_urgency=service_urgency,
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация", amount=total_claimed)],
            total_claimed=total_claimed,
        ),
        extraction_confidence=extraction_confidence,
    )


def make_risks(
    remaining: float = 1500.0,
    policy_start_date: date | None = None,
    sublimit: float | None = None,
) -> RisksAndLimits:
    return RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[RiskInfo(
            risk_id=1, name="Амбулаторное without referral", coverage_pct=80.0,
            total_limit=2000.0, remaining_limit=remaining, currency="GEL",
            sublimit=sublimit,
        )],
        annual_limit=5000.0, remaining=remaining, currency="GEL",
        policy_start_date=policy_start_date,
    )


def make_tool_input(
    overall_confidence: float = 0.95,
    diagnosis_confidence: float = 0.95,
    coherence_flags: list[str] | None = None,
    requires_manual_review: bool = False,
) -> dict:
    """Возвращает tool_input dict для LLMResult."""
    d = {
        "diagnoses": [{
            "icd10_code": "J06.9", "is_covered": True,
            "approved_amount": 96.0, "confidence": diagnosis_confidence,
        }],
        "line_items": [],
        "total_approved": 96.0,
        "deductible_applied": 0.0,
        "final_payout": 96.0,
        "requires_manual_review": requires_manual_review,
        "manual_review_reason": None,
        "overall_confidence": overall_confidence,
        "summary": "Одобрено",
    }
    if coherence_flags is not None:
        d["coherence_flags"] = coherence_flags
    return d


def make_llm_mock(
    overall_confidence: float = 0.95,
    diagnosis_confidence: float = 0.95,
    coherence_flags: list[str] | None = None,
    requires_manual_review: bool = False,
    reasoning: str | None = None,
) -> AsyncMock:
    """Мок BaseLLMClient возвращающий заданный tool_input."""
    mock = AsyncMock()
    mock.supports_thinking = True
    mock.call_tool = AsyncMock(return_value=LLMResult(
        tool_input=make_tool_input(overall_confidence, diagnosis_confidence, coherence_flags, requires_manual_review),
        reasoning=reasoning,
    ))
    mock.call_text = AsyncMock(return_value=LLMResult(text=""))
    return mock


def decision_patches(mock_llm, audit_mock=None):
    """Стандартный набор патчей для make_decision без БД и внешних API."""
    return [
        patch("layers.decision.service.check_fraud", AsyncMock(return_value=[])),
        patch("layers.decision.service.check_positive_list", AsyncMock(return_value={})),
        patch("layers.decision.service.check_exclusions", AsyncMock(return_value=None)),
        patch("layers.decision.service.get_tenant_config_float", AsyncMock(return_value=1.0)),
        patch("layers.decision.service.get_llm_client", return_value=mock_llm),
        patch("layers.decision.service.write_audit_entry", audit_mock or AsyncMock()),
        patch("layers.decision.service.enrich_all", AsyncMock(return_value={})),
        patch("layers.decision.service.random.random", return_value=0.99),
    ]


async def run_make_decision(mock_llm, extraction=None, risks=None, audit_mock=None):
    patches = decision_patches(mock_llm, audit_mock)
    for p in patches:
        p.start()
    try:
        return await make_decision(
            claim_id=CLAIM_ID, tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            extraction=extraction or make_extraction(),
            risks_limits=risks or make_risks(),
            icd10_list=[ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")],
            providers=[], contract_chunks=[],
            submission_date=date(2026, 1, 20), db=AsyncMock(),
            ocr_texts=["Диагноз: J06.9 ОРВИ Консультация 120 GEL"],
        )
    finally:
        for p in patches:
            p.stop()


# ── Шаг 21: медицинская согласованность ───────────────────────────


@pytest.mark.asyncio
async def test_coherence_flags_route_to_manual_review_not_fraud():
    """Несоответствие услуг диагнозу → manual_review со штрафом, БЕЗ fraud-роутинга."""
    decision = await run_make_decision(make_llm_mock(
        coherence_flags=["МРТ позвоночника не соответствует J06.9 (ОРВИ)"],
    ))

    assert decision.requires_manual_review is True
    assert decision.status == "manual_review"
    assert decision.manual_review_reason.startswith("medical_coherence:")
    assert decision.fraud_flags == []  # сознательное отклонение от спеки: не fraud
    # штраф 0.10: 0.95 → 0.85
    assert decision.overall_confidence == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_no_coherence_flags_no_penalty():
    """Пустые coherence_flags не влияют на решение."""
    decision = await run_make_decision(make_llm_mock(coherence_flags=[]))

    assert decision.requires_manual_review is False
    assert decision.overall_confidence == pytest.approx(0.95)


# ── Шаг 22: исключения через дерево МКБ-10 ────────────────────────


def test_prompt_renders_exclusions_section_with_ancestor_instruction():
    """Exclusion-чанки выделены отдельной секцией с инструкцией проверять предков."""
    exclusion = ContractChunkSchema(
        id=uuid4(), policy_number=POLICY_NUMBER, version_id="v1",
        section_type="exclusions", title="Исключения",
        content="Исключаются онкологические заболевания",
    )
    coverage = ContractChunkSchema(
        id=uuid4(), policy_number=POLICY_NUMBER, version_id="v1",
        section_type="coverage_cases", title="Покрытие",
        content="Покрываются ОРВИ",
    )

    prompt = build_decision_prompt(make_extraction(), {}, make_risks(), [coverage, exclusion])

    assert "## Исключения (проверь КАЖДЫЙ диагноз И КАЖДОГО его предка" in prompt
    # Секция исключений раньше остальных пунктов
    assert prompt.index("Исключаются онкологические") < prompt.index("Покрываются ОРВИ")


def test_prompt_renders_full_ancestor_list():
    """Каждый предок диагноза рендерится отдельной строкой с кодом."""
    enriched = {
        "J06.9": EnrichedDiagnosis(
            code="J06.9",
            name_r="Острая инфекция верхних дыхательных путей",
            name_g=None, name_e=None,
            ancestors=[
                AncestorNode(id=2, extcod="J06", name_r="Острые инфекции верхних дыхательных путей", name_g=None, name_e=None),
                AncestorNode(id=3, extcod=None, name_r="Болезни органов дыхания", name_g=None, name_e=None),
            ],
        )
    }

    prompt = build_decision_prompt(make_extraction(), enriched, make_risks(), [])

    assert "предок: Острые инфекции верхних дыхательных путей [J06]" in prompt
    assert "предок: Болезни органов дыхания" in prompt


# ── Шаг 23: период ожидания ───────────────────────────────────────


def test_waiting_period_emergency_bypasses():
    """Экстренный случай обходит период ожидания."""
    assert check_waiting_period(date(2026, 1, 10), date(2026, 1, 15), "emergency", 30) is True
    assert check_waiting_period(date(2026, 1, 10), date(2026, 1, 15), "urgent", 30) is True


def test_waiting_period_planned_within_window_fails():
    """Плановая услуга на 5-й день полиса при периоде 30 дней — не покрывается."""
    assert check_waiting_period(date(2026, 1, 10), date(2026, 1, 15), "planned", 30) is False


def test_waiting_period_exactly_n_days_passes():
    """Ровно N дней с начала полиса — период пройден."""
    assert check_waiting_period(date(2026, 1, 1), date(2026, 1, 31), "planned", 30) is True


@pytest.mark.asyncio
async def test_waiting_period_violation_returns_manual_review_without_claude():
    """Нарушение периода ожидания → manual_review до вызова LLM."""
    mock_llm = make_llm_mock()
    # Принудительно включить проверку периода ожидания —
    # .env может иметь DECISION_WAITING_PERIOD_ENABLED=false (dev-режим).
    import layers.decision.service as _svc
    with patch.object(_svc.settings, "decision_waiting_period_enabled", True):
        decision = await run_make_decision(
            mock_llm,
            extraction=make_extraction(service_urgency="planned"),
            risks=make_risks(policy_start_date=date(2026, 1, 5)),  # событие 2026-01-15, 10-й день
        )

    assert decision.requires_manual_review is True
    assert decision.manual_review_reason == "waiting_period_violation"
    mock_llm.call_tool.assert_not_called()


# ── Шаг 23: суб-лимиты ────────────────────────────────────────────


def test_check_sublimits_detects_violation():
    items = [LineItem(description="МРТ", amount=800.0)]
    violations = check_sublimits(items, make_risks(sublimit=500.0))
    assert len(violations) == 1
    assert "МРТ" in violations[0]


def test_check_sublimits_passes_within_limit():
    items = [LineItem(description="Консультация", amount=100.0)]
    assert check_sublimits(items, make_risks(sublimit=500.0)) == []


def test_check_sublimits_skipped_when_core_gives_no_data():
    """sublimit=None (кор-система не передала) → проверка пропускается."""
    items = [LineItem(description="МРТ", amount=99999.0)]
    assert check_sublimits(items, make_risks(sublimit=None)) == []


@pytest.mark.asyncio
async def test_sublimit_violation_routes_to_manual_review():
    """Превышение суб-лимита → manual_review с reason=sublimit_exceeded."""
    decision = await run_make_decision(
        make_llm_mock(),
        extraction=make_extraction(total_claimed=250.0),
        risks=make_risks(sublimit=200.0),
    )

    assert decision.requires_manual_review is True
    assert decision.manual_review_reason.startswith("sublimit_exceeded:")
    assert decision.status == "manual_review"


# ── Шаг 26: extended thinking ─────────────────────────────────────


@pytest.mark.asyncio
async def test_complex_case_uses_thinking_kwargs():
    """Сложный случай (сумма > порога) → call_tool вызван с use_thinking=True."""
    mock_llm = make_llm_mock()
    await run_make_decision(mock_llm, extraction=make_extraction(total_claimed=500.0))  # > 300 GEL

    call_kwargs = mock_llm.call_tool.await_args.kwargs
    assert call_kwargs["use_thinking"] is True
    assert "make_claim_decision" in call_kwargs["messages"][0]["content"]


@pytest.mark.asyncio
async def test_simple_case_uses_forced_tool_choice():
    """Простой случай (сумма <= порога) → call_tool без thinking."""
    mock_llm = make_llm_mock()
    await run_make_decision(mock_llm, extraction=make_extraction(total_claimed=120.0))

    call_kwargs = mock_llm.call_tool.await_args.kwargs
    assert call_kwargs.get("use_thinking") is False


@pytest.mark.asyncio
async def test_thinking_blocks_recorded_as_reasoning():
    """Reasoning из LLMResult.reasoning сохраняется в audit_log.output_data['reasoning']."""
    thinking_text = "Диагноз J06.9 входит в категорию острых респираторных."
    mock_llm = make_llm_mock(reasoning=thinking_text)
    audit_mock = AsyncMock()

    await run_make_decision(
        mock_llm,
        extraction=make_extraction(total_claimed=500.0),
        audit_mock=audit_mock,
    )

    output_data = audit_mock.await_args.kwargs["output_data"]
    assert "острых респираторных" in output_data["reasoning"]
    assert output_data["reasoning_mode"] == "thinking"


# ── Шаг 26: второй проход для неуверенных диагнозов ───────────────


@pytest.mark.asyncio
async def test_second_pass_refines_uncertain_diagnosis():
    """Диагноз с confidence < 0.65 → повторный узкий вызов, merge решения."""
    first_tool_input = make_tool_input(diagnosis_confidence=0.50, overall_confidence=0.50)
    second_tool_input = {
        "diagnoses": [{
            "icd10_code": "J06.9", "is_covered": True,
            "approved_amount": 96.0, "confidence": 0.92,
            "contract_reference": "Статья 4.1",
        }],
        "total_approved": 96.0, "deductible_applied": 0.0, "final_payout": 96.0,
        "requires_manual_review": False, "overall_confidence": 0.92, "summary": "",
    }

    mock_llm = AsyncMock()
    mock_llm.supports_thinking = True
    mock_llm.call_tool = AsyncMock(side_effect=[
        LLMResult(tool_input=first_tool_input),
        LLMResult(tool_input=second_tool_input),
    ])
    mock_llm.call_text = AsyncMock(return_value=LLMResult(text=""))
    audit_mock = AsyncMock()

    decision = await run_make_decision(mock_llm, audit_mock=audit_mock)

    assert mock_llm.call_tool.await_count == 2  # основной + второй проход
    assert decision.diagnoses[0].confidence == pytest.approx(0.92)
    assert decision.diagnoses[0].contract_reference == "Статья 4.1"

    audit_steps = [call.kwargs["step"] for call in audit_mock.await_args_list]
    assert "decision_second_pass" in audit_steps


@pytest.mark.asyncio
async def test_no_second_pass_for_confident_diagnosis():
    """Уверенный диагноз → один вызов LLM."""
    mock_llm = make_llm_mock(diagnosis_confidence=0.95)
    await run_make_decision(mock_llm)
    assert mock_llm.call_tool.await_count == 1
