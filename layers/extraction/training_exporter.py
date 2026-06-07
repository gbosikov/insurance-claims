"""
Экспорт обучающей выборки для ML-классификатора типов документов.

Источник: claim_documents WHERE doc_type_confirmed = TRUE
Формат вывода: JSONL — одна строка = один документ

Запуск:
    python -m layers.extraction.training_exporter --output dataset.jsonl
    python -m layers.extraction.training_exporter --output dataset.jsonl --min-source ocr_rules
    python -m layers.extraction.training_exporter --stats

Пример строки JSONL:
    {"text": "форма 100 диагноз J06.9...", "label": "form_100", "source": "ocr_rules"}
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import structlog
from sqlalchemy import select

from core.database import async_session_factory
from core.models.claim import ClaimDocument

log = structlog.get_logger()

# Порядок предпочтения источников (operator > ocr_rules > filename_hint)
SOURCE_PRIORITY = {"operator": 3, "ocr_rules": 2, "filename_hint": 1}


async def export_dataset(
    output_path: Path,
    min_source: str = "ocr_rules",
    tenant_id: str | None = None,
) -> dict[str, int]:
    """
    Выгружает подтверждённые документы как обучающую выборку.

    min_source: минимальный уровень источника для включения в выборку
        - 'filename_hint' — все подтверждённые (включая неопределённые по имени)
        - 'ocr_rules'     — только переклассифицированные или авто-апрув (рекомендуется)
        - 'operator'      — только подтверждённые оператором (максимум качество)
    """
    min_priority = SOURCE_PRIORITY.get(min_source, 2)
    stats: Counter = Counter()

    async with async_session_factory() as db:
        stmt = select(ClaimDocument).where(
            ClaimDocument.doc_type_confirmed == True,
            ClaimDocument.ocr_text.isnot(None),
            ClaimDocument.ocr_text != "",
        )
        if tenant_id:
            from uuid import UUID
            stmt = stmt.where(ClaimDocument.tenant_id == UUID(tenant_id))

        result = await db.execute(stmt)
        docs = result.scalars().all()

    written = 0
    with output_path.open("w", encoding="utf-8") as f:
        for doc in docs:
            source_priority = SOURCE_PRIORITY.get(doc.doc_type_source, 0)
            if source_priority < min_priority:
                stats["skipped_low_priority"] += 1
                continue

            if not doc.ocr_text or len(doc.ocr_text.strip()) < 20:
                stats["skipped_too_short"] += 1
                continue

            record = {
                "text":   doc.ocr_text.strip(),
                "label":  doc.doc_type.value,
                "source": doc.doc_type_source,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            stats[doc.doc_type.value] += 1
            written += 1

    stats["total_written"] = written
    log.info("export_complete", output=str(output_path), stats=dict(stats))
    return dict(stats)


async def print_stats(tenant_id: str | None = None) -> None:
    """Показать статистику накопленных обучающих данных."""
    async with async_session_factory() as db:
        stmt = select(ClaimDocument).where(
            ClaimDocument.doc_type_confirmed == True,
            ClaimDocument.ocr_text.isnot(None),
        )
        if tenant_id:
            from uuid import UUID
            stmt = stmt.where(ClaimDocument.tenant_id == UUID(tenant_id))

        result = await db.execute(stmt)
        docs = result.scalars().all()

    by_label: Counter = Counter(d.doc_type.value for d in docs)
    by_source: Counter = Counter(d.doc_type_source for d in docs)

    print(f"\nВсего подтверждённых документов: {len(docs)}")
    print("\nПо типу документа:")
    for label, count in sorted(by_label.items()):
        bar = "█" * (count // 5)
        ready = "✅" if count >= 200 else "⏳"
        print(f"  {ready} {label:15} {count:4}  {bar}")

    print("\nПо источнику:")
    for source, count in sorted(by_source.items()):
        print(f"  {source:15} {count:4}")

    min_class = min(by_label.values()) if by_label else 0
    if min_class >= 200:
        print(f"\n✅ Достаточно данных для обучения классификатора (минимум {min_class} на класс)")
    else:
        needed = max(0, 200 - min_class)
        print(f"\n⏳ Нужно ещё ~{needed} документов в наименьшем классе для обучения")


def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт обучающей выборки для классификатора")
    parser.add_argument("--output", type=Path, default=Path("dataset.jsonl"))
    parser.add_argument(
        "--min-source",
        choices=["filename_hint", "ocr_rules", "operator"],
        default="ocr_rules",
        help="Минимальный уровень источника (default: ocr_rules)",
    )
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--stats", action="store_true", help="Показать статистику без экспорта")
    args = parser.parse_args()

    if args.stats:
        asyncio.run(print_stats(args.tenant_id))
    else:
        stats = asyncio.run(export_dataset(args.output, args.min_source, args.tenant_id))
        print(f"Экспортировано: {stats}")


if __name__ == "__main__":
    main()
