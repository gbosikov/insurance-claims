"""
Слой 5 — RAG Searcher.

Задача: найти релевантные пункты договора для заявки.
Использует гибридный поиск: semantic (pgvector) + BM25 (PostgreSQL FTS).
Объединение через Reciprocal Rank Fusion.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.exceptions import ContractNotIndexedError, ContractReindexTimeoutError
from core.models.contract import ContractChunk, ContractVersion
from core.schemas.claim import ExtractionResult
from core.schemas.contract import ContractChunkSchema
from layers.rag.embedder import get_embedding

log = structlog.get_logger()
settings = get_settings()


def build_rag_query(extraction: ExtractionResult) -> str:
    """
    Строит поисковый запрос из данных заявки.
    Включает диагнозы (коды МКБ-10 + описания) и услуги.
    """
    diagnoses = " ".join(
        f"{d.icd10_code} {d.description}"
        for d in extraction.event.diagnoses
    )
    items = " ".join(i.description for i in extraction.event.line_items)
    institution = extraction.event.institution or ""

    parts = []
    if diagnoses:
        parts.append(f"страховое покрытие диагнозов: {diagnoses}")
    if items:
        parts.append(f"услуги: {items}")
    if institution:
        parts.append(f"медучреждение: {institution}")

    return ". ".join(parts) if parts else "страховое покрытие медицинских услуг"


def reciprocal_rank_fusion(
    semantic_results: list[tuple[ContractChunk, float]],
    keyword_results: list[tuple[ContractChunk, float]],
    top_k: int,
    k: int = 60,
) -> list[ContractChunk]:
    """
    Reciprocal Rank Fusion: объединяет два ранжирования в одно.
    score(d) = Σ 1/(k + rank_i)

    Args:
        k: константа (обычно 60), сглаживает влияние высоких рангов
    """
    scores: dict[str, float] = {}
    chunks_map: dict[str, ContractChunk] = {}

    # Семантические результаты
    for rank, (chunk, _) in enumerate(semantic_results):
        chunk_id = str(chunk.id)
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        chunks_map[chunk_id] = chunk

    # BM25 результаты
    for rank, (chunk, _) in enumerate(keyword_results):
        chunk_id = str(chunk.id)
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        chunks_map[chunk_id] = chunk

    # Сортируем по убыванию score
    sorted_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
    return [chunks_map[cid] for cid in sorted_ids[:top_k]]


async def _semantic_search(
    db: AsyncSession,
    tenant_id: UUID,
    policy_number: str,
    version_id: str,
    query_embedding: list[float],
    top_k: int,
) -> list[tuple[ContractChunk, float]]:
    """Векторный поиск через pgvector (cosine distance)."""
    # pgvector оператор <=> — cosine distance (меньше = ближе)
    stmt = text("""
        SELECT id, (embedding <=> :query_vec::vector) AS distance
        FROM contract_chunks
        WHERE tenant_id = :tenant_id
          AND policy_number = :policy_number
          AND version_id = :version_id
        ORDER BY embedding <=> :query_vec::vector
        LIMIT :limit
    """)

    result = await db.execute(stmt, {
        "query_vec": str(query_embedding),
        "tenant_id": str(tenant_id),
        "policy_number": policy_number,
        "version_id": version_id,
        "limit": top_k,
    })
    rows = result.fetchall()

    chunks: list[tuple[ContractChunk, float]] = []
    for row in rows:
        chunk = await db.get(ContractChunk, row.id)
        if chunk:
            chunks.append((chunk, 1.0 - row.distance))  # similarity = 1 - distance

    return chunks


async def _keyword_search(
    db: AsyncSession,
    tenant_id: UUID,
    policy_number: str,
    version_id: str,
    query: str,
    top_k: int,
) -> list[tuple[ContractChunk, float]]:
    """
    Полнотекстовый BM25 поиск через PostgreSQL FTS.
    Три языка: russian, english, simple (для грузинского).
    """
    stmt = text("""
        SELECT id,
               ts_rank_cd(
                   to_tsvector('russian', content) ||
                   to_tsvector('english', content) ||
                   to_tsvector('simple',  content),
                   plainto_tsquery('simple', :query)
               ) AS rank
        FROM contract_chunks
        WHERE tenant_id = :tenant_id
          AND policy_number = :policy_number
          AND version_id = :version_id
          AND (
              to_tsvector('russian', content) @@ plainto_tsquery('russian', :query)
              OR to_tsvector('english', content) @@ plainto_tsquery('english', :query)
              OR to_tsvector('simple',  content) @@ plainto_tsquery('simple',  :query)
          )
        ORDER BY rank DESC
        LIMIT :limit
    """)

    result = await db.execute(stmt, {
        "query": query,
        "tenant_id": str(tenant_id),
        "policy_number": policy_number,
        "version_id": version_id,
        "limit": top_k,
    })
    rows = result.fetchall()

    chunks: list[tuple[ContractChunk, float]] = []
    for row in rows:
        chunk = await db.get(ContractChunk, row.id)
        if chunk:
            chunks.append((chunk, float(row.rank)))

    return chunks


async def get_active_version(
    db: AsyncSession,
    tenant_id: UUID,
    policy_number: str,
    event_date: date,
) -> ContractVersion | None:
    """
    Возвращает версию контракта, действовавшую на дату события.
    """
    stmt = select(ContractVersion).where(
        ContractVersion.tenant_id == tenant_id,
        ContractVersion.policy_number == policy_number,
        ContractVersion.valid_from <= event_date,
        (ContractVersion.valid_to.is_(None)) | (ContractVersion.valid_to >= event_date),
    ).order_by(ContractVersion.valid_from.desc()).limit(1)

    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def search_chunks(
    *,
    db: AsyncSession,
    tenant_id: UUID,
    policy_number: str,
    version_id: str,
    query: str,
    top_k: int | None = None,
) -> list[ContractChunkSchema]:
    """
    Гибридный поиск (semantic + BM25) с Reciprocal Rank Fusion.

    1. Эмбеддинг запроса через multilingual-e5-large
    2. Семантический поиск (pgvector)
    3. BM25 поиск (PostgreSQL FTS)
    4. RRF объединение
    5. Возврат top_k чанков
    """
    if top_k is None:
        top_k = settings.rag_top_k

    # Эмбеддинг запроса
    query_embedding = get_embedding(query, is_query=True)

    # Параллельный поиск
    import asyncio
    semantic_task = _semantic_search(
        db, tenant_id, policy_number, version_id,
        query_embedding, top_k * 2
    )
    keyword_task = _keyword_search(
        db, tenant_id, policy_number, version_id,
        query, top_k * 2
    )
    semantic_results, keyword_results = await asyncio.gather(semantic_task, keyword_task)

    # RRF объединение
    merged = reciprocal_rank_fusion(
        semantic_results, keyword_results,
        top_k=top_k,
        k=settings.rag_rrf_k,
    )

    log.info(
        "rag_search_completed",
        policy_number=policy_number,
        version_id=version_id,
        semantic_hits=len(semantic_results),
        keyword_hits=len(keyword_results),
        merged=len(merged),
    )

    return [ContractChunkSchema.model_validate(chunk) for chunk in merged]


async def get_contract_chunks_with_freshness_check(
    *,
    db: AsyncSession,
    tenant_id: UUID,
    policy_number: str,
    event_date: date,
    query: str,
    contract_data=None,  # ContractData из кор-системы (уже загружен в tasks.py)
) -> list[ContractChunkSchema]:
    """
    Поиск с проверкой актуальности контракта.

    1. Получить актуальную версию из нашей БД (по event_date)
    2. Если contract_data передан — сравнить content_hash
    3. Если изменился → переиндексировать (TODO: timeout 45 сек)
    4. Вернуть chunks
    """
    version = await get_active_version(db, tenant_id, policy_number, event_date)

    if version is None:
        # Контракт ещё не проиндексирован — пробуем проиндексировать сейчас
        if contract_data is not None and contract_data.content:
            log.info("contract_not_indexed_indexing_now", policy_number=policy_number)
            try:
                from layers.rag.indexer import index_contract_from_text
                from core.storage import get_storage_client
                version_obj = await index_contract_from_text(
                    tenant_id=tenant_id,
                    policy_number=policy_number,
                    content=contract_data.content,
                    content_hash=contract_data.content_hash,
                    version_label=contract_data.version or "v1",
                    valid_from=event_date,
                    db=db,
                    storage=get_storage_client(),
                )
                version = await get_active_version(db, tenant_id, policy_number, event_date)
            except Exception as e:
                log.warning("contract_auto_index_failed", error=str(e))

        if version is None:
            raise ContractNotIndexedError(policy_number)

    # Проверка актуальности по content_hash
    if contract_data is not None and contract_data.content_hash:
        if version.content_hash and version.content_hash != contract_data.content_hash:
            log.info(
                "contract_hash_changed",
                policy_number=policy_number,
                old_hash=version.content_hash[:16],
                new_hash=contract_data.content_hash[:16],
            )
            # TODO: переиндексировать с timeout=45 сек → manual_review при таймауте
            log.warning("contract_reindex_needed_but_not_implemented", policy_number=policy_number)

    return await search_chunks(
        db=db,
        tenant_id=tenant_id,
        policy_number=policy_number,
        version_id=version.version_id,
        query=query,
    )
