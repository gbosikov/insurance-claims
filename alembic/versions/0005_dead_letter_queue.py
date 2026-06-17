"""Dead Letter Queue — постоянно упавшие Celery-задачи.

Revision ID: 0005
Revises: 4757804321fa

Задача после max_retries попадает сюда вместо молчаливого исчезновения.
Операторы видят список через GET /v1/dead-letter, могут перезапустить
(requeue) или закрыть (dismiss) каждый элемент.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "4757804321fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL = """
CREATE TABLE IF NOT EXISTS platform.dead_letter_queue (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_name       VARCHAR(100) NOT NULL,
    task_id         VARCHAR(255) UNIQUE NOT NULL,
    claim_id        UUID,
    tenant_id       UUID,
    task_args       JSONB NOT NULL DEFAULT '[]',
    task_kwargs     JSONB NOT NULL DEFAULT '{}',
    exception_type  VARCHAR(200),
    exception_msg   TEXT,
    traceback       TEXT,
    retries         INT NOT NULL DEFAULT 0,
    failed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     UUID,
    resolution      VARCHAR(50),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dlq_claim_id
    ON platform.dead_letter_queue (claim_id)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_dlq_tenant_unresolved
    ON platform.dead_letter_queue (tenant_id, failed_at DESC)
    WHERE resolved_at IS NULL;
"""


def upgrade() -> None:
    from db.migration_utils import split_sql_statements
    conn = op.get_bind()
    for stmt in split_sql_statements(_SQL):
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS platform.dead_letter_queue"))
