"""
Интеграционные тесты: Decision Engine + Routing (совместная работа).

Фокус: проверить что решение Decision Engine корректно трансформируется
в маршрут через Routing Service, а статус заявки соответствует ожиданиям.

Дополнительно: проверяем логику ICD10-обогащения и маппинга справочников.

Сценарии:
  - AUTO_APPROVED: doc_type_confirmed выставлен для всех документов
  - MANUAL_REVIEW: запись в очереди создана с правильным priority
  - REJECTED: total_approved и final_payout = 0
  - FRAUD_FLAG: приоритет urgent, payout сохранён (для истории)
  - Аудит-лог записывается на каждый шаг routing
  - ICD10 enricher: обогащение диагноза → category_chain_ru для промпта
  - MockCoreAdapter: возвращает реалистичные тестовые данные
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.models.claim import ClaimDocument, ClaimStatus
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema, LineItemDecisionSchema
from layers.decision.icd10_enricher import AncestorNode, EnrichedDiagnosis
from tests.integration.conftest import (
    CLAIM_ID,
    DOC_ID_1,
    POLICY_NUMBER,
    TENANT_ID,
    make_claim,
    make_claude_decision_response,
    make_contract_chunks,
    make_document,
    make_enriched_diagnosis,
    make_extraction_result,
    make_icd10_list,
    make_mock_db,
    make_providers,
    make_risks_and_limits,
)


# ─────────────────────────────────────────────────────────────────
# AUTO_APPROVED: doc_type_confirmed выставляется для всех документов
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_approved_confirms_document_types():
    """
    При AUTO_APPROVED routing выставляет doc_type_confirmed=True
    для всех документов заявки (для накопления обучающих данных классификатора).
    """
    claim = make_claim()
    doc1 = make_document(doc_id=DOC_ID_1)
    doc1.doc_type_confirmed = False  # исходное состояние

    db = make_mock_db(documents=[doc1])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="J06.9",
                is_covered=True,
                approved_amount=120.0,
                confidence=0.91,
            )
        ],
        line_items=[],
        total_approved=120.0,
        deductible_applied=0.0,
        final_payout=120.0,
        status="approved",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.92,
    )

    from layers.routing.service import route_claim
    result = await route_claim(claim=claim, decision=decision, db=db)

    assert result.route == "auto_approved"
    assert claim.status == ClaimStatus.AUTO_APPROVED
    # Документ должен быть подтверждён
    assert doc1.doc_type_confirmed is True


# ─────────────────────────────────────────────────────────────────
# MANUAL_REVIEW: разные причины и их приоритеты
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manual_review_normal_priority_for_low_confidence():
    """
    Низкая уверенность без высокой суммы → priority=normal.
    """
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="J06.9",
                is_covered=True,
                approved_amount=120.0,
                confidence=0.75,
            )
        ],
        line_items=[],
        total_approved=120.0,
        deductible_applied=0.0,
        final_payout=120.0,
        status="approved",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.75,  # < 0.80 → manual_review
    )

    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "manual_review"
    assert result.priority == "normal"


@pytest.mark.asyncio
async def test_manual_review_high_priority_for_large_amount():
    """
    Высокая сумма (> 500 GEL) + достаточная уверенность → priority=high.
    """
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="Z00.0",
                is_covered=True,
                approved_amount=750.0,
                confidence=0.88,
            )
        ],
        line_items=[],
        total_approved=750.0,
        deductible_applied=0.0,
        final_payout=750.0,  # > 500 → high priority
        status="approved",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.88,
    )

    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "manual_review"
    assert result.priority == "high"
    assert "high_amount" in claim.routing_reason


@pytest.mark.asyncio
async def test_manual_review_urgent_when_fraud_and_manual():
    """
    Fraud flags + manual_review → priority=urgent (fraud имеет приоритет).
    """
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="J06.9",
                is_covered=True,
                approved_amount=120.0,
                confidence=0.91,
            )
        ],
        line_items=[],
        total_approved=120.0,
        deductible_applied=0.0,
        final_payout=120.0,
        status="approved",
        requires_manual_review=True,
        manual_review_reason="borderline_case",
        fraud_flags=["frequency_anomaly"],
        overall_confidence=0.91,
    )

    result = await route_claim(claim=claim, decision=decision, db=db)
    # FRAUD_FLAG имеет приоритет над MANUAL_REVIEW
    assert result.route == "fraud_flag"
    assert result.priority == "urgent"


# ─────────────────────────────────────────────────────────────────
# Аудит-лог создаётся на каждом шаге
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routing_writes_audit_log():
    """
    route_claim() должен записать audit_log с шагом 'routing'.
    """
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="J06.9",
                is_covered=True,
                approved_amount=120.0,
                confidence=0.91,
            )
        ],
        line_items=[],
        total_approved=120.0,
        deductible_applied=0.0,
        final_payout=120.0,
        status="approved",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.91,
    )

    with patch("layers.routing.service.write_audit_entry", AsyncMock()) as mock_audit:
        await route_claim(claim=claim, decision=decision, db=db)
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args.kwargs
        assert call_kwargs.get("step") == "routing"
        assert "route" in str(call_kwargs.get("output_data", {}))


# ─────────────────────────────────────────────────────────────────
# ICD10 Enricher: обогащение диагноза иерархией
# ─────────────────────────────────────────────────────────────────


def test_enriched_diagnosis_category_chain_ru():
    """
    category_chain_ru строится из name_r + ancestors.name_r через →.
    Используется в промпте Claude для категориального рассуждения.
    """
    enriched = make_enriched_diagnosis()["J06.9"]
    chain = enriched.category_chain_ru

    assert "Острая инфекция верхних дыхательных путей" in chain
    assert "Болезни органов дыхания" in chain
    assert " → " in chain


def test_enriched_diagnosis_search_terms_includes_all_languages():
    """
    search_terms включает код + все языковые названия для RAG-запроса.
    """
    enriched = make_enriched_diagnosis()["J06.9"]
    terms = enriched.search_terms

    assert "J06.9" in terms
    assert "Острая инфекция верхних дыхательных путей" in terms
    assert "Acute upper respiratory infection" in terms


def test_enriched_diagnosis_without_ancestors():
    """
    Если ancestors пустые — chain содержит только само название.
    """
    diag = EnrichedDiagnosis(
        code="J06.9",
        name_r="ОРВИ",
        name_g=None,
        name_e=None,
        ancestors=[],
    )
    chain = diag.category_chain_ru
    assert chain == "ОРВИ"


# ─────────────────────────────────────────────────────────────────
# MockCoreAdapter: проверяем что тестовые данные реалистичны
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_core_adapter_get_contract():
    """MockCoreAdapter.get_contract() возвращает ContractData с текстом."""
    from layers.core_adapter.rest_adapter import MockCoreAdapter
    from core.schemas.core_api import ContractData

    adapter = MockCoreAdapter()
    contract = await adapter.get_contract(POLICY_NUMBER)

    assert isinstance(contract, ContractData)
    assert contract.policy_number == POLICY_NUMBER
    assert len(contract.content) > 0  # не пустой текст


@pytest.mark.asyncio
async def test_mock_core_adapter_get_risks_and_limits():
    """MockCoreAdapter.get_risks_and_limits() возвращает непустые риски."""
    from layers.core_adapter.rest_adapter import MockCoreAdapter
    from core.schemas.core_api import RisksAndLimits

    adapter = MockCoreAdapter()
    limits = await adapter.get_risks_and_limits(POLICY_NUMBER)

    assert isinstance(limits, RisksAndLimits)
    assert limits.remaining > 0
    assert len(limits.risks) > 0
    for risk in limits.risks:
        assert risk.risk_id > 0
        assert risk.coverage_pct > 0


@pytest.mark.asyncio
async def test_mock_core_adapter_submit_claim():
    """
    MockCoreAdapter.submit_claim() всегда возвращает status=0 и innum.
    Это позволяет тестировать pipeline до шага ClaimParsing_UNI без кор-системы.
    """
    from layers.core_adapter.rest_adapter import MockCoreAdapter
    from core.schemas.core_api import SubmitClaimResult

    adapter = MockCoreAdapter()
    result = await adapter.submit_claim(
        policy_number=POLICY_NUMBER,
        diagnosid=101,
        event_start_date="2026-01-15",
        event_end_date="2026-01-15",
        pers_id=201,
        config_kind=3,
        risks_list=[{"RiskID": 1, "FinalAmount": 120.0, "ServDate": "2026-01-15"}],
        file_fields=[],
        comment="Тестовое решение",
    )

    assert isinstance(result, SubmitClaimResult)
    assert result.status == 0
    assert len(result.innum) > 0


@pytest.mark.asyncio
async def test_mock_core_adapter_icd10_list():
    """MockCoreAdapter.get_icd10_list() возвращает непустой список."""
    from layers.core_adapter.rest_adapter import MockCoreAdapter

    adapter = MockCoreAdapter()
    icd10_list = await adapter.get_icd10_list()

    assert len(icd10_list) > 0
    for item in icd10_list:
        assert item.diagnosid > 0
        assert item.code  # не пустой код


@pytest.mark.asyncio
async def test_mock_core_adapter_providers():
    """MockCoreAdapter.get_providers() возвращает список провайдеров."""
    from layers.core_adapter.rest_adapter import MockCoreAdapter

    adapter = MockCoreAdapter()
    providers = await adapter.get_providers()

    assert len(providers) > 0
    for p in providers:
        assert p.pers_id > 0
        assert p.name


# ─────────────────────────────────────────────────────────────────
# Сквозной тест: Decision (с MockCoreAdapter данными) → Routing
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_decision_routing_with_mock_adapter_data():
    """
    Сквозной тест: берём данные из MockCoreAdapter,
    прогоняем через make_decision() (мокируя только Claude),
    проверяем routing.

    Это имитирует реальный pipeline без реального кор-сервера.
    """
    from layers.core_adapter.rest_adapter import MockCoreAdapter
    from layers.decision.service import make_decision
    from layers.routing.service import route_claim

    adapter = MockCoreAdapter()
    risks_limits = await adapter.get_risks_and_limits(POLICY_NUMBER)
    icd10_list = await adapter.get_icd10_list()
    providers = await adapter.get_providers()

    claim = make_claim()
    db = make_mock_db([])
    enriched = make_enriched_diagnosis()
    extraction = make_extraction_result()
    chunks = make_contract_chunks()

    claude_resp = make_claude_decision_response(
        is_covered=True,
        approved_amount=120.0,
        overall_confidence=0.91,
    )

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=claude_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)):

        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            extraction=extraction,
            risks_limits=risks_limits,
            icd10_list=icd10_list,
            providers=providers,
            contract_chunks=chunks,
            submission_date=date(2026, 1, 20),
            db=db,
            ocr_texts=["J06.9 ОРВИ Консультация 150.00 GEL"],
        )

    # Decision сформирован корректно
    assert decision.final_payout > 0
    assert decision.diagnosid is not None  # маппинг на DiagnosID выполнен

    # Routing
    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route in {"auto_approved", "manual_review"}
    assert claim.status in {ClaimStatus.AUTO_APPROVED, ClaimStatus.MANUAL_REVIEW}


# ─────────────────────────────────────────────────────────────────
# ICD10-маппинг: case-insensitive
# ─────────────────────────────────────────────────────────────────


def test_diagnosid_mapping_case_insensitive():
    """Маппинг DiagnosID нечувствителен к регистру."""
    from layers.decision.service import find_diagnosid

    icd10_list = make_icd10_list()

    assert find_diagnosid("j06.9", icd10_list) == "J06.9"   # нижний регистр → EXTCOD из справочника
    assert find_diagnosid("J06.9", icd10_list) == "J06.9"   # верхний регистр
    assert find_diagnosid("J06.9 ", icd10_list) == "J06.9"  # с пробелом


def test_diagnosid_z00_maps_correctly():
    """Z00.0 → EXTCOD "Z00.0"."""
    from layers.decision.service import find_diagnosid

    icd10_list = make_icd10_list()
    assert find_diagnosid("Z00.0", icd10_list) == "Z00.0"


# ─────────────────────────────────────────────────────────────────
# Routing: состояние claim полностью обновляется
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routing_sets_all_financial_fields_on_auto_approved():
    """
    При AUTO_APPROVED все финансовые поля claim обновляются.
    """
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="J06.9",
                is_covered=True,
                approved_amount=120.0,
                confidence=0.91,
            )
        ],
        line_items=[
            LineItemDecisionSchema(
                description="Консультация",
                claimed_amount=150.0,
                approved_amount=120.0,
                linked_icd10="J06.9",
            )
        ],
        total_approved=120.0,
        deductible_applied=30.0,
        final_payout=120.0,
        status="approved",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.91,
    )

    await route_claim(claim=claim, decision=decision, db=db)

    assert float(claim.total_approved) == 120.0
    assert float(claim.deductible_applied) == 30.0
    assert float(claim.final_payout) == 120.0
    assert float(claim.overall_confidence) == pytest.approx(0.91, abs=0.01)
    assert claim.decision_type == "auto_approved"
    assert claim.processed_at is not None


@pytest.mark.asyncio
async def test_routing_sets_zero_payout_on_rejected():
    """
    При REJECTED: total_approved=0, final_payout=0 устанавливаются явно.
    """
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])

    decision = ClaimDecision(
        claim_id=CLAIM_ID,
        diagnoses=[
            DiagnosisDecisionSchema(
                icd10_code="J06.9",
                is_covered=False,
                approved_amount=0.0,
                rejection_reason="Не входит в страховые случаи",
                confidence=0.93,
            )
        ],
        line_items=[],
        total_approved=0.0,
        deductible_applied=0.0,
        final_payout=0.0,
        status="rejected",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.93,
    )

    await route_claim(claim=claim, decision=decision, db=db)

    assert float(claim.total_approved) == 0.0
    assert float(claim.final_payout) == 0.0
    assert claim.status == ClaimStatus.REJECTED
    assert claim.decision_type == "rejected"
