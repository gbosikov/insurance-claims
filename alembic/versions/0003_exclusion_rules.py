"""Таблица правил исключений по вордингу страховых условий.

Revision ID: 0003
Revises: 0002

Создаёт exclusion_rules — детерминированные проверки до Claude (Уровень 1).
Загружается через: python -m db.loaders.load_exclusions --file db/data/exclusions.xlsx
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL = """
CREATE TABLE IF NOT EXISTS exclusion_rules (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL,
    scope           VARCHAR(10) NOT NULL DEFAULT 'all',
    description     TEXT NOT NULL,
    icd10_codes     TEXT[] NOT NULL DEFAULT '{}',
    carveout_conditions TEXT[] NOT NULL DEFAULT '{}',
    source_row      INT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exclusion_rules_tenant_scope
    ON exclusion_rules (tenant_id, scope);
"""


def upgrade() -> None:
    from db.migration_utils import split_sql_statements
    conn = op.get_bind()
    for statement in split_sql_statements(_SQL):
        conn.exec_driver_sql(statement)


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_exclusion_rules_tenant_scope"))
    op.execute(sa.text("DROP TABLE IF EXISTS exclusion_rules"))
