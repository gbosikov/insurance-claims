-- Миграция 008: типы исправлений оператора (Шаг 30 — детальная аналитика расхождений).
--
-- Оператор фиксирует ЧТО он исправил и ПОЧЕМУ Claude ошибся.
-- Эти данные питают калибровку confidence (Шаг 29) и точечное улучшение
-- промптов / RAG / quality gate.
--
-- На существующей БД применять вручную: make psql < db/migrations/008_review_correction_types.sql

ALTER TABLE manual_review_outcomes
    -- Что именно исправил оператор:
    -- amount     — изменил сумму выплаты (Claude одобрил слишком много/мало)
    -- diagnosis  — изменил покрытие диагноза (Claude ошибся в интерпретации)
    -- coverage   — изменил решение целиком (Claude одобрил → отказ или наоборот)
    -- none       — подтвердил решение Claude без изменений (QA-верификация прошла)
    ADD COLUMN IF NOT EXISTS correction_type VARCHAR(30),
    -- Почему Claude ошибся (заполняет оператор):
    -- ocr_quality        — плохое качество OCR-текста
    -- contract_gap       — договор не охватывает этот случай явно
    -- extraction_error   — Claude неверно извлёк данные из документов
    -- fraud_missed       — Claude не заметил признаки мошенничества
    -- correct            — Claude был прав, оператор подтвердил
    ADD COLUMN IF NOT EXISTS claude_error_reason VARCHAR(50);

-- Индекс для калибровочного job-а (выборка за окно по типу исправления)
CREATE INDEX IF NOT EXISTS idx_review_outcomes_calibration
    ON manual_review_outcomes (tenant_id, reviewed_at, correction_type);
