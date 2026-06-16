"""Unit тесты: db/migration_utils.py — разбиение legacy SQL для Alembic-ревизий."""

import pytest

from db.migration_utils import (
    EXCLUSION_RULES_FILES,
    LEARNING_LOOP_FILES,
    LEGACY_BASELINE_FILES,
    MIGRATIONS_DIR,
    read_migration_statements,
    split_sql_statements,
)


# ── split_sql_statements ──────────────────────────────────────────


def test_split_two_statements():
    sql = "CREATE TABLE a (id int);\nCREATE TABLE b (id int);"
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert statements[1].startswith("CREATE TABLE b")


def test_split_ignores_semicolon_inside_string():
    sql = "INSERT INTO t (v) VALUES ('a;b');\nSELECT 1;"
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert "'a;b'" in statements[0]


def test_split_ignores_semicolon_inside_line_comment():
    sql = "SELECT 1; -- комментарий; с точкой с запятой\nSELECT 2;"
    statements = split_sql_statements(sql)
    assert len(statements) == 2


def test_split_handles_escaped_quote():
    sql = "INSERT INTO t (v) VALUES ('it''s; ok');\nSELECT 1;"
    statements = split_sql_statements(sql)
    assert len(statements) == 2
    assert "it''s; ok" in statements[0]


def test_split_drops_comment_only_chunks():
    sql = "-- только комментарий\n\nSELECT 1;\n-- хвостовой комментарий\n"
    statements = split_sql_statements(sql)
    assert len(statements) == 1


# ── Реальные файлы миграций ───────────────────────────────────────


ALL_FILES = LEGACY_BASELINE_FILES + LEARNING_LOOP_FILES + EXCLUSION_RULES_FILES


def test_file_lists_cover_migrations_dir():
    """Списки в migration_utils соответствуют файлам на диске —
    новый SQL-файл без регистрации в ревизии не останется незамеченным."""
    on_disk = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert sorted(ALL_FILES) == on_disk


@pytest.mark.parametrize("filename", ALL_FILES)
def test_every_migration_splits_into_nonempty_statements(filename):
    statements = split_sql_statements((MIGRATIONS_DIR / filename).read_text(encoding="utf-8"))
    assert statements, f"{filename}: не извлечено ни одного statement"
    for stmt in statements:
        assert stmt.strip(), f"{filename}: пустой statement"
        assert not stmt.strip().startswith("--") or "\n" in stmt


@pytest.mark.parametrize("filename", ALL_FILES)
def test_no_dollar_quoting(filename):
    """Предусловие сплиттера: dollar-quoting не поддерживается.
    Появится $$-функция → переходить на полноценный парсер (sqlparse)."""
    content = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    assert "$$" not in content, f"{filename}: dollar-quoting не поддержан сплиттером"


@pytest.mark.parametrize("filename", LEARNING_LOOP_FILES)
def test_learning_loop_migrations_are_idempotent(filename):
    """Ревизия 0002 применяет 008-010 безусловно — файлы обязаны быть идемпотентными."""
    content = (MIGRATIONS_DIR / filename).read_text(encoding="utf-8").upper()
    assert "IF NOT EXISTS" in content, f"{filename}: должен использовать IF NOT EXISTS"


def test_initial_migration_statement_count_sane():
    """001_initial.sql — крупный файл: extensions, типы, ~15 таблиц, индексы, правила."""
    statements = read_migration_statements("001_initial.sql")
    assert len(statements) > 20
    joined = "\n".join(statements).upper()
    assert "CREATE TABLE CLAIMS" in joined.replace('"', "")
    assert "CREATE EXTENSION" in joined
    # append-only правила аудита не потерялись при разбиении
    assert "AUDIT_LOG_NO_UPDATE" in joined
    assert "AUDIT_LOG_NO_DELETE" in joined
