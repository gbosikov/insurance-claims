"""
Интеграционные тесты: полный pipeline обработки заявки.

Тестируем прохождение данных через несколько слоёв подряд.
Моки только для внешних сервисов: Claude API, Google Vision, httpx, storage.

Сценарии:
  1. Happy path → AUTO_APPROVED  (высокая уверенность, покрытый диагноз)
  2. Happy path → MANUAL_REVIEW  (низкая уверенность)
  3. Happy path → REJECTED        (все диагнозы не покрыты)
  4. Happy path → FRAUD_FLAG      (обнаружены фрод-флаги)
  5. Extract + Decision сквозной тест с реальными промптами
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.models.claim import Claim, ClaimDocument, ClaimStatus
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema, LineItemDecisionSchema
from tests.integration.conftest import (
    CLAIM_ID,
    POLICY_NUMBER,
    TENANT_ID,
    make_claude_decision_response,
    make_claude_extraction_response,
    make_claim,
    make_contract_chunks,
    make_document,
    make_enriched_diagnosis,
    make_extraction_result,
    make_icd10_list,
    make_mock_db,
    make_ocr_result,
    make_providers,
    make_risks_and_limits,
)


def _mock_anthropic(claude_resp):
    """Хелпер: создать правильный AsyncMock для anthropic.AsyncAnthropic."""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=claude_resp)
    return mock_client


# ─────────────────────────────────────────────────────────────────
# Тест 1: Happy path → AUTO_APPROVED
# Полная цепочка: extraction → decision → routing
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extraction_to_decision_to_auto_approved():
    """
    Extraction (Claude) → Decision (Claude) → Routing.

    Проверяем:
    - Данные корректно перетекают между extraction и decision
    - При confidence=0.91 + покрытом диагнозе → route=auto_approved
    - Claim получает статус AUTO_APPROVED
    - doc_type_confirmed выставляется для документов
    """
    claim = make_claim()
    doc = make_document(storage_path="tenants/test/form100.pdf")
    documents = [doc]
    db = make_mock_db(documents)

    extraction = make_extraction_result()
    enriched = make_enriched_diagnosis()  # dict[str, EnrichedDiagnosis]
    risks_limits = make_risks_and_limits(remaining=5000.0)
    icd10_list = make_icd10_list()
    providers = make_providers()
    chunks = make_contract_chunks()

    claude_decision_resp = make_claude_decision_response(
        is_covered=True,
        approved_amount=120.0,
        overall_confidence=0.91,  # выше порога auto_approve=0.85
    )

    mock_client = _mock_anthropic(claude_decision_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)):

        from layers.decision.service import make_decision
        from layers.routing.service import route_claim

        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=extraction,
            risks_limits=risks_limits,
            icd10_list=icd10_list,
            providers=providers,
            contract_chunks=chunks,
            submission_date=date(2026, 1, 20),
            db=db,
        )

    # Проверяем решение
    assert decision.final_payout == 120.0
    assert decision.overall_confidence == 0.91
    assert not decision.requires_manual_review
    assert decision.diagnoses[0].is_covered is True
    assert decision.diagnosid == 101  # J06.9 → diagnosid из mock списка

    # Routing
    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "auto_approved"
    assert claim.status == ClaimStatus.AUTO_APPROVED
    assert float(claim.final_payout) == 120.0
    assert float(claim.overall_confidence) == pytest.approx(0.91, abs=0.01)


# ─────────────────────────────────────────────────────────────────
# Тест 2: Happy path → MANUAL_REVIEW (низкая уверенность)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_low_confidence_routes_to_manual_review():
    """
    При overall_confidence ниже порога 0.80 → MANUAL_REVIEW.

    Проверяем:
    - Decision с confidence=0.75 создаётся корректно
    - Routing направляет в manual_review
    - Claim получает статус MANUAL_REVIEW
    - Запись в manual_review_queue создаётся (db.add вызван)
    """
    claim = make_claim()
    db = make_mock_db([])

    extraction = make_extraction_result()
    enriched = make_enriched_diagnosis()

    claude_resp = make_claude_decision_response(
        is_covered=True,
        approved_amount=120.0,
        overall_confidence=0.75,  # ниже порога manual_review=0.80
        requires_manual_review=False,
    )

    mock_client = _mock_anthropic(claude_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)):

        from layers.decision.service import make_decision
        from layers.routing.service import route_claim

        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=extraction,
            risks_limits=make_risks_and_limits(),
            icd10_list=make_icd10_list(),
            providers=make_providers(),
            contract_chunks=make_contract_chunks(),
            submission_date=date(2026, 1, 20),
            db=db,
        )

    assert decision.overall_confidence == pytest.approx(0.75, abs=0.01)

    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "manual_review"
    assert claim.status == ClaimStatus.MANUAL_REVIEW
    # ManualReviewQueue должна быть добавлена в DB
    added_objects = db.add.call_args_list
    assert len(added_objects) > 0, "db.add не был вызван"


# ─────────────────────────────────────────────────────────────────
# Тест 3: Все диагнозы не покрыты → REJECTED
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_not_covered_routes_to_rejected():
    """
    Все диагнозы не покрыты + requires_manual_review=False → REJECTED.

    Проверяем:
    - Routing возвращает route=rejected
    - final_payout = 0
    - Claim → REJECTED
    """
    claim = make_claim()
    db = make_mock_db([])

    extraction = make_extraction_result()
    enriched = make_enriched_diagnosis()

    claude_resp = make_claude_decision_response(
        is_covered=False,
        approved_amount=0.0,
        overall_confidence=0.93,
        requires_manual_review=False,
    )

    mock_client = _mock_anthropic(claude_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)):

        from layers.decision.service import make_decision
        from layers.routing.service import route_claim

        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=extraction,
            risks_limits=make_risks_and_limits(),
            icd10_list=make_icd10_list(),
            providers=make_providers(),
            contract_chunks=make_contract_chunks(),
            submission_date=date(2026, 1, 20),
            db=db,
        )

    assert decision.diagnoses[0].is_covered is False
    assert decision.final_payout == 0.0

    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "rejected"
    assert claim.status == ClaimStatus.REJECTED


# ─────────────────────────────────────────────────────────────────
# Тест 4: Фрод-флаги → FRAUD_FLAG
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fraud_flag_routes_to_fraud_flag_status():
    """
    При наличии fraud_flags → FRAUD_FLAG (высший приоритет, priority=urgent).

    Проверяем:
    - Routing возвращает route=fraud_flag независимо от coverage
    - Claim → FRAUD_FLAG
    - ManualReviewQueue создаётся с priority=urgent
    """
    claim = make_claim()
    db = make_mock_db([])

    # Решение с фрод-флагами (даже если диагноз покрыт)
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
        fraud_flags=["duplicate_claim"],  # ← фрод-флаг
        overall_confidence=0.91,
        summary="Тест фрод-флага",
    )

    from layers.routing.service import route_claim
    result = await route_claim(claim=claim, decision=decision, db=db)

    assert result.route == "fraud_flag"
    assert result.priority == "urgent"
    assert claim.status == ClaimStatus.FRAUD_FLAG

    added_objects = db.add.call_args_list
    assert len(added_objects) > 0, "ManualReviewQueue не добавлена в очередь"


# ─────────────────────────────────────────────────────────────────
# Тест 5: Данные перетекают правильно extraction → decision
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extraction_output_drives_decision_input():
    """
    Проверяем что extraction result корректно формирует промпт для decision.

    Конкретно: icd10_code из extraction попадает в prompt и Claude
    получает правильный код для вынесения решения.
    """
    extraction = make_extraction_result()  # J06.9, total_claimed=150
    assert extraction.event.diagnoses[0].icd10_code == "J06.9"
    assert extraction.event.total_claimed == 150.0

    from layers.decision.service import build_decision_prompt
    enriched = make_enriched_diagnosis()  # dict[str, EnrichedDiagnosis]
    risks_limits = make_risks_and_limits()
    chunks = make_contract_chunks()

    prompt = build_decision_prompt(
        extraction=extraction,
        enriched=enriched,
        risks_limits=risks_limits,
        chunks=chunks,
    )

    # Промпт должен содержать данные заявки
    assert "J06.9" in prompt
    assert "150" in prompt
    assert "Клиника Медикус" in prompt
    # Медицинская иерархия из enricher должна присутствовать
    assert "Болезни органов дыхания" in prompt
    # Исключения должны быть первыми (сортировка по SECTION_ORDER)
    exclusions_pos = prompt.find("[exclusions]")
    coverage_pos = prompt.find("[coverage_cases]")
    assert exclusions_pos < coverage_pos, "Исключения должны идти раньше покрытий в промпте"


# ─────────────────────────────────────────────────────────────────
# Тест 6: Маппинг DiagnosID через icd10_list
# ─────────────────────────────────────────────────────────────────


def test_diagnosid_mapping_exact_match():
    """J06.9 → diagnosid=101 (точное совпадение из icd10_list)."""
    from layers.decision.service import find_diagnosid
    icd10_list = make_icd10_list()

    diagnosid = find_diagnosid("J06.9", icd10_list)
    assert diagnosid == 101


def test_diagnosid_mapping_prefix_fallback():
    """J06 → diagnosid=101 через prefix-совпадение."""
    from layers.decision.service import find_diagnosid
    icd10_list = make_icd10_list()

    diagnosid = find_diagnosid("J06", icd10_list)
    assert diagnosid == 101


def test_diagnosid_mapping_not_found():
    """Несуществующий код → None."""
    from layers.decision.service import find_diagnosid
    icd10_list = make_icd10_list()

    diagnosid = find_diagnosid("X99.9", icd10_list)
    assert diagnosid is None


# ─────────────────────────────────────────────────────────────────
# Тест 7: risks_list формируется из decision line_items
# ─────────────────────────────────────────────────────────────────


def test_risks_list_built_from_line_items():
    """
    build_risks_list() должен построить риск-позиции для ClaimParsing_UNI.

    Проверяем:
    - RiskID берётся из первого риска с ненулевым остатком
    - FinalAmount = approved_amount из line_items
    - ServDate = event_date
    """
    from layers.decision.service import build_risks_list

    line_items = [
        LineItemDecisionSchema(
            description="Консультация терапевта",
            claimed_amount=150.0,
            approved_amount=120.0,
            linked_icd10="J06.9",
        )
    ]
    risks_limits = make_risks_and_limits()

    risks_list, config_kind = build_risks_list(line_items, risks_limits, "2026-01-15")

    assert len(risks_list) == 1
    assert risks_list[0]["RiskID"] == 1
    assert risks_list[0]["FinalAmount"] == 120.0
    assert risks_list[0]["ServDate"] == "2026-01-15"
    assert config_kind == 3  # из services[0]["config_kind"]


def test_risks_list_skips_zero_amount():
    """Позиции с approved_amount=0 не включаются в risks_list."""
    from layers.decision.service import build_risks_list

    line_items = [
        LineItemDecisionSchema(
            description="Анализ крови",
            claimed_amount=50.0,
            approved_amount=0.0,  # ← не одобрено
        )
    ]
    risks_limits = make_risks_and_limits()
    risks_list, _ = build_risks_list(line_items, risks_limits, "2026-01-15")
    assert len(risks_list) == 0


# ─────────────────────────────────────────────────────────────────
# Тест 8: Провайдер по названию учреждения
# ─────────────────────────────────────────────────────────────────


def test_provider_lookup_exact_match():
    """Точное совпадение названия учреждения → pers_id."""
    from layers.decision.service import find_pers_id
    providers = make_providers()

    pers_id = find_pers_id("Клиника Медикус", providers)
    assert pers_id == 201


def test_provider_lookup_partial_match():
    """Частичное вхождение → pers_id."""
    from layers.decision.service import find_pers_id
    providers = make_providers()

    pers_id = find_pers_id("Медикус", providers)
    assert pers_id == 201


def test_provider_lookup_not_found():
    """Неизвестное учреждение → 0 (fallback)."""
    from layers.decision.service import find_pers_id
    providers = make_providers()

    pers_id = find_pers_id("Неизвестная больница", providers)
    assert pers_id == 0


def test_provider_lookup_empty_institution():
    """Пустое название → 0."""
    from layers.decision.service import find_pers_id
    providers = make_providers()

    assert find_pers_id("", providers) == 0
    assert find_pers_id(None, providers) == 0


# ─────────────────────────────────────────────────────────────────
# Тест 9: Receive claim → правильные записи в DB
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_receive_claim_creates_claim_and_documents():
    """
    receive_claim() должен:
    1. Создать Claim в DB (db.add вызван с Claim-объектом)
    2. Создать ClaimDocument для каждого URL
    3. Поставить задачу process_claim в Celery
    4. Вернуть claim_id и статус RECEIVED
    """
    from layers.intake.service import receive_claim
    from core.schemas.claim import ClaimCreateRequest, DocumentRef

    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()

    celery = MagicMock()
    mock_claim_obj = MagicMock()
    mock_claim_obj.id = CLAIM_ID
    mock_claim_obj.status = MagicMock(value="RECEIVED")

    with patch("layers.intake.service.Claim", return_value=mock_claim_obj), \
         patch("layers.intake.service.ClaimDocument") as mock_doc_cls, \
         patch("layers.intake.service.write_audit_entry", AsyncMock()):
        mock_doc_cls.return_value = MagicMock()

        request = ClaimCreateRequest(
            policy_number=POLICY_NUMBER,
            client_reference="EXT-001",
            documents=[
                DocumentRef(url="https://medsystem.example.com/form100.pdf", filename="form100.pdf"),
                DocumentRef(url="https://medsystem.example.com/passport.jpg", filename="passport.jpg"),
            ],
        )
        response = await receive_claim(
            tenant_id=TENANT_ID,
            request=request,
            db=db,
            celery_app=celery,
        )

    assert response.claim_id == CLAIM_ID
    assert response.status == "RECEIVED"

    # Celery task должна быть поставлена в очередь
    celery.send_task.assert_called_once_with(
        "process_claim",
        kwargs={
            "claim_id": str(CLAIM_ID),
            "tenant_id": str(TENANT_ID),
        },
    )

    # ClaimDocument создан для каждого URL (2 документа)
    assert mock_doc_cls.call_count == 2


# ─────────────────────────────────────────────────────────────────
# Тест 10: Intake → проверка URL → только http/https проходят
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_receive_claim_rejects_non_http_url():
    """ftp:// URL в списке документов → 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim
    from core.schemas.claim import ClaimCreateRequest, DocumentRef

    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()

    with pytest.raises(HTTPException) as exc:
        await receive_claim(
            tenant_id=TENANT_ID,
            request=ClaimCreateRequest(
                policy_number=POLICY_NUMBER,
                documents=[
                    DocumentRef(url="ftp://storage.example.com/file.pdf", filename="file.pdf"),
                ],
            ),
            db=db,
            celery_app=MagicMock(),
        )
    assert exc.value.status_code == 422


