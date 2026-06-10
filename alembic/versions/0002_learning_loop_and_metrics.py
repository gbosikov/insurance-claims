"""Петля обучения и метрики документов: legacy SQL-миграции 008-010.

Revision ID: 0002
Revises: 0001

008 — correction_type + claude_error_reason в manual_review_outcomes (Шаг 30)
009 — quality_metrics + ocr_blocks в claim_documents (Фаза 1 персистентности)
010 — таблица diagnosis_amount_benchmarks (Шаг 24/33)

SQL-файлы написаны идемпотентно (IF NOT EXISTS) — применяются безусловно:
на БД, где их уже накатили вручную через psql, ревизия безвредна.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from db.migration_utils import LEARNING_LOOP_FILES, read_migration_statements

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for filename in LEARNING_LOOP_FILES:
        for statement in read_migration_statements(filename):
            op.execute(sa.text(statement))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS diagnosis_amount_benchmarks"))
    op.execute(sa.text(
        "ALTER TABLE claim_documents "
        "DROP COLUMN IF EXISTS quality_metrics, "
        "DROP COLUMN IF EXISTS ocr_blocks"
    ))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_review_outcomes_calibration"))
    op.execute(sa.text(
        "ALTER TABLE manual_review_outcomes "
        "DROP COLUMN IF EXISTS correction_type, "
        "DROP COLUMN IF EXISTS claude_error_reason"
    ))
