"""
Включить/отключить DB-защиту audit_log от DELETE/UPDATE (audit_log_no_delete/no_update).

audit_log хранит запись каждого решения системы (CLAUDE.md правило №5) и защищён
на уровне БД правилами `ON DELETE/UPDATE DO INSTEAD NOTHING` — это намеренная
compliance-защита, не баг. Отключать её можно ТОЛЬКО для очистки тестовых данных
в dev-окружении (например, после ручного тестирования через /devtools).

Состояние читается из settings.audit_log_immutable (AUDIT_LOG_IMMUTABLE в .env):
  AUDIT_LOG_IMMUTABLE=true  (дефолт) → защита включена, скрипт её обеспечивает
  AUDIT_LOG_IMMUTABLE=false            → защита снята

Запуск:
    docker compose exec api python -m scripts.toggle_audit_log_lock

Скрипт ВСЕГДА отказывается снимать защиту при ENVIRONMENT=production —
независимо от значения AUDIT_LOG_IMMUTABLE в .env.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from core.config import get_settings
from core.database import AsyncSessionLocal

settings = get_settings()

_RULES = ("audit_log_no_delete", "audit_log_no_update")


async def apply_lock_state() -> int:
    if not settings.audit_log_immutable and settings.environment == "production":
        print(
            "ОТКАЗ: AUDIT_LOG_IMMUTABLE=false в ENVIRONMENT=production. "
            "Защита audit_log не может быть снята в production."
        )
        return 1

    action = "ENABLE" if settings.audit_log_immutable else "DISABLE"

    async with AsyncSessionLocal() as db:
        for rule in _RULES:
            await db.execute(text(f"ALTER TABLE audit_log {action} RULE {rule}"))
        await db.commit()

    state = "включена" if settings.audit_log_immutable else "снята"
    print(f"Защита audit_log (DELETE/UPDATE) {state}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(apply_lock_state()))
