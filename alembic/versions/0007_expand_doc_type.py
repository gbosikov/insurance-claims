"""Расширение таксономии DocType — автоматическое определение типа документа.

Добавляет discharge_summary, lab_result, prescription, other к enum doc_type.
`other` — обязательный fallback для неуверенной классификации (→ manual_review).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_VALUES = ["discharge_summary", "lab_result", "prescription", "other"]


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE не может выполняться в той же транзакции,
    # где новое значение затем используется — Postgres требует autocommit.
    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE doc_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    raise NotImplementedError(
        "Postgres не поддерживает удаление значений enum без пересоздания типа"
    )
