-- Миграция 010: бенчмарки сумм по диагнозам (Шаг 24/33 — антифрод).
--
-- Перцентили сумм одобренных заявок по префиксу МКБ-10.
-- Обновляется еженедельным job-ом update_amount_benchmarks
-- (services/worker/tasks_analytics.py). Используется в check_fraud()
-- только когда fraud_amount_benchmark_enabled=True (после 3+ месяцев данных).
--
-- На существующей БД применять вручную: make psql < db/migrations/010_amount_benchmarks.sql

CREATE TABLE IF NOT EXISTS diagnosis_amount_benchmarks (
    tenant_id       UUID NOT NULL,
    icd10_prefix    VARCHAR(10) NOT NULL,     -- J06, Z00 и т.д.
    service_type    VARCHAR(50) NOT NULL DEFAULT 'all',  -- consultation | lab | imaging | hospitalization | all
    p25_amount      DECIMAL(10,2),
    p75_amount      DECIMAL(10,2),
    p95_amount      DECIMAL(10,2),
    currency        VARCHAR(3) NOT NULL DEFAULT 'GEL',
    sample_count    INT NOT NULL DEFAULT 0,   -- минимум 30 для надёжности
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, icd10_prefix, service_type, currency)
);
