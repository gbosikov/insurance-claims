"""
Фикстуры для интеграционных тестов.

Стратегия моков:
- httpx.AsyncClient      → мок (скачивание файлов по URL)
- anthropic.AsyncAnthropic → мок (Claude API)
- Google Vision / Doc AI → мок на уровне ocr_all_documents
- Core Adapter           → MockCoreAdapter (встроенный в проект)
- Storage                → AsyncMock
- DB                     → AsyncMock с настроенными результатами
"""

from __future__ import annotations

import os
import sys

# ── Обязательно ДО любых импортов из core/ ───────────────────────
# Settings требует эти поля; в тестах не нужно реальное подключение.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test_claims")
os.environ.setdefault("REDIS_URL", "redis://:test@localhost:6379/0")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-key-for-unit-tests")
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("CORE_API_PASSWORD", "test-password")
os.environ.setdefault("CORE_API_BASE_URL", "http://mock-core")

# asyncpg мокируется в корневом conftest.py — дублируем на случай
# если интеграционные тесты запускаются изолированно.
_asyncpg_mock = sys.modules.get("asyncpg") or __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
for _mod in ("asyncpg", "asyncpg.connection", "asyncpg.pool"):
    sys.modules.setdefault(_mod, _asyncpg_mock)

import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from core.models.claim import Claim, ClaimDocument, ClaimStatus, DocType
from core.schemas.claim import (
    ClaimCreateRequest,
    DiagnoisItem,
    DocumentRef,
    EventData,
    ExtractionResult,
    InsuredData,
    LineItem,
)
from core.schemas.contract import ContractChunkSchema
from core.schemas.core_api import (
    ContractData,
    ICD10Item,
    ProviderInfo,
    RiskInfo,
    RisksAndLimits,
    SubmitClaimResult,
)
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema, LineItemDecisionSchema
from layers.core_adapter.rest_adapter import MockCoreAdapter
from layers.decision.icd10_enricher import AncestorNode, EnrichedDiagnosis
from layers.ocr.service import OCRResult, TextBlock

# ── Константы для тестов ──────────────────────────────────────────

TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
CLAIM_ID = UUID("11111111-1111-1111-1111-111111111111")
DOC_ID_1 = UUID("22222222-2222-2222-2222-222222222222")
DOC_ID_2 = UUID("33333333-3333-3333-3333-333333333333")
POLICY_NUMBER = "DMC-2024-005521"

# ── Хелперы для мок-объектов ─────────────────────────────────────


def make_claim(
    status: ClaimStatus = ClaimStatus.RECEIVED,
    policy_number: str = POLICY_NUMBER,
) -> MagicMock:
    """
    Создать тестовый объект Claim через MagicMock.
    SQLAlchemy ORM-маппер не инициализирован в unit/integration тестах,
    поэтому нельзя использовать Claim.__new__() — атрибуты не работают.
    """
    claim = MagicMock()
    claim.id = CLAIM_ID
    claim.tenant_id = TENANT_ID
    claim.policy_number = policy_number
    claim.personal_id_number = None
    claim.status = status
    claim.submission_date = datetime(2026, 1, 20, 10, 0, 0)
    claim.event_date = date(2026, 1, 15)
    claim.total_claimed = None
    claim.total_approved = None
    claim.deductible_applied = None
    claim.final_payout = None
    claim.decision_type = None
    claim.overall_confidence = None
    claim.routing_reason = None
    claim.client_reference = "EXT-001"
    claim.processed_at = None
    return claim


def make_document(
    doc_id: UUID = DOC_ID_1,
    source_url: str = "https://medsystem.example.com/form100.pdf",
    doc_type: DocType = DocType.FORM_100,
    storage_path: str | None = None,
) -> MagicMock:
    """
    Создать тестовый объект ClaimDocument через MagicMock.
    SQLAlchemy ORM требует полной инициализации через сессию — в тестах недоступно.
    """
    doc = MagicMock()
    doc.id = doc_id
    doc.claim_id = CLAIM_ID
    doc.tenant_id = TENANT_ID
    doc.doc_type = doc_type
    doc.source_url = source_url
    doc.storage_path = storage_path
    doc.ocr_text = None
    doc.ocr_confidence = None
    doc.extracted_data = None
    doc.quality_score = None
    doc.quality_flags = None
    doc.doc_type_source = "filename_hint"
    doc.doc_type_confirmed = False
    return doc


def make_ocr_result(
    doc_id: UUID = DOC_ID_1,
    doc_type: DocType = DocType.FORM_100,
    text: str = (
        "Пациент: Иванов Иван Иванович\n"
        "Личный номер: 12345678901\n"
        "Дата рождения: 1988-03-15\n"
        "Полис: DMC-2024-005521\n"
        "Дата: 2026-01-15\n"
        "Диагноз: J06.9 ОРВИ\n"
        "Медучреждение: Клиника Медикус\n"
        "Консультация терапевта: 150.00 GEL\n"
        "Итого: 150.00 GEL"
    ),
    confidence: float = 0.96,
) -> OCRResult:
    return OCRResult(
        doc_id=doc_id,
        doc_type=doc_type,
        full_text=text,
        blocks=[TextBlock(text=text, confidence=confidence)],
        avg_confidence=confidence,
        low_confidence_blocks=0,
        strategy_used="document_ai_form_parser",
    )


