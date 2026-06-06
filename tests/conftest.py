"""
Pytest конфигурация и фикстуры.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID


@pytest.fixture(scope="session")
def event_loop():
    """Общий event loop для async тестов."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def tenant_id() -> UUID:
    return UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def claim_id() -> UUID:
    return UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def mock_db():
    """Mock AsyncSession."""
    db = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock()
    return db


@pytest.fixture
def mock_storage():
    """Mock StorageClient."""
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="path/to/file")
    storage.download = AsyncMock(return_value=b"fake_image_data")
    storage.generate_path = MagicMock(return_value="tenants/00000000/claims/11111111/file.pdf")
    return storage


@pytest.fixture
def sample_ocr_result():
    """Пример OCR-результата для тестов."""
    from layers.ocr.service import OCRResult, TextBlock
    from core.models.claim import DocType
    from uuid import UUID

    return OCRResult(
        doc_id=UUID("22222222-2222-2222-2222-222222222222"),
        doc_type=DocType.FORM_100,
        full_text="Пациент: Иванов Иван Иванович\nДата: 2026-01-15\nДиагноз: J06.9\nСумма: 150.00 GEL",
        blocks=[
            TextBlock(text="Иванов Иван Иванович", confidence=0.95),
            TextBlock(text="2026-01-15", confidence=0.98),
        ],
        avg_confidence=0.96,
        low_confidence_blocks=0,
        strategy_used="document_ai_form_parser",
    )


@pytest.fixture
def sample_extraction_result():
    """Пример результата extraction для тестов."""
    from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem

    return ExtractionResult(
        insured=InsuredData(
            full_name="Иванов Иван Иванович",
            birth_date="1988-03-15",
            personal_id="12345678901",
            policy_number="DMC-2024-005521",
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


@pytest.fixture
def sample_policy_limits():
    """Пример лимитов из кор-системы."""
    from core.schemas.core_api import PolicyLimits
    from datetime import date

    return PolicyLimits(
        policy_number="DMC-2024-005521",
        annual_limit=5000.0,
        used_amount=0.0,
        remaining_amount=5000.0,
        currency="GEL",
        deductible=0.0,
        as_of_date=date.today(),
    )
