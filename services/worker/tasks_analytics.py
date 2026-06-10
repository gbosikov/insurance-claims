"""
Celery Tasks — петля обучения (Шаги 29, 33).

calibrate_confidence (ежедневно, Celery Beat):
  Сравнивает заявленный AI-confidence с реальной точностью по проверенным
  оператором заявкам и обновляет platform.tenant_configs
  ['confidence_calibration_factor']. Фактор применяется в make_decision().

update_amount_benchmarks (еженедельно, Celery Beat):
  Пересчитывает P25/P75/P95 сумм одобренных заявок по префиксу МКБ-10
  (таблица diagnosis_amount_benchmarks, миграция 010). No-op пока
  fraud_amount_benchmark_enabled=False.

Beat не умеет параметризовать задачи по тенантам, поэтому есть диспетчеры
*_all: перебирают активные platform.tenants и ставят per-tenant задачи.
"""

from __future__ import annotations

import asyncio
import json

import structlog
from sqlalchemy import text as sa_text

from core.config import get_settings
from core.database import AsyncSessionLocal
from services.worker.celery_app import celery_app

log = structlog.get_logger()
settings = get_settings()

CALIBRATION_CONFIG_KEY = "confidence_calibration_factor"


def run_async(coro):
    """Запускает async-корутину в синхронном контексте Celery."""
    return asyncio.run(coro)


async def _get_active_tenant_ids() -> list[str]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            sa_text("SELECT id FROM platform.tenants WHERE status = 'active'")
        )
        return [str(row[0]) for row in result.fetchall()]


def compute_calibration_factor(
    actual_accuracy: float,
    mean_claimed: float,
    old_factor: float,
) -> tuple[float, bool]:
    """
    Чистая математика калибровки (Шаг 29).

    Возвращает (новый фактор, нужно ли обновление).
    Обновление только при значимом расхождении (learning_calibration_significant_diff);
    новый фактор клампится в [learning_calibration_factor_min, learning_calibration_factor_max].
    """
    if mean_claimed <= 0:
        return old_factor, False

    diff = abs(actual_accuracy - mean_claimed)
    if diff <= settings.learning_calibration_significant_diff:
        return old_factor, False

    new_factor = max(
        settings.learning_calibration_factor_min,
        min(settings.learning_calibration_factor_max, actual_accuracy / mean_claimed),
    )
    return new_factor, True


# ── Шаг 29: калибровка confidence ─────────────────────────────────

@celery_app.task(name="calibrate_confidence_all", queue="analytics")
def calibrate_confidence_all() -> dict:
    """Диспетчер: запустить калибровку для каждого активного тенанта."""
    tenant_ids = run_async(_get_active_tenant_ids())
    for tid in tenant_ids:
        calibrate_confidence_task.delay(tid)
    log.info("calibrate_confidence_dispatched", tenants=len(tenant_ids))
    return {"dispatched": len(tenant_ids)}


@celery_app.task(name="calibrate_confidence", queue="analytics")
def calibrate_confidence_task(tenant_id: str) -> dict:
    return run_async(_calibrate_confidence(tenant_id))


async def _calibrate_confidence(tenant_id: str) -> dict:
    """
    1. Проверенные оператором заявки за окно (manual_review_outcomes.correction_type)
       + raw confidence их decision-записей из audit_log
    2. actual_accuracy = count(correction_type='none') / count(*)
    3. Минимум learning_min_samples_for_calibration проверок — иначе skip
    4. |actual − mean_claimed| > learning_calibration_significant_diff →
       новый фактор = actual/claimed с клампом → upsert platform.tenant_configs
    5. Запись запуска в platform.usage_events (event_type='calibration_run')
    """
    if not settings.learning_feedback_loop_enabled:
        return {"status": "disabled"}

    async with AsyncSessionLocal() as db:
        # Для каждой проверенной заявки берём raw confidence из последней
        # decision-записи. overall_raw — некалиброванное значение (важно:
        # калибровка по уже откалиброванному confidence компаундила бы фактор).
        result = await db.execute(
            sa_text("""
                SELECT o.correction_type,
                       COALESCE(
                           (a.confidence->>'overall_raw')::float,
                           (a.confidence->>'overall')::float
                       ) AS claimed_confidence
                FROM manual_review_outcomes o
                LEFT JOIN LATERAL (
                    SELECT confidence
                    FROM audit_log
                    WHERE claim_id = o.claim_id
                      AND tenant_id = o.tenant_id
                      AND step = 'decision'
                    ORDER BY timestamp DESC
                    LIMIT 1
                ) a ON TRUE
                WHERE o.tenant_id = :tid
                  AND o.reviewed_at > NOW() - make_interval(days => :days)
                  AND o.correction_type IS NOT NULL
            """),
            {"tid": tenant_id, "days": settings.learning_calibration_window_days},
        )
        rows = result.fetchall()

        total = len(rows)
        required = settings.learning_min_samples_for_calibration
        if total < required:
            log.info(
                "calibration_skipped_insufficient_samples",
                tenant_id=tenant_id, samples=total, required=required,
            )
            return {"status": "skipped_insufficient_samples", "samples": total, "required": required}

        correct = sum(1 for r in rows if r[0] == "none")
        actual_accuracy = correct / total

        confidences = [r[1] for r in rows if r[1] is not None]
        if not confidences:
            log.warning("calibration_skipped_no_confidence_data", tenant_id=tenant_id)
            return {"status": "skipped_no_confidence_data", "samples": total}
        mean_claimed = sum(confidences) / len(confidences)

        old_factor = await _get_current_factor(db, tenant_id)
        new_factor, updated = compute_calibration_factor(actual_accuracy, mean_claimed, old_factor)

        if not updated:
            log.info(
                "calibration_no_update_needed",
                tenant_id=tenant_id,
                actual_accuracy=round(actual_accuracy, 3),
                mean_claimed=round(mean_claimed, 3),
                factor=old_factor,
            )
        else:
            await db.execute(
                sa_text("""
                    INSERT INTO platform.tenant_configs (tenant_id, key, value, updated_at)
                    VALUES (:tid, :key, :value, NOW())
                    ON CONFLICT (tenant_id, key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """),
                {"tid": tenant_id, "key": CALIBRATION_CONFIG_KEY, "value": str(round(new_factor, 4))},
            )

        # Журнал запуска — usage_events (audit_log требует claim_id)
        await db.execute(
            sa_text("""
                INSERT INTO platform.usage_events (tenant_id, event_type, quantity, metadata)
                VALUES (:tid, 'calibration_run', 1, CAST(:metadata AS JSONB))
            """),
            {
                "tid": tenant_id,
                "metadata": json.dumps({
                    "actual_accuracy": round(actual_accuracy, 4),
                    "mean_claimed_confidence": round(mean_claimed, 4),
                    "factor_before": old_factor,
                    "factor_after": round(new_factor, 4),
                    "updated": updated,
                    "sample_size": total,
                    "window_days": settings.learning_calibration_window_days,
                }),
            },
        )
        await db.commit()

    log.info(
        "calibration_completed",
        tenant_id=tenant_id,
        actual_accuracy=round(actual_accuracy, 3),
        mean_claimed=round(mean_claimed, 3),
        factor_before=old_factor,
        factor_after=round(new_factor, 4),
        updated=updated,
        sample_size=total,
    )
    return {
        "status": "updated" if updated else "no_change",
        "actual_accuracy": round(actual_accuracy, 4),
        "mean_claimed_confidence": round(mean_claimed, 4),
        "factor": round(new_factor, 4),
        "sample_size": total,
    }