def make_extraction_result() -> ExtractionResult:
    """Реалистичный результат extraction."""
    return ExtractionResult(
        insured=InsuredData(
            full_name="Иванов Иван Иванович",
            birth_date="1988-03-15",
            personal_id="12345678901",
            policy_number=POLICY_NUMBER,
        ),
        event=EventData(
            date="2026-01-15",
            institution="Клиника Медикус",
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация терапевта", amount=150.00)],
            total_claimed=150.00,
        ),
        extraction_confidence=0.92,
        flags=[],
    )


def make_risks_and_limits(remaining: float = 5000.0) -> RisksAndLimits:
    return RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[
            RiskInfo(
                risk_id=1,
                name="Амбулаторное лечение",
                coverage_pct=80.0,
                total_limit=1000.0,
                remaining_limit=remaining,
                currency="GEL",
                services=[{"serviceid": "S001", "name": "Консультация", "config_kind": 3}],
            )
        ],
        annual_limit=5000.0,
        remaining=remaining,
        currency="GEL",
    )


def make_icd10_list() -> list[ICD10Item]:
    return [
        ICD10Item(diagnosid=101, code="J06.9", name="Острая инфекция верхних дыхательных путей"),
        ICD10Item(diagnosid=102, code="Z00.0", name="Общий медицинский осмотр"),
    ]


def make_providers() -> list[ProviderInfo]:
    return [
        ProviderInfo(pers_id=201, name="Клиника Медикус", inn="123456789"),
    ]


def make_contract_chunks() -> list[ContractChunkSchema]:
    return [
        ContractChunkSchema(
            id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            version_id="v1",
            section_type="coverage_cases",
            title="Амбулаторное лечение",
            content=(
                "Страховым случаем является амбулаторное лечение застрахованного, "
                "включая консультации врачей-специалистов, диагностические исследования "
                "при острых заболеваниях органов дыхания."
            ),
            key_terms=["амбулаторное", "консультация", "органы дыхания"],
            embedding=None,
        ),
        ContractChunkSchema(
            id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            version_id="v1",
            section_type="exclusions",
            title="Исключения",
            content=(
                "Не являются страховыми случаями: хронические заболевания в стадии ремиссии, "
                "косметические процедуры, лечение зубов (кроме острой боли)."
            ),
            key_terms=["исключения", "хронические", "косметические"],
            embedding=None,
        ),
    ]


def make_enriched_diagnosis() -> dict[str, EnrichedDiagnosis]:
    """
    enrich_all() возвращает dict[code → EnrichedDiagnosis].
    Используется в make_decision() как: enriched.get(icd10_code).
    """
    return {
        "J06.9": EnrichedDiagnosis(
            code="J06.9",
            name_r="Острая инфекция верхних дыхательных путей",
            name_g=None,
            name_e="Acute upper respiratory infection",
            ancestors=[
                AncestorNode(
                    id=10,
                    extcod="J06",
                    name_r="Острая инфекция верхних дыхательных путей, множественная",
                    name_g=None,
                    name_e=None,
                ),
                AncestorNode(
                    id=11,
                    extcod=None,
                    name_r="Болезни органов дыхания",
                    name_g=None,
                    name_e=None,
                ),
            ],
        )
    }


# ── Claude API mocks ──────────────────────────────────────────────


def make_claude_extraction_response(
    full_name: str = "Иванов Иван Иванович",
    birth_date: str = "1988-03-15",
    personal_id: str = "12345678901",
    event_date: str = "2026-01-15",
    icd10_code: str = "J06.9",
    description: str = "ОРВИ",
    total_claimed: float = 150.0,
    confidence: float = 0.92,
) -> MagicMock:
    """Мок ответа Claude API для extraction."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {
        "insured": {
            "full_name": full_name,
            "birth_date": birth_date,
            "personal_id": personal_id,
            "policy_number": POLICY_NUMBER,
        },
        "event": {
            "date": event_date,
            "institution": "Клиника Медикус",
            "diagnoses": [{"icd10_code": icd10_code, "description": description}],
            "line_items": [{"description": "Консультация терапевта", "amount": total_claimed}],
            "total_claimed": total_claimed,
        },
        "extraction_confidence": confidence,
        "flags": [],
    }
    response = MagicMock()
    response.content = [tool_block]
    return response


def make_claude_decision_response(
    icd10_code: str = "J06.9",
    is_covered: bool = True,
    approved_amount: float = 120.0,
    confidence: float = 0.91,
    requires_manual_review: bool = False,
    overall_confidence: float = 0.91,
) -> MagicMock:
    """Мок ответа Claude API для decision."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.input = {
        "diagnoses": [
            {
                "icd10_code": icd10_code,
                "is_covered": is_covered,
                "approved_amount": approved_amount if is_covered else 0.0,
                "rejection_reason": None if is_covered else "Не входит в перечень страховых случаев",
                "contract_reference": "Статья 4.1 — Амбулаторное лечение острых заболеваний",
                "confidence": confidence,
            }
        ],
        "line_items": [
            {
                "description": "Консультация терапевта",
                "claimed_amount": 150.0,
                "approved_amount": approved_amount if is_covered else 0.0,
                "linked_icd10": icd10_code,
            }
        ],
        "total_approved": approved_amount if is_covered else 0.0,
        "deductible_applied": 0.0,
        "final_payout": approved_amount if is_covered else 0.0,
        "requires_manual_review": requires_manual_review,
        "manual_review_reason": None,
        "overall_confidence": overall_confidence,
        "summary": (
            f"Диагноз {icd10_code} покрывается договором (ст. 4.1). "
            f"Сумма к выплате: {approved_amount if is_covered else 0.0} GEL. "
            f"Уверенность: {overall_confidence:.0%}."
        ),
    }
    response = MagicMock()
    response.content = [tool_block]
    return response


