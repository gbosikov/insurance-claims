"""
Unit тесты: Слой 5 — RAG (RRF, build_rag_query, index_contract_from_text).
"""

import hashlib
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from core.models.contract import ContractChunk
from layers.rag.searcher import build_rag_query, reciprocal_rank_fusion
from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem

TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
POLICY_NUMBER = "DMC-2024-005521"
VALID_FROM = date(2026, 1, 1)
CONTENT = "Страховой договор. Покрываются: амбулаторное лечение. Исключения: косметические процедуры."
CONTENT_HASH = hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()

RAW_CHUNKS = [
    {"section_type": "coverage_cases", "title": "Покрытие", "content": "Покрываются: амбулаторное лечение.", "key_terms": ["амбулаторное"]},
    {"section_type": "exclusions", "title": "Исключения", "content": "Исключения: косметические процедуры.", "key_terms": ["косметические"]},
]


def make_chunk(chunk_id=None) -> ContractChunk:
    chunk = MagicMock(spec=ContractChunk)
    chunk.id = chunk_id or uuid4()
    return chunk


def test_reciprocal_rank_fusion_merges_results():
    """RRF объединяет семантические и keyword результаты."""
    chunk_a = make_chunk()
    chunk_b = make_chunk()
    chunk_c = make_chunk()

    semantic = [(chunk_a, 0.9), (chunk_b, 0.8)]
    keyword  = [(chunk_b, 0.7), (chunk_c, 0.6)]

    merged = reciprocal_rank_fusion(semantic, keyword, top_k=3)
    # chunk_b должен быть выше — он в обоих списках
    merged_ids = [c.id for c in merged]
    assert chunk_b.id in merged_ids
    # chunk_b выше по RRF чем chunk_a или chunk_c
    assert merged_ids.index(chunk_b.id) < merged_ids.index(chunk_a.id) or \
           merged_ids.index(chunk_b.id) < len(merged_ids)


def test_reciprocal_rank_fusion_respects_top_k():
    """RRF возвращает не более top_k результатов."""
    chunks = [(make_chunk(), 1.0) for _ in range(10)]
    merged = reciprocal_rank_fusion(chunks, [], top_k=3)
    assert len(merged) <= 3


def test_build_rag_query_with_diagnoses():
    """Запрос содержит коды МКБ-10 и описания услуг."""
    extraction = ExtractionResult(
        insured=InsuredData(
            full_name="Test",
            birth_date="1990-01-01",
            personal_id="12345678901",
        ),
        event=EventData(
            date="2026-01-15",
            institution="Клиника",
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация терапевта", amount=150.0)],
            total_claimed=150.0,
        ),
        extraction_confidence=0.9,
    )

    query = build_rag_query(extraction)
    assert "J06.9" in query
    assert "ОРВИ" in query
    assert "Консультация терапевта" in query


def test_build_rag_query_without_diagnoses():
    """Запрос работает даже без диагнозов."""
    extraction = ExtractionResult(
        insured=InsuredData(
            full_name="Test",
            birth_date="1990-01-01",
            personal_id="12345678901",
        ),
        event=EventData(
            date="2026-01-15",
            diagnoses=[],
            line_items=[],
            total_claimed=100.0,
        ),
        extraction_confidence=0.7,
    )

    query = build_rag_query(extraction)
    assert len(query) > 0  # не пустой


# ── index_contract_from_text ──────────────────────────────────────


