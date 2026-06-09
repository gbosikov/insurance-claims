"""
Загрузчик справочника провайдеров (клиник) из CSV или Excel в локальную БД.

Использование:
    python -m db.loaders.load_providers --file providers.csv
    python -m db.loaders.load_providers --file providers.xlsx
    python -m db.loaders.load_providers --stats

Ожидаемые колонки (регистр не важен):
    CUSTOMER (или PersID, ProviderID), CSTNAME, TAXPAYER
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

BATCH_SIZE = 500  # строк за один INSERT

# Маппинг ожидаемых колонок → возможные варианты написания в файле
COLUMN_ALIASES: dict[str, list[str]] = {
    "customer_id": ["customer_id", "CUSTOMER_ID", "customer", "CUSTOMER", "persid", "PERSID", "provider_id", "PROVIDER_ID"],
    "cstname":     ["cstname", "CSTNAME", "CstName", "name", "NAME", "clinic_name", "CLINIC_NAME"],
    "taxpayer":    ["taxpayer", "TAXPAYER", "TaxPayer", "inn", "INN", "tin", "TIN"],
}


def _detect_encoding(path: Path) -> str:
    """
    Определить кодировку CSV-файла.

    Порядок проверки:
    1. UTF-16 BOM (FF FE или FE FF)
    2. UTF-8 BOM (EF BB BF)
    3. Перебор: utf-8 → cp1251 → cp1252 → latin-1
    """
    with open(path, "rb") as f:
        bom = f.read(4)

    if bom[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    if bom[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"

    # Нет BOM — пробуем декодировать образец файла
    with open(path, "rb") as f:
        sample = f.read(16384)

    for enc in ("utf-8", "cp1251", "cp1252", "latin-1"):
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue

    return "utf-8"


def _read_file(path: Path, encoding: str | None = None) -> list[dict[str, Any]]:
    """Прочитать CSV или Excel, вернуть список строк."""
    import csv
    import io

    suffix = path.suffix.lower()

    if suffix == ".csv":
        enc = encoding or _detect_encoding(path)
        print(f"Кодировка: {enc}")

        raw = path.read_bytes()
        text = raw.decode(enc)

        # Убрать BOM-символ если остался после декодирования
        if text.startswith("﻿"):
            text = text[1:]

        # Автоопределение разделителя: табуляция или запятая
        first_line = text.split("\n")[0]
        delimiter = "\t" if first_line.count("\t") > first_line.count(",") else ","
        print(f"Разделитель: {'TAB' if delimiter == chr(9) else 'запятая'}")

        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        return [dict(row) for row in reader]

    elif suffix in (".xlsx", ".xls"):
        try:
            import openpyxl
        except ImportError:
            print("Установи openpyxl: pip install openpyxl")
            sys.exit(1)
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        return [dict(zip(headers, row)) for row in rows[1:]]

    else:
        print(f"Неподдерживаемый формат: {suffix}. Нужен .csv, .xlsx или .xls")
        sys.exit(1)


def _normalize_columns(row: dict[str, Any]) -> dict[str, Any] | None:
    """Привести колонки к стандартным именам. None если строка пустая."""
    result: dict[str, Any] = {}

    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in row:
                result[field] = row[alias]
                break
        if field not in result:
            result[field] = None

    # Пропустить полностью пустые строки
    if result.get("customer_id") is None:
        return None

    return result


def _parse_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _parse_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _prepare_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нормализовать строки."""
    result = []
    skipped = 0

    for raw in raw_rows:
        row = _normalize_columns(raw)
        if row is None:
            skipped += 1
            continue

        record = {
            "customer_id": _parse_int(row["customer_id"]),
            "cstname":     _parse_str(row["cstname"]),
            "taxpayer":    _parse_str(row["taxpayer"]),
        }

        if record["customer_id"] is None or record["cstname"] is None:
            skipped += 1
            continue

        result.append(record)

    if skipped:
        log.info("rows_skipped", count=skipped)

    return result


async def _load(path: Path, encoding: str | None = None) -> None:
    from sqlalchemy import text
    from core.database import AsyncSessionLocal

    print(f"Читаю файл: {path}")
    raw_rows = _read_file(path, encoding=encoding)
    print(f"Прочитано строк: {len(raw_rows)}")

    rows = _prepare_rows(raw_rows)
    print(f"Подготовлено к загрузке: {len(rows)}")

    if not rows:
        print("Нечего загружать.")
        return

    # Проверка обязательных колонок
    sample = rows[0]
    missing_names = [f for f in ("customer_id", "cstname") if sample.get(f) is None]
    if missing_names:
        print(f"Отсутствуют обязательные колонки: {missing_names}")
        sys.exit(1)

    upsert_sql = text("""
        INSERT INTO providers (customer_id, cstname, taxpayer, is_active, updated_at)
        VALUES (:customer_id, :cstname, :taxpayer, TRUE, NOW())
        ON CONFLICT (customer_id) DO UPDATE SET
            cstname    = EXCLUDED.cstname,
            taxpayer   = EXCLUDED.taxpayer,
            is_active  = TRUE,
            updated_at = NOW()
    """)

    total = len(rows)
    loaded = 0

    async with AsyncSessionLocal() as db:
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            await db.execute(upsert_sql, batch)
            await db.commit()
            loaded += len(batch)
            print(f"  Загружено: {loaded}/{total}", end="\r")

    print(f"\nГотово. Загружено/обновлено: {loaded} записей.")


async def _is_loaded() -> bool:
    """Вернуть True если в таблице уже есть данные."""
    from sqlalchemy import text
    from core.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(text("SELECT COUNT(*) FROM providers"))
            count = result.scalar()
            return (count or 0) > 0
    except Exception:
        return False


async def _stats() -> None:
    from sqlalchemy import text
    from core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        result = await db.execute(text("""
            SELECT
                COUNT(*)                               AS total,
                COUNT(*) FILTER (WHERE is_active)     AS active,
                COUNT(*) FILTER (WHERE taxpayer IS NOT NULL) AS with_taxpayer
            FROM providers
        """))
        row = result.fetchone()

    if row is None or row.total == 0:
        print("Таблица providers пуста. Загрузите данные командой --file.")
        return

    print(f"Всего провайдеров:     {row.total}")
    print(f"Активных:              {row.active}")
    print(f"С ИНН:                 {row.with_taxpayer}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Загрузка справочника провайдеров в локальную БД"
    )
    parser.add_argument("--file", type=Path, help="Путь к CSV или Excel файлу")
    parser.add_argument(
        "--encoding",
        type=str,
        default=None,
        help="Кодировка файла: utf-8, utf-16, cp1251, cp1252 и т.д. "
             "По умолчанию определяется автоматически.",
    )
    parser.add_argument(
        "--skip-if-loaded",
        action="store_true",
        help="Пропустить загрузку если таблица уже содержит данные",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Показать статистику по таблице",
    )
    args = parser.parse_args()

    if args.stats:
        asyncio.run(_stats())
    elif args.file:
        if not args.file.exists():
            print(f"Файл не найден: {args.file}")
            sys.exit(1)
        if args.skip_if_loaded and asyncio.run(_is_loaded()):
            print("Справочник провайдеров уже загружен, пропускаем.")
            return
        asyncio.run(_load(args.file, args.encoding))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
