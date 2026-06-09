"""
Интеграционные тесты: обработка ошибок и граничные случаи.

Тестируем что ошибки в одном слое корректно обрабатываются следующим слоем.
Каждый тест проверяет конкретный путь ошибки и его последствия для статуса заявки.

Сценарии:
  - Ошибки загрузки: неизвестный домен, неверный MIME, файл слишком большой
  - Политика не найдена → REJECTED
  - Исчерпан лимит → ручная проверка (decision engine ловит ошибку сам)
  - Заявка не покрыта → REJECTED
  - requires_manual_review=True от Claude → MANUAL_REVIEW независимо от confidence
  - Стохастическая QA-выборка: 5% AUTO_APPROVED → MANUAL_REVIEW (payout не меняется)
  - Неверная дата события → manual_review без вызова Claude
  - Antifrod: дублирующаяся заявка → fraud_flags
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.exceptions import (
    DocumentQualityError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from core.models.claim import Claim, ClaimDocument, ClaimStatus
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema
from tests.integration.conftest import (
    CLAIM_ID,
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
# Ошибки загрузки документов (Слой 0 — downloader)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_untrusted_host_raises():
    """Домен не в whitelist → DocumentQualityError с reason=untrusted_source."""
    from layers.intake.downloader import _check_trusted_host

    with pytest.raises(DocumentQualityError) as exc:
        _check_trusted_host(
            "https://evil-host.com/file.pdf",
            allowed_hosts=["medsystem.example.com"],
        )
    assert "untrusted" in exc.value.reason.lower()


@pytest.mark.asyncio
async def test_download_unsupported_mime_raises():
    """
    Сервер вернул Content-Type: application/zip → UnsupportedFileTypeError.
    Это должно остановить обработку и запросить документы повторно.
    """
    from layers.intake.downloader import _download_one

    doc = make_document(storage_path=None)
    doc.source_url = "https://medsystem.example.com/archive.zip"

    mock_response = MagicMock()
    mock_response.content = b"PK\x03\x04 fake zip"
    mock_response.headers = {"content-type": "application/zip"}
    mock_response.raise_for_status = MagicMock()

    with patch("layers.intake.downloader.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        with pytest.raises(UnsupportedFileTypeError):
            await _download_one(
                doc,
                allowed_hosts=["medsystem.example.com"],
                storage=AsyncMock(),
                tenant_id=TENANT_ID,
            )


@pytest.mark.asyncio
async def test_download_file_too_large_raises():
    """
    Файл > 20 МБ → FileTooLargeError.
    Pipeline должен перейти в DOCS_REQUESTED.
    """
    from layers.intake.downloader import _download_one

    doc = make_document(storage_path=None)
    doc.source_url = "https://medsystem.example.com/huge_scan.jpg"

    mock_response = MagicMock()
    mock_response.content = b"x" * (21 * 1024 * 1024)  # 21 MB
    mock_response.headers = {"content-type": "image/jpeg"}
    mock_response.raise_for_status = MagicMock()

    with patch("layers.intake.downloader.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        with pytest.raises(FileTooLargeError):
            await _download_one(
                doc,
                allowed_hosts=["medsystem.example.com"],
                storage=AsyncMock(),
                tenant_id=TENANT_ID,
            )


@pytest.mark.asyncio
async def test_download_network_error_raises_httpx_error():
    """
    Сетевая ошибка (URL недоступен) → httpx.HTTPStatusError.
    Worker должен поймать это и перейти в DOCS_REQUESTED.
    """
    import httpx
    from layers.intake.downloader import _download_one

    doc = make_document(storage_path=None)
    doc.source_url = "https://medsystem.example.com/expired_link.pdf"

    with patch("layers.intake.downloader.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404 Not Found",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )
        )
        mock_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await _download_one(
                doc,
                allowed_hosts=["medsystem.example.com"],
                storage=AsyncMock(),
                tenant_id=TENANT_ID,
            )


# ─────────────────────────────────────────────────────────────────
# Decision: граничные случаи
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decision_invalid_event_date_returns_manual_review():
    """
    Если event_date не парсится как ISO-дата → manual_review без вызова Claude.

    Проверяем:
    - make_decision не вызывает Claude API (AsyncAnthropic не инстанцируется)
    - Возвращает ClaimDecision с requires_manual_review=True
    """
    from core.schemas.claim import EventData, ExtractionResult, InsuredData
    from layers.decision.service import make_decision

    bad_extraction = ExtractionResult(
        insured=InsuredData(
            full_name="Тест Тест",
            birth_date="1990-01-01",
            personal_id="12345678901",
        ),
        event=EventData(
            date="INVALID-DATE",  # ← некорректная дата
            diagnoses=[],
            line_items=[],
            total_claimed=100.0,
        ),
        extraction_confidence=0.9,
        flags=[],
    )

    db = make_mock_db([])

    with patch("anthropic.AsyncAnthropic") as mock_anthropic:
        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=bad_extraction,
            risks_limits=make_risks_and_limits(),
            icd10_list=make_icd10_list(),
            providers=make_providers(),
            contract_chunks=make_contract_chunks(),
            submission_date=date(2026, 1, 20),
            db=db,
        )
        # Claude не должен вызываться при плохой дате
        mock_anthropic.assert_not_called()

    assert decision.requires_manual_review is True


@pytest.mark.asyncio
async def test_decision_exhausted_limit_returns_manual_review():
    """
    remaining=0 при проверке лимита → PolicyLimitExhaustedError ловится внутри
    make_decision(), которая возвращает manual_review ClaimDecision (не бросает).

    Это детерминированная проверка (уровень 1), Claude не вызывается.
    """
    from layers.decision.service import make_decision

    extraction = make_extraction_result()
    exhausted_limits = make_risks_and_limits(remaining=0.0)

    db = make_mock_db([])

    with patch("anthropic.AsyncAnthropic") as mock_anthropic:
        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=extraction,
            risks_limits=exhausted_limits,
            icd10_list=make_icd10_list(),
            providers=make_providers(),
            contract_chunks=make_contract_chunks(),
            submission_date=date(2026, 1, 20),
            db=db,
        )
        # Claude не должен вызываться — детерминированная проверка завершила обработку
        mock_anthropic.assert_not_called()

    assert decision.requires_manual_review is True
    assert decision.manual_review_reason == "limit_exhausted"


@pytest.mark.asyncio
async def test_decision_requires_manual_review_flag_propagates():
    """
    Claude возвращает requires_manual_review=True → routing в MANUAL_REVIEW
    даже при высоком overall_confidence (0.93).
    """
    from layers.decision.service import make_decision
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])
    enriched = make_enriched_diagnosis()

    claude_resp = make_claude_decision_response(
        is_covered=True,
        approved_amount=120.0,
        overall_confidence=0.93,
        requires_manual_review=True,  # ← явно требует ручной проверки
    )
    # Дополняем с manual_review_reason
    claude_resp.content[0].input["manual_review_reason"] = "граничный случай по договору"

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=claude_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)):

        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=make_extraction_result(),
            risks_limits=make_risks_and_limits(),
            icd10_list=make_icd10_list(),
            providers=make_providers(),
            contract_chunks=make_contract_chunks(),
            submission_date=date(2026, 1, 20),
            db=db,
        )

    assert decision.requires_manual_review is True

    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "manual_review"
    assert claim.status == ClaimStatus.MANUAL_REVIEW


# ─────────────────────────────────────────────────────────────────
# Antifrod: дублирующаяся заявка
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fraud_check_duplicate_claim_detected():
    """
    check_fraud() находит дубль: тот же personal_id + event_date.
    Возвращает ["duplicate_claim"] в fraud_flags.
    """
    from layers.decision.service import check_fraud

    # Мокируем DB: запрос на дубль возвращает уже существующую заявку
    existing_claim = MagicMock()

    scalars_with_dup = MagicMock()
    scalars_with_dup.first = MagicMock(return_value=existing_claim)

    result_with_dup = MagicMock()
    result_with_dup.scalars = MagicMock(return_value=scalars_with_dup)
    result_with_dup.scalar = MagicMock(return_value=0)

    result_empty = MagicMock()
    result_empty.scalars = MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    result_empty.scalar = MagicMock(return_value=0)

    call_count = 0

    async def execute_side_effect(stmt, params=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # первый запрос — поиск дубля
            return result_with_dup
        return result_empty  # второй запрос — счётчик частоты

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)

    flags = await check_fraud(
        db=db,
        tenant_id=TENANT_ID,
        personal_id="12345678901",
        event_date=date(2026, 1, 15),
        institution="Клиника Медикус",
        total_amount=150.0,
    )

    assert "duplicate_claim" in flags


@pytest.mark.asyncio
async def test_fraud_check_frequency_anomaly_detected():
    """
    check_fraud() обнаруживает превышение частоты заявок (> MAX_CLAIMS за период).
    Возвращает ["frequency_anomaly"] в fraud_flags.
    """
    from core.config import get_settings
    from layers.decision.service import check_fraud

    settings = get_settings()

    # Нет дубля
    no_dup_result = MagicMock()
    no_dup_result.scalars = MagicMock(
        return_value=MagicMock(first=MagicMock(return_value=None))
    )

    # Превышение частоты: count > FRAUD_FREQUENCY_MAX_CLAIMS
    freq_result = MagicMock()
    freq_result.scalar = MagicMock(return_value=settings.fraud_frequency_max_claims + 1)

    call_count = 0

    async def execute_side_effect(stmt, params=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # поиск дубля
            return no_dup_result
        return freq_result  # счётчик частоты

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=execute_side_effect)

    flags = await check_fraud(
        db=db,
        tenant_id=TENANT_ID,
        personal_id="12345678901",
        event_date=date(2026, 1, 15),
        institution="Клиника Медикус",
        total_amount=150.0,
    )

    assert "frequency_anomaly" in flags


@pytest.mark.asyncio
async def test_fraud_check_clean_claim_no_flags():
    """Чистая заявка — нет дублей и нет превышения частоты → пустой список флагов."""
    from layers.decision.service import check_fraud

    empty_result = MagicMock()
    empty_result.scalars = MagicMock(
        return_value=MagicMock(first=MagicMock(return_value=None))
    )
    empty_result.scalar = MagicMock(return_value=0)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=empty_result)

    flags = await check_fraud(
        db=db,
        tenant_id=TENANT_ID,
        personal_id="12345678901",
        event_date=date(2026, 1, 15),
        institution="Клиника Медикус",
        total_amount=150.0,
    )

    assert flags == []


# ─────────────────────────────────────────────────────────────────
# Стохастическая QA-выборка (Шаг 28)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stochastic_qa_routes_approved_to_manual_review():
    """
    При rate=1.0 (100% выборка) каждый AUTO_APPROVED → MANUAL_REVIEW.

    Проверяем:
    - requires_manual_review=True
    - manual_review_reason="stochastic_qa_sample"
    - final_payout НЕ изменяется (оператор только верифицирует)
    """
    from layers.decision.service import make_decision
    from layers.routing.service import route_claim

    claim = make_claim()
    db = make_mock_db([])
    enriched = make_enriched_diagnosis()

    claude_resp = make_claude_decision_response(
        is_covered=True,
        approved_amount=120.0,
        overall_confidence=0.91,
        requires_manual_review=False,
    )

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=claude_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)), \
         patch("layers.decision.service.settings") as mock_settings, \
         patch("random.random", return_value=0.0):  # всегда меньше любого порога

        mock_settings.confidence_auto_approve = 0.85
        mock_settings.confidence_manual_review = 0.80
        mock_settings.confidence_request_docs = 0.70
        mock_settings.manual_review_amount_threshold = 500.0
        mock_settings.manual_review_currency = "GEL"
        mock_settings.fraud_frequency_window_days = 30
        mock_settings.fraud_frequency_max_claims = 10
        mock_settings.claude_model = "claude-sonnet-4-20250514"
        mock_settings.claude_decision_temperature = 0.1
        mock_settings.claude_decision_max_tokens = 4000
        mock_settings.decision_stochastic_qa_rate = 1.0  # 100% → всегда срабатывает

        decision = await make_decision(
            claim_id=CLAIM_ID,
            tenant_id=TENANT_ID,
            extraction=make_extraction_result(),
            risks_limits=make_risks_and_limits(),
            icd10_list=make_icd10_list(),
            providers=make_providers(),
            contract_chunks=make_contract_chunks(),
            submission_date=date(2026, 1, 20),
            db=db,
        )

    assert decision.requires_manual_review is True
    assert decision.manual_review_reason == "stochastic_qa_sample"
    assert decision.final_payout == 120.0  # payout сохранён

    result = await route_claim(claim=claim, decision=decision, db=db)
    assert result.route == "manual_review"
    assert claim.status == ClaimStatus.MANUAL_REVIEW
    # Выплата сохранена — оператор верифицирует, не переназначает
    assert float(claim.final_payout) == 120.0


@pytest.mark.asyncio
async def test_stochastic_qa_at_rate_zero_never_triggers():
    """При decision_stochastic_qa_rate=0 — QA-выборка никогда не срабатывает."""
    from layers.decision.service import make_decision

    db = make_mock_db([])
    enriched = make_enriched_diagnosis()

    claude_resp = make_claude_decision_response(
        is_covered=True,
        approved_amount=120.0,
        overall_confidence=0.91,
    )

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=claude_resp)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value=enriched)), \
         patch("layers.decision.service.settings") as mock_settings:

        mock_settings.confidence_auto_approve = 0.85
        mock_settings.confidence_manual_review = 0.80
        mock_settings.confidence_request_docs = 0.70
        mock_settings.manual_review_amount_threshold = 500.0
        mock_settings.manual_review_currency = "GEL"
        mock_settings.fraud_frequency_window_days = 30
        mock_settings.fraud_frequency_max_claims = 10
        mock_settings.claude_model = "claude-sonnet-4-20250514"
        mock_settings.claude_decision_temperature = 0.1
        mock_settings.claude_decision_max_tokens = 4000
        mock_settings.decision_stochastic_qa_rate = 0.0  # никогда не срабатывает

        for _ in range(5):  # несколько попыток
            decision = await make_decision(
                claim_id=CLAIM_ID,
                tenant_id=TENANT_ID,
                extraction=make_extraction_result(),
                risks_limits=make_risks_and_limits(),
                icd10_list=make_icd10_list(),
                providers=make_providers(),
                contract_chunks=make_contract_chunks(),
                submission_date=date(2026, 1, 20),
                db=db,
            )
            assert decision.requires_manual_review is False
            assert decision.manual_review_reason != "stochastic_qa_sample"


# ─────────────────────────────────────────────────────────────────
# Routing: высокая сумма → manual_review
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_amount_routes_to_manual_review():
    """
    final_payout > MANUAL_REVIEW_AMOUNT_THRESHOLD (500 GEL) → MANUAL_REVIEW.

    Проверяем:
    - route = manual_review
    - priority = high (не urgent)
    - Даже при высоком confidence (0.92)
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
                approved_amount=600.0,
                confidence=0.92,
            )
        ],
        line_items=[],
        total_approved=600.0,
        deductible_applied=0.0,
        final_payout=600.0,  # > 500 GEL
        status="approved",
        requires_manual_review=False,
        fraud_flags=[],
        overall_confidence=0.92,
    )

    result = await route_claim(claim=claim, decision=decision, db=db)

    assert result.route == "manual_review"
    assert result.priority == "high"
    assert claim.status == ClaimStatus.MANUAL_REVIEW
    assert "high_amount" in claim.routing_reason


