"""
Классификатор типа документа по OCR-тексту (Layer 4).

Метод: регулярные выражения на RU + KA + EN паттернах.
Вызывается в начале extract_claim_data(), до передачи текста в Claude.

Если тип, определённый здесь, отличается от типа из Layer 1 (по имени файла) —
обновляет ClaimDocument.doc_type и doc_type_source в БД,
обновляет OCRResult.doc_type чтобы Claude получил правильный лейбл в промпте.

Порог MIN_MATCHES: минимум совпадений для уверенной переклассификации.
Ниже порога — оставляем тип из Layer 1 (filename_hint).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.claim import ClaimDocument, DocType

log = structlog.get_logger()

MIN_MATCHES = 2  # минимум совпадений для уверенной переклассификации

CONTENT_PATTERNS: dict[DocType, list[str]] = {
    DocType.FORM_100: [
        # Russian
        r"форм[ауе]?\s*[№#]?\s*1?0{2}",
        r"направлени[ея]",
        r"мкб[\s\-]*10",
        r"диагноз",
        r"лечащий\s+врач",
        r"выписк[аи]",
        r"медицинск",
        r"поликлиник",
        r"стационар",
        r"амбулатор",
        r"история\s+болезни",
        # Georgian
        r"სამედიცინო",
        r"დიაგნოზ",
        r"ფორმა",
        r"მიმართულება",
        r"ამბულატორი",
        r"სტაციონარი",
        r"ექიმი",
        # English
        r"form\s*1?0{2}",
        r"icd[\s\-]*10",
        r"diagnosis",
        r"medical\s+report",
        r"discharge\s+summary",
        r"referral",
    ],
    DocType.ID_DOCUMENT: [
        # Russian
        r"личный\s+номер",
        r"паспорт",
        r"удостоверени",
        r"дата\s+рождени",
        r"гражданств",
        r"место\s+рождени",
        # Georgian
        r"პირადი\s+ნომერი",
        r"პასპორტი",
        r"მოქალაქე",
        r"დაბადების",
        r"გვარი",
        r"სახელი",
        r"საქართველო",
        # 11-значный личный номер Грузии
        r"\b\d{11}\b",
        # English
        r"personal\s+id",
        r"date\s+of\s+birth",
        r"nationality",
        r"place\s+of\s+birth",
        r"id\s+card",
        r"passport\s+number",
    ],
    DocType.RECEIPT: [
        # Russian
        r"квитанци",
        r"к\s+оплате",
        r"итого",
        r"счёт[\s\-]фактур",
        r"оплачен",
        r"получател[ьи]",
        r"плательщик",
        r"сумм[аы]\s+оплат",
        # Georgian
        r"გადასახდელი",
        r"სულ",
        r"ქვითარი",
        r"გადახდა",
        r"მომსახურება",
        r"სალაროს\s+შემოსავლის\s+ორდერი",   # кассовый ордер (наличный чек)
        r"სალაროს\s+შემოსავ",                # сокращённый вариант
        r"უნაღდო\s+ანგარიშსწორების\s+ორდერი",  # безналичный расчётный ордер
        r"მიღებულია\s+(?:ნათია|ნინო|მარია|\w+)",  # "получено от ФИО"
        r"ანგარიშსწ",                         # часть слова "безналичный расчёт"
        # English
        r"receipt",
        r"total\s+amount",
        r"invoice",
        r"amount\s+due",
        r"payment\s+received",
        # Суммы в GEL
        r"\d+[\.,]\d+\s*(?:gel|₾|lari|ლარი|лари)",
    ],
}

# Предкомпилированные паттерны — создаются один раз при импорте модуля
_COMPILED: dict[DocType, list[re.Pattern]] = {
    doc_type: [re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns]
    for doc_type, patterns in CONTENT_PATTERNS.items()
}


@dataclass
class ClassificationResult:
    doc_type: DocType
    confidence: float           # 0.0 – 1.0
    match_count: int            # количество совпавших паттернов
    changed_from: DocType | None = None  # не None если тип изменился


def classify_by_ocr_text(text: str, current_type: DocType) -> ClassificationResult:
    """
    Классифицировать документ по OCR-тексту через regex-паттерны.

    Подсчитывает совпадения для каждого типа документа.
    Побеждает тип с наибольшим числом совпадений при условии >= MIN_MATCHES.
    Если уверенности недостаточно — возвращает current_type без изменений.
    """
    scores: dict[DocType, int] = {dt: 0 for dt in DocType}

    for doc_type, patterns in _COMPILED.items():
        for pattern in patterns:
            if pattern.search(text):
                scores[doc_type] += 1

    best_type = max(scores, key=scores.get)
    best_score = scores[best_type]

    if best_score < MIN_MATCHES:
        return ClassificationResult(
            doc_type=current_type,
            confidence=0.5,
            match_count=best_score,
        )

    # confidence: 2 совпадения → ~0.65, весь набор → 1.0
    total_patterns = len(CONTENT_PATTERNS[best_type])
    confidence = min(1.0, 0.50 + (best_score / total_patterns) * 0.50)

    changed_from = current_type if best_type != current_type else None

    return ClassificationResult(
        doc_type=best_type,
        confidence=round(confidence, 3),
        match_count=best_score,
        changed_from=changed_from,
    )


async def reclassify_documents(
    ocr_results: list,   # list[OCRResult] — избегаем circular import
    db: AsyncSession,
    claim_id: UUID,
    tenant_id: UUID,
) -> list:
    """
    Переклассифицировать типы документов по OCR-тексту.

    Для каждого документа:
    - Применяет classify_by_ocr_text()
    - При изменении типа → обновляет ClaimDocument в БД (doc_type + doc_type_source)
    - Всегда обновляет doc_type_source = 'ocr_rules' (фиксируем что прошли классификатор)
    - Возвращает обновлённый список OCRResult с актуальными doc_type

    Обновлённые типы попадают в промпт Claude в Layer 4 через _build_user_message().
    """
    updated: list = []
    reclassified_count = 0

    for result in ocr_results:
        classification = classify_by_ocr_text(result.full_text, result.doc_type)

        stmt = (
            select(ClaimDocument)
            .where(ClaimDocument.id == result.doc_id)
            .where(ClaimDocument.tenant_id == tenant_id)
        )
        db_result = await db.execute(stmt)
        doc = db_result.scalar_one_or_none()

        if doc is None:
            updated.append(result)
            continue

        doc.doc_type_source = "ocr_rules"

        if classification.changed_from is not None:
            doc.doc_type = classification.doc_type
            reclassified_count += 1

            log.info(
                "doc_type_reclassified",
                claim_id=str(claim_id),
                doc_id=str(result.doc_id),
                from_type=classification.changed_from.value,
                to_type=classification.doc_type.value,
                confidence=classification.confidence,
                match_count=classification.match_count,
            )

            # Создаём новый OCRResult с исправленным типом
            from layers.ocr.service import OCRResult
            updated.append(OCRResult(
                doc_id=result.doc_id,
                doc_type=classification.doc_type,
                full_text=result.full_text,
                blocks=result.blocks,
                avg_confidence=result.avg_confidence,
                low_confidence_blocks=result.low_confidence_blocks,
                strategy_used=result.strategy_used,
            ))
        else:
            updated.append(result)

    await db.flush()

    if reclassified_count:
        log.info(
            "reclassification_complete",
            claim_id=str(claim_id),
            reclassified=reclassified_count,
            total=len(ocr_results),
        )

    return updated
