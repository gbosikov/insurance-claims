"""
Загрузчик справочника МКБ-10 из CSV или Excel в локальную БД.

Использование:
    python -m db.loaders.load_icd10 --file icd10.csv
    python -m db.loaders.load_icd10 --file icd10.xlsx
    python -m db.loaders.load_icd10 --file icd10.csv --only-available
    python -m db.loaders.load_icd10 --stats

Ожидаемые колонки (регистр не важен):
    ID, NAME_G, NAME_E, NAME_R, AVAILABLE, PID, EXTCOD

Поддерживаемые форматы AVAILABLE: True/False, true/false, 1/0, yes/no, да/нет
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
    "id":           ["id", "ID", "Id"],
    "pid":          ["pid", "PID", "Pid", "parent_id", "PARENT_ID"],
    "extcod":       ["extcod", "EXTCOD", "ExtCod", "icd_code", "code", "CODE"],
    "name_r":       ["name_r", "NAME_R", "NameR", "name_ru", "NAME_RU"],
    "name_g":       ["name_g", "NAME_G", "NameG", "name_ka", "NAME_KA", "name_a", "NAME_A", "NameA"],
    "name_e":       ["name_e", "NAME_E", "NameE", "name_en", "NAME_EN"],
    "is_available": ["available", "AVAILABLE", "Available", "is_available"],
}


def _detect_encoding(path: Path) -> str:
    """
    Определить кодировку CSV-файла.

    Порядок проверки:
    1. UTF-16 BOM (FF FE или FE FF) — типичный экспорт из Excel
    2. UTF-8 BOM (EF BB BF)
    3. Перебор: utf-8 → cp1251 → cp1252 → latin-1
    """
    with open(path, "rb") as f:
        bom = f.read(4)

    if bom[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"        # Python сам определит порядок байт по BOM
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

    return "utf-8"  # последний fallback


def _read_file(path: Path, encoding: str | None = None) -> list[dict[str, Any]]:
    """Прочитать CSV или Excel, вернуть список строк."""
    import csv
    import io

    suffix = path.suffix.lower()

    if suffix == ".csv":
        enc = encoding or _detect_encoding(path)
        print(f"Кодировка: {enc}")

        # Читаем бинарно → декодируем → передаём в csv.DictReader
        # Это надёжнее чем open(..., encoding=enc) для UTF-16
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
        # openpyxl читает Excel нативно в Unicode — кодировка не нужна
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
        # Если поле не найдено — None
        if field not in result:
            result[field] = None

    # Пропустить полностью пустые строки
    if result.get("id") is None:
        return None

    return result


def _parse_bool(value: Any) -> bool:
    """Конвертировать разные форматы boolean в Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "да", "t", "y")
    return True  # если не распознано — считаем активным


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


def _prepare_rows(
    raw_rows: list[dict[str, Any]],
    only_available: bool,
) -> list[dict[str, Any]]:
    """Нормализовать и отфильтровать строки."""
    result = []
    skipped = 0

    for raw in raw_rows:
        row = _normalize_columns(raw)
        if row is None:
            skipped += 1
            continue

        record = {
            "id":           _parse_int(row["id"]),
            "pid":          _parse_int(row["pid"]),
            "extcod":       _parse_str(row["extcod"]),
            "name_r":       _parse_str(row["name_r"]),
            "name_g":       _parse_str(row["name_g"]),
            "name_e":       _parse_str(row["name_e"]),
            "is_available": _parse_bool(row["is_available"]),
        }

        if record["id"] is None:
            skipped += 1
            continue

        if only_available and not record["is_available"]:
            skipped += 1
            continue

        result.append(record)

    if skipped:
        log.info("rows_skipped", count=skipped)

    return result


async def _load(path: Path, only_available: bool, encoding: str | None = None) -> None:
    from sqlalchemy import text
    from core.database import AsyncSessionLocal

    print(f"Читаю файл: {path}")
    raw_rows = _read_file(path, encoding=encoding)
    print(f"Прочитано строк: {len(raw_rows)}")

    rows = _prepare_rows(raw_rows, only_available)
    print(f"Подготовлено к загрузке: {len(rows)}")

    if not rows:
        print("Нечего загружать.")
        return

    # Проверка что все обязательные колонки нашлись
    sample = rows[0]
    missing_names = [f for f in ("id",) if sample.get(f) is None]
    if missing_names:
        print(f"Отсутствуют обязательные колонки: {missing_names}")
        sys.exit(1)

    upsert_sql = text("""
        INSERT INTO icd10_diagnoses (id, pid, extcod, name_r, name_g, name_e, is_available, updated_at)
        VALUES (:id, :pid, :extcod, :name_r, :name_g, :name_e, :is_available, NOW())
        ON CONFLICT (id) DO UPDATE SET
            pid          = EXCLUDED.pid,
            extcod       = EXCLUDED.extcod,
            name_r       = EXCLUDED.name_r,
            name_g       = EXCLUDED.name_g,
            name_e       = EXCLUDED.name_e,
            is_available = EXCLUDED.is_available,
            updated_at   = NOW()
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
            result = await db.execute(text("SELECT COUNT(*) FROM icd10_diagnoses"))
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
                COUNT(*)                                      AS total,
                COUNT(*) FILTER (WHERE is_available = TRUE)  AS available,
                COUNT(*) FILTER (WHERE extcod IS NOT NULL)   AS with_code,
                COUNT(*) FILTER (WHERE pid IS NULL)          AS root_nodes
            FROM icd10_diagnoses
        """))
        row = result.fetchone()

    if row is None or row.total == 0:
        print("Таблица icd10_diagnoses пуста. Загрузите данные командой --file.")
        return

    print(f"Всего записей:       {row.total}")
    print(f"Активных:            {row.available}")
    print(f"С кодом МКБ-10:      {row.with_code}")
    print(f"Корневых узлов:      {row.root_nodes}")


async def _main_async(args: argparse.Namespace) -> None:
    """Единый async entry point — один event loop для всех операций с БД."""
    if args.stats:
        await _stats()
    elif args.file:
        if not args.file.exists():
            print(f"Файл не найден: {args.file}")
            sys.exit(1)
        if args.skip_if_loaded and await _is_loaded():
            print("Справочник МКБ-10 уже загружен, пропускаем.")
            return
        await _load(args.file, args.only_available, args.encoding)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Загрузка справочника МКБ-10 в локальную БД"
    )
    parser.add_argument("--file", type=Path, help="Путь к CSV или Excel файлу")
    parser.add_argument(
        "--only-available",
        action="store_true",
        help="Загружать только записи с AVAILABLE=True",
    )
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

    if not args.stats and not args.file:
        parser.print_help()
        return

    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
