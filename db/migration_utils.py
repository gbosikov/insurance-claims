"""
Утилиты для Alembic-ревизий: чтение legacy SQL-миграций (db/migrations/*.sql)
и разбиение на отдельные statements.

Разбиение необходимо: asyncpg не выполняет несколько statements одним вызовом
(prepared statements). Сплиттер учитывает строковые литералы ('...') и
строчные комментарии (-- ...); dollar-quoting ($$) в наших миграциях
не используется — проверяется тестом tests/unit/test_migration_utils.py.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# 001–007: базовая схема. Применяются ревизией 0001 только на пустой БД —
# на существующей (созданной docker-entrypoint-initdb.d) пропускаются.
LEGACY_BASELINE_FILES = [
    "001_initial.sql",
    "002_doc_type_training.sql",
    "003_source_url.sql",
    "004_icd10_local.sql",
    "005_providers.sql",
    "006_carveout_structure.sql",
    "007_positive_list_procedures.sql",
]

# 008–010: петля обучения и метрики документов. Написаны идемпотентно
# (IF NOT EXISTS) — ревизия 0002 применяет их безусловно.
LEARNING_LOOP_FILES = [
    "008_review_correction_types.sql",
    "009_document_metrics.sql",
    "010_amount_benchmarks.sql",
]

# 011: правила исключений по вордингу (exclusion_rules).
# Применяется ревизией 0003 (инлайн SQL); файл используется для initdb-бутстрапа.
EXCLUSION_RULES_FILES = [
    "011_exclusion_rules.sql",
]


def split_sql_statements(sql: str) -> list[str]:
    """
    Разбить SQL-скрипт на statements по «;» на верхнем уровне.

    Игнорирует «;» внутри одинарных кавычек и строчных комментариев.
    Block-комментарии (/* */) и dollar-quoting не поддерживаются —
    в наших миграциях их нет (закреплено тестом).
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    in_line_comment = False
    i = 0

    while i < len(sql):
        char = sql[i]

        if in_line_comment:
            current.append(char)
            if char == "\n":
                in_line_comment = False
        elif in_string:
            current.append(char)
            if char == "'":
                # '' внутри строки — экранированная кавычка, не конец строки
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append("'")
                    i += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
            current.append(char)
        elif char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            in_line_comment = True
            current.append(char)
        elif char == ";":
            statement = "".join(current).strip()
            if _has_sql_content(statement):
                statements.append(statement)
            current = []
        else:
            current.append(char)
        i += 1

    tail = "".join(current).strip()
    if _has_sql_content(tail):
        statements.append(tail)

    return statements


def _has_sql_content(statement: str) -> bool:
    """Statement содержит что-то кроме комментариев и пустых строк."""
    for line in statement.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return True
    return False


def read_migration_statements(filename: str) -> list[str]:
    """Прочитать legacy-миграцию и вернуть список statements."""
    path = MIGRATIONS_DIR / filename
    return split_sql_statements(path.read_text(encoding="utf-8"))
