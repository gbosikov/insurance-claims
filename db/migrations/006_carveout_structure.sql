-- Миграция 006: Добавить chunk_structure для CARVEOUT-исключений в контрактах

-- ── Расширение contract_chunks ────────────────────────────────────────
-- chunk_structure содержит JSON для особых типов чанков:
-- - exclusion_with_carveout: {"type": "exclusion_with_carveout",
--                              "excluded_icd10": ["N18", "I10"],
--                              "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
--                              "general_exceptions": ["B15"]}

ALTER TABLE contract_chunks
ADD COLUMN chunk_structure JSONB DEFAULT NULL;

-- Индекс для быстрого поиска CARVEOUT-чанков
CREATE INDEX idx_contract_chunks_carveout
    ON contract_chunks USING gin (chunk_structure)
    WHERE section_type = 'exclusion_with_carveout';

-- ── Версия chunking-промпта ────────────────────────────────────────
-- Отслеживаем версию CHUNKING_SYSTEM_PROMPT для правильного re-parsing

CREATE TABLE chunking_prompt_versions (
    id SERIAL PRIMARY KEY,
    policy_number VARCHAR(50) NOT NULL,
    version_id VARCHAR(20) NOT NULL,
    tenant_id UUID NOT NULL,
    prompt_version VARCHAR(20) NOT NULL,  -- "chunking/v1.0.0", "chunking/v2.0.0" после добавления CARVEOUT-парсинга
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, policy_number, version_id)
);

CREATE INDEX idx_chunking_prompt_versions_lookup
    ON chunking_prompt_versions (tenant_id, policy_number, version_id);
