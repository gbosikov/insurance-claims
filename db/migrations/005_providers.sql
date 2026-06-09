-- Миграция 005: справочник провайдеров (клиник)
-- Источник: CSV с полями CUSTOMER, CSTNAME, TAXPAYER
-- Загрузка: python -m db.loaders.load_providers --file /path/to/providers.csv

CREATE TABLE IF NOT EXISTS providers (
    id          SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL UNIQUE,   -- PersID (код провайдера в кор-системе)
    cstname     TEXT NOT NULL,              -- имя клиники (может быть на EN или KA)
    taxpayer    VARCHAR(50),                -- ИНН/TIN провайдера
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Поиск по PersID (CUSTOMER)
CREATE INDEX IF NOT EXISTS idx_providers_customer_id
    ON providers (customer_id);

-- Поиск по названию клиники (fuzzy match)
CREATE INDEX IF NOT EXISTS idx_providers_cstname_trgm
    ON providers USING GIN (cstname gin_trgm_ops);

-- Полнотекстовый поиск по названию
CREATE INDEX IF NOT EXISTS idx_providers_fts
    ON providers USING GIN (
        to_tsvector('english', COALESCE(cstname, '')) ||
        to_tsvector('simple',  COALESCE(cstname, ''))  -- simple покрывает грузинский
    );

-- Только активные провайдеры
CREATE INDEX IF NOT EXISTS idx_providers_active
    ON providers (customer_id)
    WHERE is_active = TRUE;