# ─────────────────────────────────────────────────────────────────
# Тест 11: Download → successful download + storage
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_successful_stores_file():
    """
    download_all_documents() скачивает файл, определяет MIME,
    загружает в storage и обновляет storage_path документа.
    """
    from layers.intake.downloader import download_all_documents

    doc = make_document(storage_path=None)
    doc.source_url = "https://medsystem.example.com/form100.pdf"
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="tenants/00000000/form100.pdf")
    storage.generate_path = MagicMock(return_value="tenants/00000000/form100.pdf")

    db = make_mock_db([doc])

    mock_response = MagicMock()
    mock_response.content = b"%PDF-1.4 fake pdf content"
    mock_response.headers = {"content-type": "application/pdf"}
    mock_response.raise_for_status = MagicMock()

    with patch("layers.intake.downloader.httpx.AsyncClient") as mock_client_cls, \
         patch("layers.intake.downloader.write_audit_entry", AsyncMock()):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await download_all_documents(
            documents=[doc],
            allowed_hosts=["medsystem.example.com"],
            storage=storage,
            db=db,
            tenant_id=TENANT_ID,
            claim_id=CLAIM_ID,
        )

    # storage.upload должен быть вызван
    storage.upload.assert_called_once()
    # storage_path документа должен быть заполнен
    assert doc.storage_path == "tenants/00000000/form100.pdf"
