"""
core/tenant_config.py — чтение per-tenant конфигурации из platform.tenant_configs.

Используется для значений, которые обновляются во время работы системы
(например confidence_calibration_factor — ежедневный job калибровки).
Settings (@lru_cache) для таких значений не подходит: кэшируется при старте процесса.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


async def get_tenant_config_float(
    db: AsyncSession,
    tenant_id: UUID,
    key: str,
    default: float,
) -> float:
    """
    Прочитать float-значение ключа тенанта; default при отсутствии или ошибке.

    Ошибки чтения не критичны (значение — поправочный коэффициент),
    поэтому деградируем до default с предупреждением в логе.
    """
    try:
        result = await db.execute(
            sa_text(
                "SELECT value FROM platform.tenant_configs "
                "WHERE tenant_id = :tid AND key = :key"
            ),
            {"tid": str(tenant_id), "key": key},
        )
        row = result.fetchone()
        if row is None:
            return default
        return float(row[0])
    except Exception as e:
        log.warning(
            "tenant_config_read_failed",
            tenant_id=str(tenant_id), key=key, error=str(e),
        )
        return default