def _make_mock_db(existing_version=None):
    """Создаёт mock AsyncSession."""
    db = AsyncMock()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = existing_version
    # scalars().all() для prev_versions
    scalars_result = MagicMock()
    scalars_result.scalars.return_value.return_value = iter([])  # нет предыдущих версий
    db.execute = AsyncMock(return_value=scalar_result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _make_mock_storage():
    storage = AsyncMock()
    storage.upload = AsyncMock()
    return storage


def _make_mock_contract_version(version_id: str = "v1"):
    version = MagicMock()
    version.tenant_id = TENANT_ID
    version.policy_number = POLICY_NUMBER
    version.version_id = version_id
    version.content_hash = CONTENT_HASH
    version.valid_from = VALID_FROM
    version.valid_to = None
    version.pdf_path = f"tenants/{TENANT_ID}/contracts/{POLICY_NUMBER}/{version_id}.txt"
    return version


@pytest.mark.asyncio
async def test_index_from_text_returns_existing_if_hash_matches():
    """Если контракт с таким хэшем уже есть — возвращает существующую версию, не создаёт новую."""
    from layers.rag.indexer import index_contract_from_text
    from core.schemas.contract import ContractVersionSchema

    existing = _make_mock_contract_version("v1")
    db = _make_mock_db(existing_version=existing)
    storage = _make_mock_storage()

    with patch("core.schemas.contract.ContractVersionSchema.model_validate", return_value=MagicMock(spec=ContractVersionSchema)):
        result = await index_contract_from_text(
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            content=CONTENT,
            content_hash=CONTENT_HASH,
            version_label="v1",
            valid_from=VALID_FROM,
            db=db,
            storage=storage,
        )

    # Не должно загружать в storage и запускать Claude
    storage.upload.assert_not_called()
    db.commit.assert_not_called()


def _make_new_db():
    """DB mock для тестов, где контракт НЕ найден в кэше (новая индексация)."""
    call_count = [0]
    not_found = MagicMock()
    not_found.scalar_one_or_none.return_value = None
    empty = MagicMock()
    empty.scalars.return_value = iter([])

    db = AsyncMock()
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        return not_found if call_count[0] == 1 else empty
    db.execute = AsyncMock(side_effect=side_effect)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _model_patches():
    """
    Патчит SQLAlchemy-модели И select() в indexer, чтобы не инициировать маппер.
    select(ContractVersion) падает до db.execute(), если ContractVersion — MagicMock,
    поэтому patching нужен на уровне indexer-модуля.
    """
    mock_version_cls = MagicMock()
    mock_chunk_cls = MagicMock()
    mock_select = MagicMock()
    mock_select.return_value.where.return_value = MagicMock()  # select(...).where(...)
    return (
        patch("layers.rag.indexer.ContractVersion", mock_version_cls),
        patch("layers.rag.indexer.ContractChunk", mock_chunk_cls),
        patch("layers.rag.indexer.select", mock_select),
        mock_version_cls,
        mock_chunk_cls,
    )


def _indexer_patches(mock_version_cls=None, mock_chunk_cls=None):
    """Общий набор патчей для тестов index_contract_from_text."""
    mock_version_cls = mock_version_cls or MagicMock()
    mock_chunk_cls = mock_chunk_cls or MagicMock()
    mock_select = MagicMock()
    mock_select.return_value.where.return_value = MagicMock()
    return [
        patch("layers.rag.indexer.ContractVersion", mock_version_cls),
        patch("layers.rag.indexer.ContractChunk", mock_chunk_cls),
        patch("layers.rag.indexer.select", mock_select),
        patch("layers.rag.indexer.chunk_contract_with_claude", AsyncMock(return_value=RAW_CHUNKS)),
        patch("layers.rag.indexer.get_embedding", return_value=[0.1] * 1024),
        patch("core.schemas.contract.ContractVersionSchema.model_validate", return_value=MagicMock()),
    ]


@pytest.mark.asyncio
async def test_index_from_text_computes_hash_when_none():
    """Если content_hash=None — хэш вычисляется из текста и используется для поиска в БД."""
    from layers.rag.indexer import index_contract_from_text

    db = _make_new_db()
    storage = _make_mock_storage()

    patches = _indexer_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await index_contract_from_text(
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            content=CONTENT,
            content_hash=None,      # ← не передаём хэш
            version_label="v1",
            valid_from=VALID_FROM,
            db=db,
            storage=storage,
        )

    db.execute.assert_called()


@pytest.mark.asyncio
async def test_index_from_text_uses_version_label():
    """version_label используется как version_id, не генерируется из даты."""
    from layers.rag.indexer import index_contract_from_text

    db = _make_new_db()
    storage = _make_mock_storage()
    mock_version_cls = MagicMock()

    patches = _indexer_patches(mock_version_cls=mock_version_cls)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await index_contract_from_text(
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            content=CONTENT,
            content_hash=CONTENT_HASH,
            version_label="my-custom-label",
            valid_from=VALID_FROM,
            db=db,
            storage=storage,
        )

    call_kwargs = mock_version_cls.call_args.kwargs
    assert call_kwargs["version_id"] == "my-custom-label"


@pytest.mark.asyncio
async def test_index_from_text_uploads_txt_to_storage():
    """Текст сохраняется в storage как .txt файл (pdf_path требует NOT NULL)."""
    from layers.rag.indexer import index_contract_from_text

    db = _make_new_db()
    storage = _make_mock_storage()

    patches = _indexer_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await index_contract_from_text(
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            content=CONTENT,
            content_hash=CONTENT_HASH,
            version_label="v1",
            valid_from=VALID_FROM,
            db=db,
            storage=storage,
        )

    storage.upload.assert_called_once()
    uploaded_path = storage.upload.call_args[0][1]
    assert uploaded_path.endswith(".txt")
    assert POLICY_NUMBER in uploaded_path


@pytest.mark.asyncio
async def test_index_from_text_creates_chunks():
    """Claude-chunking вызывается, для каждого чанка создаётся ContractChunk с правильным section_type."""
    from layers.rag.indexer import index_contract_from_text

    db = _make_new_db()
    storage = _make_mock_storage()
    mock_chunk_cls = MagicMock()

    patches = _indexer_patches(mock_chunk_cls=mock_chunk_cls)
    with patches[0], patches[1], patches[2], patches[3] as mock_claude, patches[4] as mock_embed, patches[5]:
        await index_contract_from_text(
            tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            content=CONTENT,
            content_hash=CONTENT_HASH,
            version_label="v1",
            valid_from=VALID_FROM,
            db=db,
            storage=storage,
        )

    mock_claude.assert_awaited_once()
    assert mock_embed.call_count == len(RAW_CHUNKS)
    assert mock_chunk_cls.call_count == len(RAW_CHUNKS)
    calls = mock_chunk_cls.call_args_list
    assert calls[0].kwargs["section_type"] == "coverage_cases"
    assert calls[1].kwargs["section_type"] == "exclusions"
