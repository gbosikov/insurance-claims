-- ── Таблица: POSITIVE LIST процедур (явно покрытые процедуры) ──────────────
-- Раздел 1.7.3-1.7.4 грузинских ДМС-контрактов содержит явный перечень
-- покрытых процедур/услуг (полипэктомия, аденоидэктомия, стентирование и т.д.)
-- Эти процедуры ВСЕГДА покрыты, независимо от CARVEOUT-исключений.

CREATE TABLE positive_list_procedures (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID REFERENCES platform.tenants(id),
    policy_number       VARCHAR(50) NOT NULL,
    version_id          VARCHAR(20) NOT NULL,
    procedure_code      VARCHAR(50),                  -- SNOMED, ICD-9-CM или внутренний код
    procedure_name_ka   TEXT NOT NULL,                -- На грузинском
    procedure_name_ru   TEXT,                         -- На русском
    procedure_name_en   TEXT,                         -- На английском
    coverage_percent    DECIMAL(5,2) DEFAULT 100.0,   -- % покрытия
    sublimit            DECIMAL(10,2),                -- Суб-лимит для процедуры, если есть
    section_reference   VARCHAR(20),                  -- Например "1.7.3" (где в контракте)
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, policy_number, version_id, procedure_code)
);

CREATE INDEX idx_positive_list_lookup
    ON positive_list_procedures (tenant_id, policy_number, version_id);

CREATE INDEX idx_positive_list_procedure_code
    ON positive_list_procedures (tenant_id, procedure_code);


-- ── Таблица: Результаты парсинга POSITIVE LIST (audit) ────────────────────
CREATE TABLE positive_list_parsing_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL,
    policy_number       VARCHAR(50) NOT NULL,
    version_id          VARCHAR(20) NOT NULL,
    claude_prompt_used  VARCHAR(50),                  -- версия промпта
    raw_response        TEXT,                         -- сырой ответ от Claude
    procedures_found    INT,                          -- сколько процедур распарсили
    errors              TEXT[],                       -- какие-то ошибки/предупреждения
    parsed_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_parsing_log_lookup
    ON positive_list_parsing_log (tenant_id, policy_number, version_id);
