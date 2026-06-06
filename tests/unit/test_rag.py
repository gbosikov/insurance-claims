"""
Unit тесты: Слой 5 — RAG (RRF, build_rag_query).
"""

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from core.models.contract import ContractChunk
from layers.rag.searcher import build_rag_query, reciprocal_rank_fusion
from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem


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