# ── DB mock ───────────────────────────────────────────────────────


def make_mock_db(documents: list | None = None) -> AsyncMock:
    """
    Создать AsyncMock для DB с реалистичными возвратами.

    - db.execute() для ClaimDocument queries → возвращает переданные documents
    - db.execute() для fraud checks → возвращает пустые результаты (нет фрода)
    - db.execute() для всего остального → пустой результат
    """
    docs = documents or []

    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()

    # Результат с документами (для route_claim → doc_type_confirmed)
    scalars_with_docs = MagicMock()
    scalars_with_docs.all = MagicMock(return_value=docs)
    scalars_with_docs.first = MagicMock(return_value=None)

    result_with_docs = MagicMock()
    result_with_docs.scalars = MagicMock(return_value=scalars_with_docs)
    result_with_docs.scalar_one_or_none = MagicMock(return_value=None)
    result_with_docs.fetchone = MagicMock(return_value=None)

    # Пустой результат (для fraud checks, icd10 queries, audit_log и т.д.)
    empty_scalars = MagicMock()
    empty_scalars.all = MagicMock(return_value=[])
    empty_scalars.first = MagicMock(return_value=None)

    empty_result = MagicMock()
    empty_result.scalars = MagicMock(return_value=empty_scalars)
    empty_result.scalar = MagicMock(return_value=0)
    empty_result.scalar_one_or_none = MagicMock(return_value=None)
    empty_result.fetchone = MagicMock(return_value=None)
    empty_result.fetchall = MagicMock(return_value=[])

    # execute возвращает результат с доками для claim_documents запросов,
    # иначе пустой результат
    async def execute_side_effect(stmt, params=None):
        stmt_str = str(stmt).lower()
        if "claim_documents" in stmt_str and docs:
            return result_with_docs
        return empty_result

    db.execute = AsyncMock(side_effect=execute_side_effect)

    return db


# ── pytest fixtures ───────────────────────────────────────────────


@pytest.fixture
def tenant_id() -> UUID:
    return TENANT_ID


@pytest.fixture
def claim_id() -> UUID:
    return CLAIM_ID


@pytest.fixture
def sample_claim() -> Claim:
    return make_claim()


@pytest.fixture
def sample_documents() -> list[ClaimDocument]:
    return [
        make_document(
            doc_id=DOC_ID_1,
            source_url="https://medsystem.example.com/form100.pdf",
            storage_path="tenants/00000000/claims/11111111/form100.pdf",
        )
    ]


@pytest.fixture
def sample_ocr_results(sample_documents) -> list[OCRResult]:
    return [make_ocr_result(doc_id=DOC_ID_1)]


@pytest.fixture
def sample_extraction() -> ExtractionResult:
    return make_extraction_result()


@pytest.fixture
def sample_risks_limits() -> RisksAndLimits:
    return make_risks_and_limits()


@pytest.fixture
def sample_icd10_list() -> list[ICD10Item]:
    return make_icd10_list()


@pytest.fixture
def sample_providers() -> list[ProviderInfo]:
    return make_providers()


@pytest.fixture
def sample_contract_chunks() -> list[ContractChunkSchema]:
    return make_contract_chunks()


@pytest.fixture
def sample_enriched() -> list[EnrichedDiagnosis]:
    return make_enriched_diagnosis()


@pytest.fixture
def mock_db(sample_documents) -> AsyncMock:
    return make_mock_db(sample_documents)


@pytest.fixture
def mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="tenants/00000000/claims/11111111/form100.pdf")
    storage.download = AsyncMock(return_value=b"%PDF-1.4 fake pdf content")
    storage.generate_path = MagicMock(return_value="tenants/00000000/claims/11111111/form100.pdf")
    return storage


@pytest.fixture
def mock_core_adapter() -> MockCoreAdapter:
    """MockCoreAdapter уже встроен в проект — использует тестовые данные."""
    return MockCoreAdapter()


@pytest.fixture
def claude_extraction_response() -> MagicMock:
    return make_claude_extraction_response()


@pytest.fixture
def claude_decision_response() -> MagicMock:
    return make_claude_decision_response()
