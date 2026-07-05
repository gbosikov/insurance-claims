"""Portal users — веб-портал аутентификация.

Добавляет таблицу platform.users для входа в веб-портал (JWT).
Полностью независимо от platform.api_keys (machine-to-machine).

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL = """
CREATE TABLE IF NOT EXISTS platform.users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES platform.tenants(id),
    email           VARCHAR(200) UNIQUE NOT NULL,
    password_hash   VARCHAR(200) NOT NULL,
    full_name       VARCHAR(200),
    role            VARCHAR(20) NOT NULL DEFAULT 'viewer',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON COLUMN platform.users.role IS
    'viewer = только чтение портала; operator = ручная проверка; admin = управление системой';

CREATE INDEX IF NOT EXISTS idx_platform_users_email
    ON platform.users (email);

CREATE INDEX IF NOT EXISTS idx_platform_users_tenant
    ON platform.users (tenant_id);
"""


def upgrade() -> None:
    from db.migration_utils import split_sql_statements
    conn = op.get_bind()
    for stmt in split_sql_statements(_SQL):
        conn.exec_driver_sql(stmt)


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS platform.users"))
