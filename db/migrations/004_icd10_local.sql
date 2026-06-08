-- Миграция 004: локальный справочник диагнозов МКБ-10
-- Источник: CSV/Excel с полями ID, NAME_G, NAME_E, NAME_R, AVAILABLE, PID, EXTCOD
-- Загрузка: python -m db.loaders.load_icd10 --file /path/to/icd10.csv

CREATE TABLE IF NOT EXISTS icd10_diagnoses (
    id           INT PRIMARY KEY,          -- оригинальный ID из справочника
    pid          INT,                       -- родительский ID (иерархия МКБ-10)
    extcod       VARCHAR(20),               -- код МКБ-10: J06.9, M99.3 (NULL у разделов/глав)
    name_r       TEXT,                      -- наименование на русском
    name_g       TEXT,                      -- наименование на грузинском
    name_e       TEXT,                      -- наименование на английском
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Быстрый поиск по коду МКБ-10
CREATE INDEX IF NOT EXISTS idx_icd10_extcod
    ON icd10_diagnoses (extcod)
    WHERE extcod IS NOT NULL;

-- Обход дерева родителей
CREATE INDEX IF NOT EXISTS idx_icd10_pid
    ON icd10_diagnoses (pid)
    WHERE pid IS NOT NULL;

-- Полнотекстовый поиск по трём языкам
CREATE INDEX IF NOT EXISTS idx_icd10_fts
    ON icd10_diagnoses USING gin (
        to_tsvector('russian', COALESCE(name_r, '')) ||
        to_tsvector('simple',  COALESCE(name_g, '')) ||
        to_tsvector('english', COALESCE(name_e, ''))
    );

-- Только активные
CREATE INDEX IF NOT EXISTS idx_icd10_available
    ON icd10_diagnoses (extcod)
    WHERE is_available = TRUE AND extcod IS NOT NULL;
