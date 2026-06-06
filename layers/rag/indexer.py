"""
Слой 5 — RAG Indexer.

Задача: онбординг контракта — один раз при загрузке PDF.
1. Скачать PDF из storage
2. Извлечь текст (pymupdf)
3. Семантический chunking через Claude API
4. Векторизация через multilingual-e5-large
5. Сохранение в pgvector (contract_chunks)
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import UUID

import anthropic
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.models.contract import ContractChunk, ContractVersion
from core.schemas.contract import ContractVersionSchema
from core.storage import StorageClient
from layers.rag.embedder import get_embedding

log = structlog.get_logger()
settings = get_settings()

CHUNKING_PROMPT_VERSION = "chunking/v1.0.0"

CHUNKING_SYSTEM_PROMPT = """Раздели страховой договор на смысловые секции.
Договор может быть на русском, грузинском или английском языке.
Сохраняй текст каждой секции без изменений на языке оригинала.

Для каждой секции верни JSON-объект:
- section_type: coverage_cases | exclusions | claim_conditions | limits | definitions | appeal_process | general
- title: краткое название (до 10 слов)
- content: полный текст секции БЕЗ изменений
- key_terms: список ключевых терминов, кодов МКБ-10, названий услуг

ПРАВИЛА:
- Не изменяй и не сокращай текст секций
- Каждый пункт об исключении — отдельный чанк
- Минимальный размер чанка: 2 предложения
- Максимальный размер чанка: 800 символов (если больше — раздели логично)
- Верни ТОЛЬКО JSON-массив секций, без пояснений и markdown"""


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Извлекает текст из PDF через pymupdf."""
    import fitz  # pymupdf

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_text: list[str] = []
    for page in doc:
        text = page.get_text("text")
        if text.strip():
            pages_text.append(text)
    return "\n\n".join(pages_text)


def compute_hash(content: bytes) -> str:
    """SHA-256 хэш для проверки актуальности контракта."""
    return hashlib.sha256(content).hexdigest()


async def chunk_contract_with_claude(text: str) -> list[dict]:
    """
    Семантический chunking контракта через Claude API.
    Возвращает список секций: [{section_type, title, content, key_terms}, ...]
    """
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=settings.claude_chunking_max_tokens,
        temperature=settings.claude_extraction_temperature,  # 0.0 — детерминированность
        system=CHUNKING_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Раздели следующий страховой договор на смысловые секции:\n\n{text}"
        }],
    )

    raw_text = response.content[0].text.strip()

    # Убираем markdown-обёртку если Claude всё же добавил
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1])

    try:
        chunks = json.loads(raw_text)
        if not isinstance(chunks, list):
            raise ValueError("Expected JSON array")
        return chunks
    except (json.JSONDecodeError, ValueError) as e:
        log.error("chunking_parse_error", error=str(e), raw_text=raw_text[:200])
        # Fallback: один большой чанк
        return [{
            "section_type": "general",
            "title": "Полный текст договора",
            "content": text,
            "key_terms": [],
        }]


async def index_contract(
    *,
    tenant_id: UUID,
    policy_number: str,
    pdf_bytes: bytes,
    valid_from: date,
    valid_to: date | None = None,
    storage: StorageClient,
    db: AsyncSession,
) -> ContractVersionSchema:
    """
    Полный цикл индексации контракта.

    1. Вычислить content_hash
    2. Проверить нет ли уже такой версии
    3. Сохранить PDF в storage
    4. Извлечь текст из PDF
    5. Claude: семантический chunking
    6. Для каждого чанка — эмбеддинг через multilingual-e5-large
    7. Сохранить в contract_chunks
    8. Создать запись contract_versions
    """
    content_hash = compute_hash(pdf_bytes)

    # Проверяем: нет ли уже такой версии (по хэшу)
    existing = await db.execute(
        select(ContractVersion).where(
            ContractVersion.tenant_id == tenant_id,
            ContractVersion.policy_number == policy_number,
            ContractVersion.content_hash == content_hash,
        )
    )
    existing_version = existing.scalar_one_or_none()
    if existing_version:
        log.info("contract_already_indexed", policy_number=policy_number, hash=content_hash[:16])
        return ContractVersionSchema.model_validate(existing_version)

    # Генерируем version_id (дата + порядковый номер)
    version_id = f"v{valid_from.strftime('%Y%m%d')}"

    # Сохраняем PDF в storage
    pdf_path = f"tenants/{tenant_id}/contracts/{policy_number}/{version_id}.pdf"
    await storage.upload(pdf_bytes, pdf_path, content_type="application/pdf")

    # Закрываем предыдущую версию (valid_to)
    prev_versions = await db.execute(
        select(ContractVersion).where(
            ContractVersion.tenant_id == tenant_id,
            ContractVersion.policy_number == policy_number,
            ContractVersion.valid_to.is_(None),
        )
    )
    for prev in prev_versions.scalars():
        prev.valid_to = valid_from

    # Создаём новую версию
    contract_version = ContractVersion(
        tenant_id=tenant_id,
        policy_number=policy_number,
        version_id=version_id,
        content_hash=content_hash,
        valid_from=valid_from,
        valid_to=valid_to,
        pdf_path=pdf_path,
    )
    db.add(contract_version)
    await db.flush()

    # Извлекаем текст и создаём чанки
    text = extract_text_from_pdf(pdf_bytes)
    log.info("contract_text_extracted", policy_number=policy_number, chars=len(text))

    raw_chunks = await chunk_contract_with_claude(text)
    log.info("contract_chunked", policy_number=policy_number, chunks=len(raw_chunks))

    # Векторизуем и сохраняем
    for chunk_data in raw_chunks:
        content = chunk_data.get("content", "")
        if not content.strip():
            continue

        embedding = get_embedding(content, is_query=False)

        chunk = ContractChunk(
            tenant_id=tenant_id,
            policy_number=policy_number,
            version_id=version_id,
            section_type=chunk_data.get("section_type", "general"),
            title=chunk_data.get("title"),
            content=content,
            key_terms=chunk_data.get("key_terms", []),
            embedding=embedding,
        )
        db.add(chunk)

    await db.commit()

    log.info(
        "contract_indexed",
        policy_number=policy_number,
        version_id=version_id,
        chunks=len(raw_chunks),
    )

    return ContractVersionSchema.model_validate(contract_version)
