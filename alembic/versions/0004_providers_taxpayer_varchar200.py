"""Расширить providers.taxpayer до VARCHAR(200).

Revision ID: 0004
Revises: 0003

Колонка taxpayer VARCHAR(50) слишком мала для некоторых записей в Cliniks.csv
(отдельные строки содержат длинные описания вместо короткого ИНН).
Расширяем до VARCHAR(200) — безопасная, обратно-совместимая операция.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE providers ALTER COLUMN taxpayer TYPE VARCHAR(200)"
    )


def downgrade() -> None:
    # Возврат к VARCHAR(50) может потерять данные — выполнять осознанно.
    op.execute(
        "ALTER TABLE providers ALTER COLUMN taxpayer TYPE VARCHAR(50) USING LEFT(taxpayer, 50)"
    )
