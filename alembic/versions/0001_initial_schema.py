"""Базовая схема: legacy SQL-миграции 001-007.

Revision ID: 0001
Revises: None

Применяет db/migrations/001-007 на ПУСТОЙ БД. Если таблица claims уже
существует (БД создана через docker-entrypoint-initdb.d до внедрения
Alembic) — ревизия пропускается: baseline уже применён, Alembic просто
фиксирует это в alembic_version. Так upgrade head работает одинаково
на свежей и на существующей БД без ручного stamp.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from db.migration_utils import LEGACY_BASELINE_FILES, read_migration_statements

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("claims"):
        # БД создана docker-entrypoint-initdb.d — схема 001-007 уже есть
        return

    for filename in LEGACY_BASELINE_FILES:
        for statement in read_migration_statements(filename):
            op.execute(sa.text(statement))


def downgrade() -> None:
    raise NotImplementedError(
        "Baseline-ревизия необратима: откат начальной схемы = удаление всех данных. "
        "Восстанавливайтесь из бэкапа."
    )
