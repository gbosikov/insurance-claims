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

# OCR-стратегии по типу документа
OCR_STRATEGIES: dict[DocType, str] = {
    DocType.FORM_100:    "document_ai_form_parser",
    DocType.ID_DOCUMENT: "vision_text_detection",
    DocType.RECEIPT:     "document_ai_form_parser",
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
            blocks.append(TextBlock(
                text=block_text.strip(),
                confidence=block.confidence,
            ))

    return blocks


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
            confidence=0.90,
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


async def ocr_document(
    doc: ClaimDocument,
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
) -> OCRResult:
    """
    OCR одного документа с retry-логикой.

    1. Скачать обработанное изображение
    2. Вызвать Google API согласно стратегии
    3. Retry: MAX_RETRIES=3, backoff=[1,3,10] сек
    4. Пометить блоки с confidence < threshold
    5. Сохранить результат в ClaimDocument
    6. Запись в audit_log
    """
    with AuditTimer() as timer:
        # Скачиваем обработанное изображение (preprocessed_path приоритетнее)
        path = doc.preprocessed_path or doc.storage_path
        image_bytes = await storage.download(path)

        blocks: list[TextBlock] = []
        last_error: Exception | None = None

        for attempt in range(settings.ocr_max_retries):
            try:
                blocks = await _ocr_single_attempt(image_bytes, doc.doc_type)
                last_error = None
                break
            except Exception as e:
                last_error = e
                log.warning(
                    "ocr_attempt_failed",
                    doc_id=str(doc.id),
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt < settings.ocr_max_retries - 1:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])

        if last_error is not None:
            raise OCRFailedError(doc_id=str(doc.id), reason=str(last_error))

        # Вычисляем средний confidence
        if blocks:
            avg_confidence = sum(b.confidence for b in blocks) / len(blocks)
        else:
            avg_confidence = 0.0

        # Считаем блоки с низким confidence
        low_conf_blocks = sum(
            1 for b in blocks if b.confidence < settings.ocr_min_confidence
        )

        # Полный текст — конкатенация блоков
        full_text = "\n".join(b.text for b in blocks if b.text.strip())

        # Сохраняем в БД
        doc.ocr_text = full_text
        doc.ocr_confidence = avg_confidence
        await db.flush()

    strategy = OCR_STRATEGIES.get(doc.doc_type, "vision_text_detection")

    await write_audit_entry(
        db,
        claim_id=doc.claim_id,
        tenant_id=tenant_id,
        step="ocr",
        input_data={"doc_id": str(doc.id), "doc_type": doc.doc_type.value, "strategy": strategy},
        output_data={
            "avg_confidence": round(avg_confidence, 3),
            "blocks_count": len(blocks),
            "low_confidence_blocks": low_conf_blocks,
            "text_length": len(full_text),
        },
        confidence={"avg": round(avg_confidence, 3)},
        duration_ms=timer.duration_ms,
    )

    log.info(
        "ocr_completed",
        doc_id=str(doc.id),
        avg_confidence=round(avg_confidence, 3),
        blocks=len(blocks),
    )

    return OCRResult(
        doc_id=doc.id,
        doc_type=doc.doc_type,
        full_text=full_text,
        blocks=blocks,
        avg_confidence=avg_confidence,
        low_confidence_blocks=low_conf_blocks,
        strategy_used=strategy,
    )


async def ocr_all_documents(
    documents: list[ClaimDocument],
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
) -> list[OCRResult]:
    """
    Параллельный OCR всех документов заявки через asyncio.gather.
    При ошибке одного документа — OCRFailedError пропагируется выше.
    """
    tasks = [
        ocr_document(doc, storage, db, tenant_id)
        for doc in documents
    ]
    return await asyncio.gather(*tasks)
