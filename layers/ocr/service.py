"""
Слой 3 — OCR Service.

Задача: распознать текст через Google Vision API / Document AI.
Параллельная обработка всех документов заявки.

Стратегии по типу документа:
- form_100    → Document AI Form Parser (структурный парсинг полей)
- id_document → Vision API DOCUMENT_TEXT_DETECTION
- receipt     → Document AI Form Parser (табличные данные)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import OCRFailedError
from core.models.claim import ClaimDocument, DocType
from core.storage import StorageClient

log = structlog.get_logger()
settings = get_settings()

# OCR-стратегии по типу документа.
# Используем Vision API DOCUMENT_TEXT_DETECTION для всех типов:
# Document AI Form Parser требует отдельного processor в GCP Console,
# Vision API работает сразу после включения API.
OCR_STRATEGIES: dict[DocType, str] = {
    DocType.FORM_100:    "vision_text_detection",
    DocType.ID_DOCUMENT: "vision_text_detection",
    DocType.RECEIPT:     "vision_text_detection",
}

RETRY_BACKOFF = [1, 3, 10]  # секунды между попытками


@dataclass
class TextBlock:
    text: str
    confidence: float
    bounding_box: dict | None = None


@dataclass
class OCRResult:
    doc_id: UUID
    doc_type: DocType
    full_text: str
    blocks: list[TextBlock] = field(default_factory=list)
    avg_confidence: float = 0.0
    low_confidence_blocks: int = 0
    strategy_used: str = ""


def _ocr_with_vision_api(image_bytes: bytes) -> list[TextBlock]:
    """
    Google Vision API — DOCUMENT_TEXT_DETECTION (синхронный клиент).
    Подходит для ID-документов и общего текста.
    Использует ADC (Application Default Credentials) автоматически.
    Запускается в thread executor чтобы не блокировать event loop.
    """
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)

    response = client.document_text_detection(
        image=image,
        image_context=vision.ImageContext(
            language_hints=settings.ocr_language_hints
        ),
    )

    if response.error.message:
        raise OCRFailedError(doc_id="unknown", reason=response.error.message)

    blocks: list[TextBlock] = []
    for page in response.full_text_annotation.pages:
        for block in page.blocks:
            block_text = ""
            for para in block.paragraphs:
                for word in para.words:
                    word_text = "".join(s.text for s in word.symbols)
                    block_text += word_text + " "
            bbox = None
            if block.bounding_box and block.bounding_box.vertices:
                bbox = {"vertices": [{"x": v.x, "y": v.y} for v in block.bounding_box.vertices]}
            blocks.append(TextBlock(
                text=block_text.strip(),
                confidence=block.confidence,
                bounding_box=bbox,
            ))

    return blocks


def _document_ai_text_confidence(document) -> float:
    """
    Confidence полного текста Document AI.
    Document AI не даёт document-level confidence — выводим из доступных данных:
    среднее по layout страниц → среднее по entities → 0.0 (неизвестно).
    """
    page_confs = [
        page.layout.confidence
        for page in document.pages
        if page.layout and page.layout.confidence
    ]
    if page_confs:
        return sum(page_confs) / len(page_confs)

    entity_confs = [e.confidence for e in document.entities if e.confidence]
    if entity_confs:
        return sum(entity_confs) / len(entity_confs)

    log.warning("docai_confidence_unavailable")
    return 0.0


def _ocr_with_document_ai(image_bytes: bytes) -> list[TextBlock]:
    """
    Google Document AI Form Parser — структурный парсинг форм и таблиц (синхронный клиент).
    Используется для form_100 и receipt.
    Запускается в thread executor чтобы не блокировать event loop.
    """
    from google.cloud import documentai

    client = documentai.DocumentProcessorServiceClient()
    processor_name = settings.gcp_document_ai_processor

    raw_document = documentai.RawDocument(
        content=image_bytes,
        mime_type="image/png",
    )

    request = documentai.ProcessRequest(
        name=processor_name,
        raw_document=raw_document,
    )

    result = client.process_document(request=request)
    document = result.document

    blocks: list[TextBlock] = []

    for entity in document.entities:
        confidence = entity.confidence if entity.confidence else 0.0
        blocks.append(TextBlock(
            text=f"{entity.type_}: {entity.mention_text}",
            confidence=confidence,
        ))

    if document.text:
        blocks.insert(0, TextBlock(
            text=document.text,
            confidence=_document_ai_text_confidence(document),
        ))

    return blocks


async def _ocr_single_attempt(image_bytes: bytes, doc_type: DocType) -> list[TextBlock]:
    """Один вызов OCR согласно стратегии для типа документа.
    Синхронные Google API клиенты запускаются в thread pool executor.
    """
    import functools
    loop = asyncio.get_running_loop()
    strategy = OCR_STRATEGIES.get(doc_type, "vision_text_detection")

    if strategy == "vision_text_detection":
        return await loop.run_in_executor(
            None, functools.partial(_ocr_with_vision_api, image_bytes)
        )
    else:
        return await loop.run_in_executor(
            None, functools.partial(_ocr_with_document_ai, image_bytes)
        )


async def _recognize_page(image_bytes: bytes, doc_type: DocType, doc_id: str) -> list[TextBlock]:
    """Один Vision API вызов для одной страницы с retry-логикой."""
    last_error: Exception | None = None
    for attempt in range(settings.ocr_max_retries):
        try:
            return await _ocr_single_attempt(image_bytes, doc_type)
        except Exception as e:
            last_error = e
            log.warning(
                "ocr_attempt_failed",
                doc_id=doc_id,
                attempt=attempt + 1,
                error=str(e),
            )
            if attempt < settings.ocr_max_retries - 1:
                await asyncio.sleep(RETRY_BACKOFF[attempt])
    raise OCRFailedError(doc_id=doc_id, reason=str(last_error))


async def _recognize_document(
    doc: ClaimDocument,
    storage: StorageClient,
    page_paths: list[str],
) -> "_OCRRawData":
    """
    Вызов Google Vision API для всех страниц документа (без записи в БД).
    page_paths — список путей к обработанным страницам из preprocessing.
    Для однострочных файлов (JPG/PNG) len(page_paths) == 1.
    Для многостраничных PDF len(page_paths) == кол-во страниц.
    Безопасно запускать параллельно через asyncio.gather.
    """
    with AuditTimer() as timer:
        all_blocks: list[TextBlock] = []

        for page_path in page_paths:
            image_bytes = await storage.download(page_path)
            page_blocks = await _recognize_page(image_bytes, doc.doc_type, str(doc.id))
            all_blocks.extend(page_blocks)

    strategy = OCR_STRATEGIES.get(doc.doc_type, "vision_text_detection")
    avg_confidence = sum(b.confidence for b in all_blocks) / len(all_blocks) if all_blocks else 0.0
    min_confidence = min(b.confidence for b in all_blocks) if all_blocks else 0.0
    low_conf_indices = [i for i, b in enumerate(all_blocks) if b.confidence < settings.ocr_min_confidence]
    full_text = "\n".join(b.text for b in all_blocks if b.text.strip())

    return _OCRRawData(
        doc=doc,
        blocks=all_blocks,
        full_text=full_text,
        avg_confidence=avg_confidence,
        min_confidence=min_confidence,
        low_conf_indices=low_conf_indices,
        strategy=strategy,
        duration_ms=timer.duration_ms,
        pages_count=len(page_paths),
    )


class _OCRRawData:
    """Промежуточный результат OCR до записи в БД."""
    __slots__ = ("doc", "blocks", "full_text", "avg_confidence", "min_confidence",
                 "low_conf_indices", "strategy", "duration_ms", "pages_count")

    def __init__(self, doc, blocks, full_text, avg_confidence, min_confidence,
                 low_conf_indices, strategy, duration_ms, pages_count=1):
        self.doc = doc
        self.blocks = blocks
        self.full_text = full_text
        self.avg_confidence = avg_confidence
        self.min_confidence = min_confidence
        self.low_conf_indices = low_conf_indices
        self.strategy = strategy
        self.duration_ms = duration_ms
        self.pages_count = pages_count


async def _save_ocr_result(raw: "_OCRRawData", db: AsyncSession, tenant_id: UUID) -> OCRResult:
    """
    Запись результата OCR в БД (последовательно — не вызывать из gather).
    """
    doc = raw.doc
    low_conf_blocks = len(raw.low_conf_indices)

    doc.ocr_text = raw.full_text
    doc.ocr_confidence = raw.avg_confidence
    doc.ocr_blocks = {
        "strategy": raw.strategy,
        "low_confidence_blocks": low_conf_blocks,
        "blocks": [
            {
                "text": b.text[:settings.ocr_block_text_max_chars],
                "confidence": round(b.confidence, 3),
                "bbox": b.bounding_box,
            }
            for b in raw.blocks
        ],
    }
    await db.flush()

    # $0.0015 за страницу (Google Vision API, DOCUMENT_TEXT_DETECTION)
    ocr_cost_usd = round(raw.pages_count * 0.0015, 6)

    await write_audit_entry(
        db,
        claim_id=doc.claim_id,
        tenant_id=tenant_id,
        step="ocr",
        input_data={"doc_id": str(doc.id), "doc_type": doc.doc_type.value, "strategy": raw.strategy},
        output_data={
            "avg_confidence": round(raw.avg_confidence, 3),
            "min_confidence": round(raw.min_confidence, 3),
            "blocks_count": len(raw.blocks),
            "low_confidence_blocks": low_conf_blocks,
            "low_confidence_block_indices": raw.low_conf_indices,
            "text_length": len(raw.full_text),
            "pages_count": raw.pages_count,
            "ocr_cost_usd": ocr_cost_usd,
        },
        confidence={"avg": round(raw.avg_confidence, 3), "min": round(raw.min_confidence, 3)},
        duration_ms=raw.duration_ms,
    )

    log.info(
        "ocr_completed",
        doc_id=str(doc.id),
        avg_confidence=round(raw.avg_confidence, 3),
        blocks=len(raw.blocks),
    )

    return OCRResult(
        doc_id=doc.id,
        doc_type=doc.doc_type,
        full_text=raw.full_text,
        blocks=raw.blocks,
        avg_confidence=raw.avg_confidence,
        low_confidence_blocks=low_conf_blocks,
        strategy_used=raw.strategy,
    )


async def ocr_document(
    doc: ClaimDocument,
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
    page_paths: list[str] | None = None,
) -> OCRResult:
    """OCR одного документа (Vision API + запись в БД).

    page_paths — все страницы из preprocessing (None → fallback к doc.preprocessed_path).
    """
    effective_paths = page_paths or [doc.preprocessed_path or doc.storage_path]
    raw = await _recognize_document(doc, storage, effective_paths)
    return await _save_ocr_result(raw, db, tenant_id)


async def ocr_all_documents(
    documents: list[ClaimDocument],
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
    preprocessed_docs: "list | None" = None,
) -> list[OCRResult]:
    """
    OCR всех документов заявки.
    Google Vision вызывается параллельно (медленные внешние запросы).
    Запись в БД выполняется последовательно (asyncpg не поддерживает
    конкурентные операции на одном соединении).

    preprocessed_docs — результат preprocess_all_documents; содержит page_paths
    для многостраничных PDF. Если None — fallback к doc.preprocessed_path.
    """
    # Строим маппинг doc_id → page_paths из preprocessing
    pages_map: dict[str, list[str]] = {}
    if preprocessed_docs:
        for pd in preprocessed_docs:
            if pd.page_paths:
                pages_map[str(pd.doc_id)] = pd.page_paths

    raw_results = await asyncio.gather(*[
        _recognize_document(
            doc,
            storage,
            pages_map.get(str(doc.id)) or [doc.preprocessed_path or doc.storage_path],
        )
        for doc in documents
    ])
    results = []
    for raw in raw_results:
        result = await _save_ocr_result(raw, db, tenant_id)
        results.append(result)
    return results
