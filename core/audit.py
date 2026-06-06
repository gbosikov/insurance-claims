"""
core/audit.py — централизованная запись в аудит-лог.

ПРАВИЛО: каждый шаг обработки заявки ОБЯЗАН вызвать write_audit_entry.
Если запись не удалась — бросаем AuditLogError (критическая ошибка).
"""

import time
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import AuditLogError
from core.models.audit import AuditLog

log = structlog.get_logger()


async def write_audit_entry(
    db: AsyncSession,
    *,
    claim_id: UUID,
    tenant_id: UUID,
    step: str,
    input_data: dict[str, Any] | None = None,
    output_data: dict[str, Any] | None = None,
    confidence: dict[str, Any] | None = None,
    rag_chunks: list[str] | None = None,
    prompt_version: str | None = None,
    model_version: str | None = None,
    operator_id: UUID | None = None,
    override_reason: str | None = None,
    duration_ms: int | None = None,
) -> AuditLog:
    """
    Записывает одну строку в иммутабельный аудит-лог.

    Используется во всех 8 слоях обработки заявки:
    intake | preprocessing | ocr | extraction | rag_search | decision | routing | manual_review
    """
    try:
        entry = AuditLog(
            claim_id=claim_id,
            tenant_id=tenant_id,
            step=step,
            input_data=input_data,
            output_data=output_data,
            confidence=confidence,
            rag_chunks=rag_chunks,
            prompt_version=prompt_version,
            model_version=model_version,
            operator_id=operator_id,
            override_reason=override_reason,
            duration_ms=duration_ms,
        )
        db.add(entry)
        await db.flush()   # сбрасываем в транзакцию (без commit — вызывающий управляет транзакцией)

        log.info(
            "audit_entry_written",
            claim_id=str(claim_id),
            step=step,
            duration_ms=duration_ms,
        )
        return entry

    except Exception as e:
        log.error("audit_log_write_failed", claim_id=str(claim_id), step=step, error=str(e))
        raise AuditLogError(f"Failed to write audit log for {claim_id} step={step}: {e}") from e


class AuditTimer:
    """Context manager для замера времени выполнения шага."""

    def __init__(self) -> None:
        self._start: float = 0.0
        self.duration_ms: int = 0

    def __enter__(self) -> "AuditTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *_: Any) -> None:
        self.duration_ms = int((time.monotonic() - self._start) * 1000)