# ─────────────────────────────────────────────────────────────────
# Intake: валидация пустых данных
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_receive_claim_empty_policy_number():
    """Пустой policy_number (только пробелы) → 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim
    from core.schemas.claim import ClaimCreateRequest, DocumentRef

    with pytest.raises(HTTPException) as exc:
        await receive_claim(
            tenant_id=TENANT_ID,
            request=ClaimCreateRequest(
                policy_number="   ",  # ← только пробелы
                documents=[DocumentRef(url="https://med.com/file.pdf", filename="f.pdf")],
            ),
            db=AsyncMock(),
            celery_app=MagicMock(),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_receive_claim_empty_documents_list():
    """Пустой список documents → 422."""
    from fastapi import HTTPException
    from layers.intake.service import receive_claim
    from core.schemas.claim import ClaimCreateRequest

    with pytest.raises(HTTPException) as exc:
        await receive_claim(
            tenant_id=TENANT_ID,
            request=ClaimCreateRequest(
                policy_number="DMC-001",
                documents=[],
            ),
            db=AsyncMock(),
            celery_app=MagicMock(),
        )
    assert exc.value.status_code == 422


# ─────────────────────────────────────────────────────────────────
# Download: whitelist в production vs development
# ─────────────────────────────────────────────────────────────────


def test_empty_whitelist_in_production_blocks_all():
    """В production пустой whitelist → DocumentQualityError для любого URL."""
    from layers.intake import downloader

    with patch.object(downloader.settings, "environment", "production"):
        with pytest.raises(DocumentQualityError):
            downloader._check_trusted_host("https://any-host.com/file.pdf", [])


def test_empty_whitelist_in_development_allows_with_warning():
    """В development пустой whitelist → предупреждение, но не ошибка."""
    from layers.intake import downloader

    with patch.object(downloader.settings, "environment", "development"):
        # Не должно кидать исключение
        downloader._check_trusted_host("https://any-host.com/file.pdf", [])


def test_whitelist_subdomain_blocked():
    """
    Субдомен не считается разрешённым если в whitelist только основной домен.
    evil.medsystem.example.com НЕ должен проходить.
    """
    from layers.intake import downloader

    with patch.object(downloader.settings, "environment", "production"):
        with pytest.raises(DocumentQualityError):
            downloader._check_trusted_host(
                "https://evil.medsystem.example.com/file.pdf",
                ["medsystem.example.com"],
            )
