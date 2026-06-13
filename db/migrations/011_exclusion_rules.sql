-- Таблица правил исключений из страхового покрытия.
-- Загружается из Excel-вординга; в будущем — через API Lite GROUP по CardNumber.
--
-- scope: 'all'    — применяется ко всем застрахованным
--        'family' — дополнительно к членам семьи (CardNumber суффикс /2, /3, /4)
--
-- icd10_codes: объединённые коды и диапазоны из колонок 2 и 3 вординга.
--   Примеры элементов: "N18", "N18.0-N18.9", "F00-F99", "C00-C97"
--   (оба тире: ASCII '-' и en-dash '–' нормализуются загрузчиком к ASCII)
--
-- carveout_conditions: условия при которых исключение НЕ действует.
--   Значения: 'urgent' | 'diagnostic' | 'first_test'
--   Пример: ['urgent', 'diagnostic'] →
--     "исключено, кроме ургентных вмешательств и первичной диагностики"

CREATE TABLE IF NOT EXISTS exclusion_rules (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL,
    scope           VARCHAR(10) NOT NULL DEFAULT 'all',   -- 'all' | 'family'
    description     TEXT NOT NULL,
    icd10_codes     TEXT[] NOT NULL DEFAULT '{}',
    carveout_conditions TEXT[] NOT NULL DEFAULT '{}',
    source_row      INT,        -- номер строки в Excel (для отладки)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exclusion_rules_tenant_scope
    ON exclusion_rules (tenant_id, scope);