async def _get_current_factor(db, tenant_id: str) -> float:
    result = await db.execute(
        sa_text("""
            SELECT value FROM platform.tenant_configs
            WHERE tenant_id = :tid AND key = :key
        """),
        {"tid": tenant_id, "key": CALIBRATION_CONFIG_KEY},
    )
    row = result.fetchone()
    if row is None:
        return settings.decision_confidence_calibration_factor
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return settings.decision_confidence_calibration_factor


# ── Шаг 33: бенчмарки сумм по диагнозам ───────────────────────────

@celery_app.task(name="update_amount_benchmarks_all", queue="analytics")
def update_amount_benchmarks_all() -> dict:
    """Диспетчер: пересчитать бенчмарки для каждого активного тенанта."""
    tenant_ids = run_async(_get_active_tenant_ids())
    for tid in tenant_ids:
        update_amount_benchmarks_task.delay(tid)
    log.info("update_amount_benchmarks_dispatched", tenants=len(tenant_ids))
    return {"dispatched": len(tenant_ids)}


@celery_app.task(name="update_amount_benchmarks", queue="analytics")
def update_amount_benchmarks_task(tenant_id: str) -> dict:
    return run_async(_update_amount_benchmarks(tenant_id))


async def _update_amount_benchmarks(tenant_id: str) -> dict:
    """
    P25/P75/P95 сумм одобренных заявок за 90 дней по префиксу МКБ-10.
    Только префиксы с ≥30 заявками (надёжность перцентилей).
    """
    if not settings.fraud_amount_benchmark_enabled:
        log.info("amount_benchmarks_disabled", tenant_id=tenant_id)
        return {"status": "disabled", "hint": "fraud_amount_benchmark_enabled=False"}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            sa_text("""
                INSERT INTO diagnosis_amount_benchmarks
                    (tenant_id, icd10_prefix, service_type,
                     p25_amount, p75_amount, p95_amount,
                     currency, sample_count, updated_at)
                SELECT c.tenant_id,
                       split_part(dd.icd10_code, '.', 1),
                       'all',
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY c.total_claimed),
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY c.total_claimed),
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY c.total_claimed),
                       :currency,
                       count(*),
                       NOW()
                FROM claims c
                JOIN diagnosis_decisions dd ON dd.claim_id = c.id
                WHERE c.tenant_id = :tid
                  AND c.status IN ('AUTO_APPROVED', 'PAID')
                  AND c.total_claimed IS NOT NULL
                  AND c.created_at > NOW() - INTERVAL '90 days'
                  AND dd.is_covered = TRUE
                GROUP BY c.tenant_id, split_part(dd.icd10_code, '.', 1)
                HAVING count(*) >= 30
                ON CONFLICT (tenant_id, icd10_prefix, service_type, currency)
                DO UPDATE SET p25_amount   = EXCLUDED.p25_amount,
                              p75_amount   = EXCLUDED.p75_amount,
                              p95_amount   = EXCLUDED.p95_amount,
                              sample_count = EXCLUDED.sample_count,
                              updated_at   = NOW()
            """),
            {"tid": tenant_id, "currency": settings.manual_review_currency},
        )
        await db.commit()
        updated_rows = result.rowcount or 0

    log.info("amount_benchmarks_updated", tenant_id=tenant_id, prefixes=updated_rows)
    return {"status": "updated", "prefixes": updated_rows}
