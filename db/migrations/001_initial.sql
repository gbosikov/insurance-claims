-- ═══════════════════════════════════════════════════════════════════
-- 001_initial.sql — начальная схема Insurance Claims Processing System
-- ═══════════════════════════════════════════════════════════════════

-- ── РАСШИРЕНИЯ ────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- fuzzy search для ФИО

-- ── ТИПЫ ──────────────────────────────────────────────────────────
CREATE TYPE claim_status AS ENUM (
    'RECEIVED',
    'PREPROCESSING',
    'OCR_PROCESSING',
    'EXTRACTING',
    'IDENTITY_CHECK',
    'RAG_SEARCH',
    'DECISION_PENDING',
    'AUTO_APPROVED',
    'MANUAL_REVIEW',
    'DOCS_REQUESTED',
    'FRAUD_FLAG',
    'REJECTED',
    'PAID'
);

CREATE TYPE doc_type AS ENUM ('form_100', 'id_document', 'receipt');

-- ── ПЛАТФОРМА (мультиарендность) ──────────────────────────────────
CREATE SCHEMA IF NOT EXISTS platform;

CREATE TABLE platform.tenants (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug        VARCHAR(50) UNIQUE NOT NULL,
    name        VARCHAR(200) NOT NULL,
    plan        VARCHAR(20) NOT NULL DEFAULT 'starter',
    status      VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Дефолтный тенант для первого клиента
INSERT INTO platform.tenants (id, slug, name, plan)
VALUES ('00000000-0000-0000-0000-000000000001', 'default', 'Default Tenant', 'enterprise');

CREATE TABLE platform.tenant_configs (
    tenant_id   UUID REFERENCES platform.tenants(id),
    key         VARCHAR(100) NOT NULL,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_by  UUID,
    PRIMARY KEY (tenant_id, key)
);

CREATE TABLE platform.api_keys (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID REFERENCES platform.tenants(id),
    key_hash        VARCHAR(64) UNIQUE NOT NULL,  -- SHA-256 от ключа
    name            VARCHAR(100),
    environment     VARCHAR(20) DEFAULT 'production',
    scopes          TEXT[] DEFAULT ARRAY['claims:write', 'claims:read'],
    rate_limit_rpm  INT DEFAULT 60,
    last_used_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE platform.usage_events (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID REFERENCES platform.tenants(id),
    event_type  VARCHAR(50) NOT NULL,  -- claim_auto_approved, claim_manual, etc.
    quantity    INT DEFAULT 1,
    metadata    JSONB,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── КОНТРАКТЫ ─────────────────────────────────────────────────────

CREATE TABLE contract_versions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL,
    policy_number   VARCHAR(50) NOT NULL,
    version_id      VARCHAR(20) NOT NULL,
    content_hash    VARCHAR(64),          -- SHA-256 для проверки актуальности
    valid_from      DATE NOT NULL,
    valid_to        DATE,                 -- NULL = текущая версия
    pdf_path        TEXT NOT NULL,        -- путь в storage
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (tenant_id, policy_number, version_id)
);

CREATE INDEX idx_contract_versions_lookup
    ON contract_versions (tenant_id, policy_number, valid_from DESC);

CREATE TABLE contract_chunks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL,
    policy_number   VARCHAR(50) NOT NULL,
    version_id      VARCHAR(20) NOT NULL,
    section_type    VARCHAR(50),   -- coverage_cases | exclusions | claim_conditions | limits | definitions | appeal_process | general
    title           TEXT,
    content         TEXT NOT NULL,
    key_terms       TEXT[],
    embedding       vector(1024),  -- multilingual-e5-large → 1024 измерения
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Индекс для векторного поиска (pgvector IVFFlat)
CREATE INDEX idx_contract_chunks_embedding
    ON contract_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Индекс для фильтрации по тенанту и полису
CREATE INDEX idx_contract_chunks_lookup
    ON contract_chunks (tenant_id, policy_number, version_id);

-- Полнотекстовый поиск — три языка одновременно
-- russian: стемминг для русского
-- english: стемминг для английского
-- simple:  без стемминга (покрывает грузинский)
CREATE INDEX idx_contract_chunks_fts
    ON contract_chunks USING gin (
        to_tsvector('russian', content) ||
        to_tsvector('english', content) ||
        to_tsvector('simple',  content)
    );

-- ── ЗАЯВКИ ────────────────────────────────────────────────────────

CREATE TABLE claims (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL,
    policy_number       VARCHAR(50),
    personal_id_number  VARCHAR(30),       -- личный номер из документов
    status              claim_status NOT NULL DEFAULT 'RECEIVED',
    submission_date     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_date          DATE,
    total_claimed       DECIMAL(10,2),
    total_approved      DECIMAL(10,2),
    deductible_applied  DECIMAL(10,2),
    final_payout        DECIMAL(10,2),
    decision_type       VARCHAR(30),       -- auto_approved | manual | rejected | fraud_flag
    overall_confidence  DECIMAL(4,3),
    routing_reason      TEXT,
    client_reference    VARCHAR(100),      -- внешний ID клиента
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_claims_tenant_status ON claims (tenant_id, status);
CREATE INDEX idx_claims_policy        ON claims (tenant_id, policy_number);
CREATE INDEX idx_claims_personal_id   ON claims (tenant_id, personal_id_number);
CREATE INDEX idx_claims_created_at    ON claims (tenant_id, created_at DESC);

CREATE TABLE claim_documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id        UUID REFERENCES claims(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL,
    doc_type        doc_type NOT NULL,
    storage_path    TEXT NOT NULL,         -- путь к оригиналу в storage
    preprocessed_path TEXT,               -- путь к обработанному файлу
    ocr_text        TEXT,
    ocr_confidence  DECIMAL(4,3),
    extracted_data  JSONB,
    quality_score   DECIMAL(4,3),
    quality_flags   TEXT[],               -- low_resolution | blurry | dark | cropped
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_claim_documents_claim ON claim_documents (claim_id);

-- Решения по диагнозам (МКБ-10)
CREATE TABLE diagnosis_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id            UUID REFERENCES claims(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL,
    icd10_code          VARCHAR(20),
    description         TEXT,
    is_covered          BOOLEAN,
    approved_amount     DECIMAL(10,2),
    rejection_reason    TEXT,
    contract_reference  TEXT,             -- "Статья 4.2, пункт 3"
    confidence          DECIMAL(4,3),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_diagnosis_decisions_claim ON diagnosis_decisions (claim_id);

-- Решения по строкам услуг
CREATE TABLE line_item_decisions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id        UUID REFERENCES claims(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL,
    description     TEXT,
    claimed_amount  DECIMAL(10,2),
    approved_amount DECIMAL(10,2),
    linked_icd10    VARCHAR(20),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── АУДИТ-ЛОГ (append-only) ───────────────────────────────────────

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    claim_id        UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    step            VARCHAR(50) NOT NULL,  -- intake | preprocessing | ocr | extraction | rag_search | decision | routing
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    input_data      JSONB,
    output_data     JSONB,
    confidence      JSONB,
    rag_chunks      TEXT[],               -- ID чанков использованных в RAG
    prompt_version  VARCHAR(20),
    model_version   VARCHAR(50),
    operator_id     UUID,
    override_reason TEXT,
    duration_ms     INT
);

-- Запрещаем UPDATE и DELETE — иммутабельный лог (требование регулятора)
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

CREATE INDEX idx_audit_log_claim   ON audit_log (claim_id, timestamp);
CREATE INDEX idx_audit_log_tenant  ON audit_log (tenant_id, timestamp);

-- ── РУЧНАЯ ПРОВЕРКА ───────────────────────────────────────────────

CREATE TABLE manual_review_queue (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id        UUID REFERENCES claims(id),
    tenant_id       UUID NOT NULL,
    priority        VARCHAR(20) DEFAULT 'normal',  -- urgent | high | normal
    reason          VARCHAR(100) NOT NULL,          -- low_confidence | high_amount | fraud_flag | system_error | etc.
    operator_note   TEXT,
    assigned_to     UUID,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_manual_review_queue_tenant ON manual_review_queue (tenant_id, priority, created_at);

CREATE TABLE manual_review_outcomes (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id            UUID REFERENCES claims(id),
    tenant_id           UUID NOT NULL,
    auto_decision       JSONB,    -- что решила система
    expert_decision     JSONB,    -- что решил эксперт
    discrepancy_reason  TEXT,
    operator_id         UUID NOT NULL,
    reviewed_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ── АПЕЛЛЯЦИИ ─────────────────────────────────────────────────────

CREATE TABLE appeals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id            UUID REFERENCES claims(id),
    tenant_id           UUID NOT NULL,
    status              VARCHAR(30) NOT NULL DEFAULT 'RECEIVED',  -- RECEIVED | IN_REVIEW | RESOLVED
    client_reason       TEXT NOT NULL,
    additional_docs     TEXT[],
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline_at         TIMESTAMPTZ,
    reviewed_by         UUID,
    expert_reasoning    TEXT,
    outcome             VARCHAR(20),       -- upheld | overturned | partial
    revised_payout      DECIMAL(10,2),
    resolved_at         TIMESTAMPTZ
);

-- ── СЧЁТЧИКИ ДЛЯ АНТИФРОДА ────────────────────────────────────────

CREATE TABLE claim_frequency (
    tenant_id       UUID NOT NULL,
    personal_id     VARCHAR(30) NOT NULL,
    period_start    DATE NOT NULL,         -- начало 30-дневного окна
    claim_count     INT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, personal_id, period_start)
);

-- ── КОММЕНТАРИИ ───────────────────────────────────────────────────
COMMENT ON TABLE audit_log IS 'Иммутабельный аудит-лог. Хранение 7 лет по требованию регулятора.';
COMMENT ON TABLE contract_chunks IS 'Чанки контрактов с эмбеддингами multilingual-e5-large (1024 измерения).';
COMMENT ON TABLE manual_review_outcomes IS 'Результаты ручной проверки — источник данных для петли обратной связи.';
