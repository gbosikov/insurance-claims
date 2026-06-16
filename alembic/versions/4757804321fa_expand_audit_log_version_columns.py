"""expand_audit_log_version_columns

Revision ID: 4757804321fa
Revises: 0004
Create Date: 2026-06-14 17:22:11.227419

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '4757804321fa'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        'audit_log', 'prompt_version',
        existing_type=sa.String(20),
        type_=sa.String(100),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        'audit_log', 'prompt_version',
        existing_type=sa.String(100),
        type_=sa.String(20),
        existing_nullable=True,
    )
