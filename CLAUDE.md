# CLAUDE.md — Insurance Claims Processing System

> Инструкция для Claude Code по построению системы автоматизированной обработки страховых требований (ДМС).
> Читай этот файл полностью перед началом любой задачи.

---

## Обзор проекта

Система является **middleware** между кор-системой (Lite GROUP) и личным кабинетом застрахованного.

```
Личный кабинет          Наша система (middleware)        Кор-система Lite GROUP
застрахованного    →    ┌─────────────────────────┐  →   /LiteApi/LiteServiceJSON
                        │  OCR → AI анализ        │
Форма 100 +             │  → решение              │  ←   Договор, риски, лимиты
фото ID +               │  → ClaimParsing_UNI     │
номер мед. карточки     └─────────────────────────┘
```

**Что делает система:**
1. Принимает от клиента: документы (форма 100, фото ID) + номер медицинской карточки (`PolicyNumber`)
2. Запрашивает из кор-системы: генеральный договор, список рисков, лимиты и остатки, справочник ICD10
3. Распознаёт документы через OCR → извлекает диагнозы, даты, суммы
4. Анализирует через Claude API: соответствие документов → условиям договора → доступным рискам
5. Вызывает `ClaimParsing_UNI` в кор-системе для создания убытка с прикреплёнными документами

**Языки документов:** Русский, Грузинский, Английский  
**Объём:** 50–300 заявок в сутки  
**SLA:** ≤ 5 минут на заявку (p90)  
**AI:** Google Vision API (OCR) + Claude API (extraction + decision)  
**Развёртывание:** Docker Compose (dev) / Kubernetes (prod)

---

## Структура проекта

```
insurance-claims/
├── CLAUDE.md                    ← этот файл
├── docker-compose.yml           ← dev окружение
├── docker-compose.prod.yml      ← prod окружение
├── .env.example                 ← шаблон переменных окружения
├── Makefile                     ← команды для разработки
│
├── services/
│   ├── api/                     ← FastAPI gateway (главный entrypoint)
│   ├── worker/                  ← Celery worker (фоновые задачи)
│   └── portal/                  ← React клиентский портал
│
├── core/                        ← общий код для всех сервисов
│   ├── config.py
│   ├── database.py
│   ├── models/
│   ├── schemas/
│   └── exceptions.py
│
├── layers/                      ← бизнес-логика по слоям
│   ├── intake/                  ← слой 1
│   ├── preprocessing/           ← слой 2
│   ├── ocr/                     ← слой 3
│   ├── extraction/              ← слой 4
│   │   ├── service.py           ← извлечение данных через Claude API
│   │   ├── classifier.py        ← regex-классификатор типов документов по OCR-тексту
│   │   └── training_exporter.py ← экспорт подтверждённых примеров для обучения ML
│   ├── rag/                     ← слой 5 (indexer.py + searcher.py + embedder.py)
│   ├── core_adapter/            ← слой 6
│   ├── decision/                ← слой 7
│   └── routing/                 ← слой 8
│
├── db/
│   └── migrations/              ← SQL миграции (применяются при старте)
│       ├── 001_initial.sql      ← начальная схема
│       └── 002_doc_type_training.sql ← поля для обучающей выборки классификатора
│
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/                ← тестовые документы
```

---

## Правила для Claude Code

### Общие правила

1. **Всегда читай этот файл целиком** перед началом задачи
2. **Строй послойно** — не переходи к следующему слою пока текущий не покрыт тестами
3. **Никогда не хардкоди** секреты, URL, пороговые значения — всё через `core/config.py`
4. **Каждая функция** работающая с внешним API обёрнута в retry-логику
5. **Каждое решение** системы пишет запись в `audit_log` — без исключений
6. **tenant_id** присутствует во всех запросах к БД — мультиарендность с первого дня
7. **Structured output** при вызовах Claude API — только tool use, никогда не парси свободный текст
8. **При любой неопределённости** в данных заявки — маршрут `manual_review`, не отказ
9. **Имена директорий слоёв** не содержат числовых префиксов (`intake/`, не `1_intake/`) —
   Python не может импортировать пакеты, начинающиеся с цифры
10. **ClaimParsing_UNI вызывается всегда** — даже при низкой уверенности AI.
    Поле `Comment` = полный AI-вердикт (решение + обоснование + уровень уверенности + флаги).
    Только технические ошибки (quality gate, policy not found, system error) останавливают отправку.

### Стиль кода

```python
# Используй async/await везде где есть I/O
# Claude API вызывать только через AsyncAnthropic (не Anthropic) + await
# Синхронные внешние клиенты (Google Vision, Document AI) — запускать через
#   loop = asyncio.get_running_loop(); await loop.run_in_executor(None, func, *args)
# Из синхронного контекста (Celery) запускать async через asyncio.run(), не get_event_loop()
# Типизируй всё через Pydantic v2
# Логируй через structlog (JSON-формат)
# Обработка ошибок — кастомные исключения из core/exceptions.py
# Тесты — pytest + pytest-asyncio
```

---

## Слой 0 — Инфраструктура (начни отсюда)

### docker-compose.yml

```yaml
version: "3.9"

services:

  # ── База данных ──────────────────────────────────────────────────
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB:       ${POSTGRES_DB:-claims}
      POSTGRES_USER:     ${POSTGRES_USER:-claims_user}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./db/migrations:/docker-entrypoint-initdb.d   # применяются при первом старте
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-claims_user}"]
      interval: 5s
      timeout: 5s
      retries: 10

  # ── Redis (очередь + кэш) ────────────────────────────────────────
  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "--no-auth-warning", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  # ── FastAPI (главный сервис) ─────────────────────────────────────
  api:
    build:
      context: .
      dockerfile: services/api/Dockerfile
    environment:
      - DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      # ADC: путь внутри контейнера — не меняй
      - GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json
      - STORAGE_BUCKET=${STORAGE_BUCKET}
      - ENVIRONMENT=development
    volumes:
      # Монтируем gcloud credentials с хост-машины (Windows)
      # Windows путь: %APPDATA%\gcloud → в docker-compose через переменную
      - ${APPDATA}/gcloud:/root/.config/gcloud:ro
      - .:/app                        # hot reload в dev
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    command: uvicorn services.api.main:app --host 0.0.0.0 --port 8000 --reload

  # ── Celery Worker (фоновые задачи) ───────────────────────────────
  worker:
    build:
      context: .
      dockerfile: services/worker/Dockerfile
    environment:
      - DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json
      - STORAGE_BUCKET=${STORAGE_BUCKET}
      - ENVIRONMENT=development
      - EMBEDDING_MODEL=intfloat/multilingual-e5-large
      - TRANSFORMERS_CACHE=/app/.cache/huggingface
    volumes:
      - ${APPDATA}/gcloud:/root/.config/gcloud:ro   # ADC credentials
      - .:/app
      - model_cache:/app/.cache/huggingface   # модель скачивается один раз
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    command: celery -A services.worker.celery_app worker --loglevel=info --concurrency=4

  # ── Celery Beat (планировщик) ────────────────────────────────────
  beat:
    build:
      context: .
      dockerfile: services/worker/Dockerfile
    environment:
      - DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
    volumes:
      - .:/app
    depends_on:
      - redis
    command: celery -A services.worker.celery_app beat --loglevel=info

  # ── React Portal ────────────────────────────────────────────────
  portal:
    build:
      context: services/portal
      dockerfile: Dockerfile
    environment:
      - VITE_API_URL=http://localhost:8000
    ports:
      - "3000:3000"
    volumes:
      - ./services/portal:/app
      - /app/node_modules
    command: npm run dev -- --host

  # ── LibreOffice (конвертация .docx → PDF) ────────────────────────
  libreoffice:
    image: linuxserver/libreoffice:latest
    environment:
      - PUID=1000
      - PGID=1000
    volumes:
      - ./tmp/conversions:/conversions
    ports:
      - "3001:3000"

volumes:
  postgres_data:
  redis_data:
  model_cache:    # кэш multilingual-e5-large (~1.1 GB, скачивается при первом старте)
```

### .env.example

```bash
# ── PostgreSQL ────────────────────────────────────────────────────
POSTGRES_DB=claims
POSTGRES_USER=claims_user
POSTGRES_PASSWORD=change_me_in_production

# ── Redis ─────────────────────────────────────────────────────────
REDIS_PASSWORD=change_me_in_production

# ── AI APIs ───────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...

# Google аутентификация через Application Default Credentials (ADC)
# JSON-ключ НЕ используется — аутентификация через gcloud CLI
# Файл создаётся командой: gcloud auth application-default login
# Путь на Windows: %APPDATA%\gcloud\application_default_credentials.json
# В docker-compose монтируется через: ${APPDATA}/gcloud:/root/.config/gcloud:ro
# Переменная GOOGLE_APPLICATION_CREDENTIALS уже прописана в docker-compose.yml

# ── Embeddings (локальная модель) ─────────────────────────────────
EMBEDDING_MODEL=intfloat/multilingual-e5-large   # RU + KA + EN
TRANSFORMERS_CACHE=/app/.cache/huggingface

# ── Storage ───────────────────────────────────────────────────────
STORAGE_BUCKET=claims-documents-dev
STORAGE_PROVIDER=gcs          # gcs | s3 | local (local только для dev)

# ── Кор-система Lite GROUP ────────────────────────────────────────
# Аутентификация: POST /api/User/authenticate → JWT токен (кэшируется)
CORE_API_BASE_URL=http://192.168.0.249:8077
CORE_API_USERNAME=webplatform
CORE_API_PASSWORD=839459ef0bc96d557fa5d1eda47a45bc
CORE_API_TIMEOUT=10
CORE_API_RETRY=3
# Токен кэшируется в Redis, обновляется автоматически при истечении

# ── Конфигурация системы ──────────────────────────────────────────
CONFIDENCE_AUTO_APPROVE=0.85
CONFIDENCE_MANUAL_REVIEW=0.80
CONFIDENCE_REQUEST_DOCS=0.70
MANUAL_REVIEW_AMOUNT_THRESHOLD=500.00
MANUAL_REVIEW_CURRENCY=GEL
DOCUMENT_RETENTION_MONTHS=84
AUDIT_LOG_RETENTION_MONTHS=84
APPEAL_WINDOW_DAYS=30
APPEAL_REVIEW_SLA_DAYS=5

# ── Антифрод ──────────────────────────────────────────────────────
FRAUD_FREQUENCY_WINDOW_DAYS=30
FRAUD_FREQUENCY_MAX_CLAIMS=10
FRAUD_AMOUNT_SIGMA_THRESHOLD=3.0

# ── Приложение ────────────────────────────────────────────────────
ENVIRONMENT=development       # development | production
SECRET_KEY=change_me_in_production
```

### Makefile

```makefile
.PHONY: up down logs migrate test lint

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

migrate:
	docker compose exec api python -m db.migrate

test:
	docker compose exec api pytest tests/ -v

lint:
	docker compose exec api ruff check . && mypy .

shell:
	docker compose exec api python

psql:
	docker compose exec postgres psql -U claims_user -d claims
```

---

## Слой 0.1 — Миграции базы данных

**Файл:** `db/migrations/001_initial.sql`

```sql
-- Включаем расширения
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- для fuzzy search

-- ── ТИПЫ ──────────────────────────────────────────────────────────

CREATE TYPE claim_status AS ENUM (
    'RECEIVED', 'PREPROCESSING', 'OCR_PROCESSING',
    'EXTRACTING', 'IDENTITY_CHECK', 'RAG_SEARCH',
    'DECISION_PENDING', 'AUTO_APPROVED', 'MANUAL_REVIEW',
    'DOCS_REQUESTED', 'FRAUD_FLAG', 'REJECTED', 'PAID'
);

CREATE TYPE doc_type AS ENUM ('form_100', 'id_document', 'receipt');

-- ── ПЛАТФОРМА (общие таблицы) ─────────────────────────────────────

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
    key_hash        VARCHAR(64) UNIQUE NOT NULL,
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
    event_type  VARCHAR(50) NOT NULL,
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
    content_hash    VARCHAR(64),
    valid_from      DATE NOT NULL,
    valid_to        DATE,
    pdf_path        TEXT NOT NULL,
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
    section_type    VARCHAR(50),
    title           TEXT,
    content         TEXT NOT NULL,
    key_terms       TEXT[],
    embedding       vector(1024),  -- multilingual-e5-large выдаёт 1024 измерения
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Индекс для векторного поиска
CREATE INDEX idx_contract_chunks_embedding
    ON contract_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Индекс для фильтрации по тенанту и полису
CREATE INDEX idx_contract_chunks_lookup
    ON contract_chunks (tenant_id, policy_number, version_id);

-- Индекс для полнотекстового поиска (BM25) — три языка
-- 'russian' — стемминг для русского
-- 'english' — стемминг для английского
-- 'simple'  — без стемминга, покрывает грузинский
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
    personal_id_number  VARCHAR(30),      -- личный номер из документов
    status              claim_status NOT NULL DEFAULT 'RECEIVED',
    submission_date     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_date          DATE,
    total_claimed       DECIMAL(10,2),
    total_approved      DECIMAL(10,2),
    deductible_applied  DECIMAL(10,2),
    final_payout        DECIMAL(10,2),
    decision_type       VARCHAR(30),
    overall_confidence  DECIMAL(4,3),
    routing_reason      TEXT,
    client_reference    VARCHAR(100),     -- внешний ID клиента
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_claims_tenant_status ON claims (tenant_id, status);
CREATE INDEX idx_claims_policy        ON claims (tenant_id, policy_number);
CREATE INDEX idx_claims_personal_id   ON claims (tenant_id, personal_id_number);

CREATE TABLE claim_documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id        UUID REFERENCES claims(id) ON DELETE CASCADE,
    tenant_id       UUID NOT NULL,
    doc_type        doc_type NOT NULL,
    storage_path    TEXT NOT NULL,
    ocr_text        TEXT,
    ocr_confidence  DECIMAL(4,3),
    extracted_data  JSONB,
    quality_score   DECIMAL(4,3),
    quality_flags   TEXT[],
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE diagnosis_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id            UUID REFERENCES claims(id) ON DELETE CASCADE,
    tenant_id           UUID NOT NULL,
    icd10_code          VARCHAR(20),
    description         TEXT,
    is_covered          BOOLEAN,
    approved_amount     DECIMAL(10,2),
    rejection_reason    TEXT,
    contract_reference  TEXT,
    confidence          DECIMAL(4,3),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

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

-- ── АУДИТ ─────────────────────────────────────────────────────────

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    claim_id        UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    step            VARCHAR(50) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    input_data      JSONB,
    output_data     JSONB,
    confidence      JSONB,
    rag_chunks      TEXT[],
    prompt_version  VARCHAR(20),
    model_version   VARCHAR(50),
    operator_id     UUID,
    override_reason TEXT,
    duration_ms     INT
);

-- append-only: запрещаем UPDATE и DELETE
CREATE RULE audit_log_no_update AS ON UPDATE TO audit_log DO INSTEAD NOTHING;
CREATE RULE audit_log_no_delete AS ON DELETE TO audit_log DO INSTEAD NOTHING;

CREATE INDEX idx_audit_log_claim  ON audit_log (claim_id, timestamp);
CREATE INDEX idx_audit_log_tenant ON audit_log (tenant_id, timestamp);

-- ── РУЧНАЯ ПРОВЕРКА ───────────────────────────────────────────────

CREATE TABLE manual_review_queue (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id        UUID REFERENCES claims(id),
    tenant_id       UUID NOT NULL,
    priority        VARCHAR(20) DEFAULT 'normal',  -- urgent | high | normal
    reason          VARCHAR(50) NOT NULL,
    operator_note   TEXT,
    assigned_to     UUID,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE manual_review_outcomes (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id            UUID REFERENCES claims(id),
    tenant_id           UUID NOT NULL,
    auto_decision       JSONB,
    expert_decision     JSONB,
    discrepancy_reason  TEXT,
    operator_id         UUID NOT NULL,
    reviewed_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ── АПЕЛЛЯЦИИ ─────────────────────────────────────────────────────

CREATE TABLE appeals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    claim_id            UUID REFERENCES claims(id),
    tenant_id           UUID NOT NULL,
    status              VARCHAR(30) NOT NULL DEFAULT 'RECEIVED',
    client_reason       TEXT NOT NULL,
    additional_docs     TEXT[],
    submitted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deadline_at         TIMESTAMPTZ,
    reviewed_by         UUID,
    expert_reasoning    TEXT,
    outcome             VARCHAR(20),
    revised_payout      DECIMAL(10,2),
    resolved_at         TIMESTAMPTZ
);

-- ── СЧЁТЧИКИ ДЛЯ АНТИФРОДА ────────────────────────────────────────

CREATE TABLE claim_frequency (
    tenant_id       UUID NOT NULL,
    personal_id     VARCHAR(30) NOT NULL,
    period_start    DATE NOT NULL,
    claim_count     INT DEFAULT 0,
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_id, personal_id, period_start)
);
```

---

## Слой 0.2 — Общий код (core/)

### core/config.py

```python
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── База данных ────────────────────────────────────────────────
    database_url: str
    redis_url: str

    # ── AI APIs ────────────────────────────────────────────────────
    anthropic_api_key: str
    # Google: аутентификация через ADC.
    # В docker-compose передаётся через GOOGLE_APPLICATION_CREDENTIALS.
    # В коде google-cloud библиотеки подхватывают ADC автоматически — не читать вручную.

    # ── Storage ────────────────────────────────────────────────────
    storage_bucket: str
    storage_provider: str = "local"  # local | gcs | s3  (local только для dev)

    # ── Кор-система Lite GROUP ─────────────────────────────────────
    # JWT токен получается через /api/User/authenticate, кэшируется в Redis
    # При 401 — автоматическое обновление токена без остановки обработки
    core_api_base_url: str = "http://192.168.0.249:8077"
    core_api_username: str = "webplatform"
    core_api_password: str = ""            # только через .env, не хардкодить
    core_api_timeout: int = 10
    core_api_retry: int = 3

    # ── Пороги принятия решений ────────────────────────────────────
    confidence_auto_approve: float = 0.85
    confidence_manual_review: float = 0.80
    confidence_request_docs: float = 0.70
    manual_review_amount_threshold: float = 500.00
    manual_review_currency: str = "GEL"

    # ── Хранение данных ────────────────────────────────────────────
    document_retention_months: int = 84
    audit_log_retention_months: int = 84

    # ── Апелляции ──────────────────────────────────────────────────
    appeal_window_days: int = 30
    appeal_review_sla_days: int = 5

    # ── Антифрод ───────────────────────────────────────────────────
    fraud_frequency_window_days: int = 30
    fraud_frequency_max_claims: int = 10
    fraud_amount_sigma_threshold: float = 3.0

    # ── Приложение ─────────────────────────────────────────────────
    environment: str = "development"
    secret_key: str = "change_me"

    # ── Эмбеддинги (локальная модель) ─────────────────────────────
    embedding_model: str = "intfloat/multilingual-e5-large"
    transformers_cache: str = "/app/.cache/huggingface"

    # ── Claude API ─────────────────────────────────────────────────
    # Не менять без обновления prompts/ и записи в changelog
    claude_model: str = "claude-sonnet-4-20250514"
    claude_extraction_temperature: float = 0.0
    claude_decision_temperature: float = 0.1
    claude_extraction_max_tokens: int = 1000
    claude_decision_max_tokens: int = 2000
    claude_chunking_max_tokens: int = 4096

    # ── OCR ────────────────────────────────────────────────────────
    ocr_max_retries: int = 3
    ocr_min_confidence: float = 0.70
    ocr_language_hints: list[str] = ["ru", "ka", "en"]
    # Полный путь к процессору Document AI:
    # projects/{project_id}/locations/{location}/processors/{processor_id}
    gcp_document_ai_processor: str = "projects/insurance-claims-dev/locations/us/processors/FORM_PARSER"

    # ── Quality Gate ───────────────────────────────────────────────
    quality_min_resolution_dpi: int = 150
    quality_max_blur_score: float = 100.0
    quality_min_brightness: float = 40.0
    quality_max_brightness: float = 220.0
    quality_max_skew_angle_deg: float = 45.0

    # ── RAG ────────────────────────────────────────────────────────
    rag_top_k: int = 5
    rag_rrf_k: int = 60   # константа Reciprocal Rank Fusion

    # ВАЖНО: extra="ignore" обязателен — .env содержит POSTGRES_DB, REDIS_PASSWORD
    # и другие переменные для docker-compose, которых нет в Settings
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Возвращает singleton настроек (кэшируется)."""
    return Settings()
```

### core/exceptions.py

```python
class ClaimsBaseError(Exception):
    """Базовый класс ошибок системы"""

class PolicyNotFoundError(ClaimsBaseError):
    """Полис не найден в кор-системе"""

class PolicyInactiveError(ClaimsBaseError):
    """Полис существует но неактивен"""

class DocumentQualityError(ClaimsBaseError):
    """Документ не прошёл quality gate"""
    def __init__(self, reason: str, detail: str):
        self.reason = reason    # low_resolution | blurry | dark | cropped
        self.detail = detail    # сообщение для клиента

class OCRFailedError(ClaimsBaseError):
    """OCR завершился с ошибкой"""

class ExtractionFailedError(ClaimsBaseError):
    """Claude не смог извлечь данные"""

class CoreAPIUnavailableError(ClaimsBaseError):
    """Кор-система недоступна после всех retry"""

class ContractNotIndexedError(ClaimsBaseError):
    """Контракт не проиндексирован"""

class AuditLogError(ClaimsBaseError):
    """Не удалось записать аудит-лог — критическая ошибка"""
```

---

## Слой 1 — Intake Service

**Файл:** `layers/intake/service.py`

**Задача:** принять документы, проверить форматы, сохранить оригиналы, создать запись заявки, поставить задачу в очередь.

```python
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "application/pdf"
}
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB

DOC_TYPE_HINTS = {
    # Ключевые слова в имени файла → тип документа
    "form_100":    ["form100", "form-100", "форма", "направление"],
    "id_document": ["passport", "id", "паспорт", "удостоверение"],
    "receipt":     ["receipt", "check", "чек", "квитанция"],
}

async def receive_claim(
    tenant_id: UUID,
    policy_number: str,           # номер медицинской карточки — обязательный параметр
    files: list[UploadFile],
    client_reference: str | None,
    db: AsyncSession,
    storage: StorageClient,
) -> Claim:
    """
    1. Валидация policy_number (непустой, формат)
    2. Валидация каждого файла (формат, размер)
    3. Определение типа документа
    4. Сохранение оригиналов в storage (зашифрованные)
    5. Создание записи Claim в БД (policy_number сохраняется сразу)
    6. Создание записей ClaimDocument
    7. Постановка задачи process_claim в Celery
    8. Запись в audit_log: step=intake
    9. Возврат claim_id клиенту
    """
```

**API endpoint:** `POST /v1/claims`  
**Тело запроса:** `multipart/form-data` — поле `policy_number` (string) + файлы  
**Ответ:** `{ "claim_id": "...", "status": "RECEIVED", "estimated_completion_sec": 300 }`

---

## Слой 2 — Preprocessing Service

**Файл:** `layers/preprocessing/service.py`

**Задача:** quality gate + подготовка изображений для OCR. Запускается как первый шаг Celery-задачи.

```python
QUALITY_THRESHOLDS = {
    "min_resolution_dpi":  150,
    "max_blur_score":      100,   # Laplacian variance — ниже = хуже
    "min_brightness":      40,
    "max_brightness":      220,
    "max_skew_angle_deg":  45,
}

QUALITY_ERROR_MESSAGES = {
    "low_resolution": "Разрешение слишком низкое. Сфотографируйте документ с расстояния 20–30 см.",
    "blurry":         "Изображение размытое. Удерживайте камеру неподвижно при съёмке.",
    "dark":           "Изображение слишком тёмное. Обеспечьте хорошее освещение.",
    "bright":         "Изображение пересвечено. Избегайте прямого света на документ.",
    "cropped":        "Текст обрезан по краям. Убедитесь, что весь документ помещается в кадр.",
}

async def preprocess_document(
    doc: ClaimDocument,
    storage: StorageClient,
) -> PreprocessedDocument:
    """
    1. Загрузить оригинал из storage
    2. Если PDF — конвертировать страницы в изображения
    3. Если DOCX — отправить в LibreOffice → PDF → изображения
    4. Для каждого изображения:
       a. Проверить разрешение (PIL / OpenCV)
       b. Проверить размытость (cv2.Laplacian variance)
       c. Проверить яркость (np.mean)
       d. Определить угол наклона (deskew)
       e. Если quality_score < порога → DocumentQualityError с причиной
    5. Применить коррекции: deskew, denoise, contrast enhancement
    6. Сохранить обработанные изображения в storage
    7. Вернуть список путей к обработанным изображениям
    8. audit_log: step=preprocessing, quality_scores={...}
    """

# Зависимости:
# pip install opencv-python-headless pillow numpy
```

**Важно:** если хотя бы один документ не прошёл quality gate — Celery-задача немедленно останавливается, статус заявки → `DOCS_REQUESTED`, клиент получает уведомление с конкретной причиной.

---

## Слой 3 — OCR Service

**Файл:** `layers/ocr/service.py`

**Задача:** распознать текст через Google Vision API. Параллельно для всех документов заявки.

```python
OCR_STRATEGIES = {
    "form_100":    "document_ai_form_parser",
    "id_document": "vision_text_detection",
    "receipt":     "document_ai_form_parser",
}

LANGUAGE_HINTS = ["ru", "ka", "en"]  # Russian + Georgian + English

async def ocr_document(
    doc: ClaimDocument,
    preprocessed_path: str,
    storage: StorageClient,
) -> OCRResult:
    """
    1. Загрузить обработанное изображение
    2. Выбрать стратегию по типу документа
    3. Вызвать Google Vision API:
       - document_ai_form_parser: используй Document AI Form Parser processor
       - vision_text_detection: используй Vision API annotateImage с DOCUMENT_TEXT_DETECTION
    4. Передать language_hints=["ru", "ka", "en"]
    5. Для каждого блока текста сохранить confidence_score
    6. Если средний confidence < 0.70 → пометить весь документ флагом low_confidence
    7. Retry-логика: MAX_RETRIES=3, backoff=[1, 3, 10] секунд
    8. Fallback при недоступности API: raise OCRFailedError → route_to_manual
    9. audit_log: step=ocr, avg_confidence=..., blocks_count=...
    """

async def ocr_all_documents(
    documents: list[ClaimDocument],
    storage: StorageClient,
) -> list[OCRResult]:
    """Параллельный OCR всех документов через asyncio.gather"""
    return await asyncio.gather(*[
        ocr_document(doc, storage) for doc in documents
    ])

# Зависимости:
# pip install google-cloud-vision google-cloud-documentai
```

---

## Слой 4 — Extraction Service

**Файлы:** `layers/extraction/service.py`, `layers/extraction/classifier.py`, `layers/extraction/training_exporter.py`

**Задача:** переклассифицировать типы документов по содержимому → извлечь структурированные данные через Claude API. Строго через tool use — никогда не парси свободный текст.

### Классификатор типов документов (classifier.py)

Вызывается **первым шагом** `extract_claim_data()`, до передачи текста в Claude.

**Проблема:** Layer 1 определяет тип документа по имени файла (`filename_hint`) — ненадёжно, т.к. клиент может назвать файл произвольно (`scan001.jpg`).

**Решение:** после OCR анализируем содержимое текста regex-паттернами на RU + KA + EN:

```python
# layers/extraction/classifier.py

MIN_MATCHES = 2  # минимум совпадений для переклассификации

CONTENT_PATTERNS = {
    DocType.FORM_100:    [...],  # мкб-10, диагноз, სამედიცინო, icd-10, diagnosis ...
    DocType.ID_DOCUMENT: [...],  # личный номер, \b\d{11}\b, პირადი ნომერი, passport ...
    DocType.RECEIPT:     [...],  # итого, к оплате, სულ, \d+GEL, invoice ...
}

def classify_by_ocr_text(text: str, current_type: DocType) -> ClassificationResult:
    """Подсчитать совпадения для каждого типа. Победитель с >= MIN_MATCHES совпадений."""

async def reclassify_documents(ocr_results, db, claim_id, tenant_id) -> list[OCRResult]:
    """
    Для каждого документа:
    - classify_by_ocr_text()
    - Если тип изменился → ClaimDocument.doc_type + doc_type_source='ocr_rules' в БД
    - Обновляет OCRResult.doc_type → Claude получит правильный лейбл в промпте
    """
```

**Важно:** `reclassify_documents()` вызывается до `_build_user_message()` — Claude видит уже исправленные метки документов.

### Сбор обучающих данных (training_exporter.py)

`ClaimDocument` содержит два новых поля:
- `doc_type_source` (`filename_hint` | `ocr_rules` | `operator`) — как определили тип
- `doc_type_confirmed` (`bool`) — верифицирован ли тип для обучения

**Когда подтверждается:**
- `AUTO_APPROVED` → routing/service.py автоматически ставит `confirmed=True` для всех документов заявки
- Оператор подтверждает в ручной проверке → `confirmed=True`, `source='operator'` (через Portal, Шаг 17)

**Экспорт:**
```powershell
# Статистика: сколько примеров накоплено
python -m layers.extraction.training_exporter --stats

# Экспорт в JSONL для обучения
python -m layers.extraction.training_exporter --output dataset.jsonl

# Только оператором подтверждённые (максимальное качество)
python -m layers.extraction.training_exporter --output dataset.jsonl --min-source operator
```

**Цель:** ~200 примеров на класс → обучить `multilingual-e5-large + LogisticRegression` → заменить regex на ML-классификатор (Шаг 20).

### Правило для classifier.py

```python
# classifier.py вызывается ВСЕГДА в extract_claim_data() как первый шаг
# Даже если имя файла говорит правильный тип — source обновляется до 'ocr_rules'
# Это гарантирует что все записи для обучения прошли контентный анализ
```

```python
EXTRACTION_TOOL = {
    "name": "extract_claim_data",
    "description": "Извлечь структурированные данные из OCR-текста страховых документов",
    "input_schema": {
        "type": "object",
        "properties": {
            "insured": {
                "type": "object",
                "properties": {
                    "full_name":      {"type": "string", "description": "Полное ФИО"},
                    "birth_date":     {"type": "string", "description": "ISO 8601: YYYY-MM-DD"},
                    "personal_id":    {"type": "string", "description": "Личный номер / ID"},
                    "policy_number":  {"type": "string", "description": "Номер страхового полиса"},
                },
                "required": ["full_name", "birth_date", "personal_id"]
            },
            "event": {
                "type": "object",
                "properties": {
                    "date":        {"type": "string", "description": "Дата события ISO 8601"},
                    "institution": {"type": "string"},
                    "diagnoses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "icd10_code":  {"type": "string"},
                                "description": {"type": "string"},
                            }
                        }
                    },
                    "line_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "amount":      {"type": "number"},
                            }
                        }
                    },
                    "total_claimed": {"type": "number"},
                },
                "required": ["date", "total_claimed"]
            },
            "extraction_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Список проблем: low_confidence_name, missing_date, etc."
            }
        },
        "required": ["insured", "event", "extraction_confidence"]
    }
}

EXTRACTION_SYSTEM_PROMPT = """
Ты — система извлечения данных из страховых документов.
Документы могут быть на русском, грузинском или английском языке.
Извлекай данные независимо от языка документа.
Нормализуй все данные в единый формат (даты ISO 8601, суммы float).

Правила:
- Извлекай только то что явно написано в тексте
- Нормализуй даты в формат YYYY-MM-DD
- Нормализуй суммы в float (без символов валюты)
- Если данные нечёткие или неоднозначные — добавь флаг и снизь confidence
- Личный номер: последовательность цифр 9-11 символов в ID-документах
- Коды МКБ-10: формат буква+цифры, например J06.9, Z00.0
- Если поле отсутствует — не придумывай, оставь null
"""

async def extract_claim_data(
    ocr_results: list[OCRResult],
    claim_id: UUID,
) -> ExtractionResult:
    """
    1. Объединить OCR-тексты всех документов с метками типа
    2. Вызвать Claude API с EXTRACTION_TOOL (tool_choice="required")
    3. Получить структурированный JSON
    4. Выполнить кросс-валидацию:
       - ФИО из form_100 vs id_document (fuzzy match ≥ 0.90)
       - birth_date точное совпадение
       - event_date ≤ submission_date
       - total_claimed из form_100 ≈ сумма line_items из receipt (±1%)
    5. При несоответствии → добавить флаг, снизить confidence
    6. audit_log: step=extraction, confidence=..., flags=[...]
    """

# Вызов Claude API:
# model: settings.claude_model
# temperature: settings.claude_extraction_temperature (0.0)
# max_tokens: 1000
# tool_choice: {"type": "tool", "name": "extract_claim_data"}
```

---

## Слой 5 — RAG Service

**Файл:** `layers/rag/`

Состоит из трёх файлов: `indexer.py` (онбординг контракта), `searcher.py` (поиск при заявке), `embedder.py` (обёртка над моделью).

### layers/rag/embedder.py

```python
# Модель загружается лениво при первом обращении (lazy loading).
# Поддерживает RU + KA + EN без дополнительной настройки.
from sentence_transformers import SentenceTransformer

_model: SentenceTransformer | None = None

def get_embedding(text: str, is_query: bool = False) -> list[float]:
    # multilingual-e5-large требует префикс:
    # "query:"   для поисковых запросов
    # "passage:" для текстов документов
    global _model
    if _model is None:
        _model = SentenceTransformer(settings.embedding_model)
    prefix = "query: " if is_query else "passage: "
    return _model.encode(prefix + text).tolist()
```

### layers/rag/indexer.py

```python
CHUNKING_SYSTEM_PROMPT = """
Раздели страховой договор на смысловые секции.
Договор может быть на русском, грузинском или английском языке.
Сохраняй текст каждой секции без изменений на языке оригинала.

Для каждой секции верни JSON-объект:
- section_type: coverage_cases | exclusions | claim_conditions |
                limits | definitions | appeal_process | general
- title: краткое название (до 10 слов)
- content: полный текст секции БЕЗ изменений
- key_terms: список ключевых терминов, кодов, названий

Правила:
- Не изменяй и не сокращай текст
- Каждый пункт об исключении — отдельный чанк
- Минимальный размер чанка: 2 предложения
- Максимальный размер чанка: 800 символов (если больше — раздели)
- Верни ТОЛЬКО JSON-массив, без пояснений
"""

async def index_contract(
    tenant_id: UUID,
    policy_number: str,
    pdf_path: str,              # путь в storage
    storage: StorageClient,
    db: AsyncSession,
) -> ContractVersion:
    """
    1. Скачать PDF из storage
    2. Если .docx — конвертировать через LibreOffice API
    3. Извлечь текст из PDF (pymupdf / pdfplumber)
    4. Вызвать Claude API для семантического chunking
       (temperature=0, max_tokens=4096)
    5. Для каждого чанка:
       a. Вызвать multilingual-e5-large для получения эмбеддинга
          model.encode(f"passage: {chunk.content}")
       b. Сохранить в contract_chunks с embedding (vector 1024)
    6. Сохранить версию в contract_versions с content_hash
    7. Вернуть ContractVersion
    """

async def get_contract_chunks_with_freshness_check(
    tenant_id: UUID,
    policy_number: str,
    event_date: date,
    query: str,
    db: AsyncSession,
    storage: StorageClient,
) -> list[ContractChunk]:
    """
    АЛГОРИТМ ПРОВЕРКИ АКТУАЛЬНОСТИ (см. раздел 5.4 instructions.md):

    1. Запросить метаданные контракта из кор-системы
       GET /policies/{policy_number}/contract/meta
    2. Получить последнюю версию из нашей БД
    3. Сравнить content_hash (или updated_at если хэш недоступен)
    4. Если совпадает → search_chunks() — быстро (0.1 сек)
    5. Если изменился → reindex_contract() with timeout=45 сек
       При timeout → route_to_manual_review с пояснением
    6. Выбрать version_id актуальный на event_date
    """
```

### layers/rag/searcher.py

```python
# Эмбеддинги через embedder.py (lazy loading)
from layers.rag.embedder import get_embedding

async def search_chunks(
    tenant_id: UUID,
    policy_number: str,
    version_id: str,
    query: str,
    db: AsyncSession,
    top_k: int = 5,
) -> list[ContractChunk]:
    """
    ГИБРИДНЫЙ ПОИСК (semantic + BM25 + Reciprocal Rank Fusion):

    1. Получить embedding запроса через multilingual-e5-large:
       get_embedding(query, is_query=True)
    2. Семантический поиск через pgvector:
       SELECT ... ORDER BY embedding <=> $query_vec LIMIT top_k*2
       WHERE tenant_id=$1 AND policy_number=$2 AND version_id=$3
    3. Полнотекстовый поиск BM25 — три языка одновременно:
       WHERE (
         to_tsvector('russian', content) @@ plainto_tsquery('russian', $query)
         OR to_tsvector('english', content) @@ plainto_tsquery('english', $query)
         OR to_tsvector('simple',  content) @@ plainto_tsquery('simple',  $query)
       )
       -- 'simple' покрывает грузинский (без стемминга)
    4. Reciprocal Rank Fusion: score = Σ 1/(k + rank_i), k=60
    5. Вернуть top_k объединённых результатов
    """

def build_rag_query(extraction: ExtractionResult) -> str:
    """Построить поисковый запрос из данных заявки"""
    diagnoses = " ".join([
        f"{d.icd10_code} {d.description}"
        for d in extraction.event.diagnoses
    ])
    items = " ".join([i.description for i in extraction.event.line_items])
    return f"страховое покрытие диагнозов: {diagnoses}. услуги: {items}"
```

---

## Слой 6 — Core System Adapter (Lite GROUP)

**Файл:** `layers/core_adapter/`

### Аутентификация Lite GROUP

Кор-система использует JWT-аутентификацию. Токен получается один раз и кэшируется в Redis.
При получении 401 — автоматическое обновление без остановки обработки заявки.

```
AUTH:  POST http://192.168.0.249:8077/api/User/authenticate
CALL:  POST http://192.168.0.249:8077/LiteApi/LiteServiceJSON
       Header: Authorization: <TOKEN>
       Body: { "METHODNAME": "...", "XML_DATA": { ... } }
```

```python
# layers/core_adapter/lite_group_auth.py

REDIS_TOKEN_KEY = "lite_group:jwt_token"
TOKEN_TTL_SECONDS = 3600  # 1 час, обновляется при 401

async def get_token(redis: Redis, settings: Settings) -> str:
    """
    Получить JWT токен — из кэша Redis или запросить новый.
    При 401 в любом методе — вызвать refresh_token() и повторить запрос.
    """
    cached = await redis.get(REDIS_TOKEN_KEY)
    if cached:
        return cached.decode()

    return await refresh_token(redis, settings)


async def refresh_token(redis: Redis, settings: Settings) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.core_api_base_url}/api/User/authenticate",
            json={
                "userName": settings.core_api_username,
                "passWord": settings.core_api_password,
            },
            timeout=settings.core_api_timeout,
        )
        resp.raise_for_status()
        token = resp.json()["token"]  # или "TOKEN" — уточнить по реальному ответу

    await redis.setex(REDIS_TOKEN_KEY, TOKEN_TTL_SECONDS, token)
    return token
```

### Методы кор-системы

```python
# layers/core_adapter/lite_group_adapter.py

BASE_URL  = settings.core_api_base_url
SERVICE_URL = f"{BASE_URL}/LiteApi/LiteServiceJSON"

async def call_method(
    method_name: str,
    xml_data: dict,
    redis: Redis,
    settings: Settings,
) -> dict:
    """
    Универсальный вызов любого метода кор-системы.
    Retry-логика: 3 попытки, backoff [1, 3, 10] сек.
    При 401 — refresh_token() и одна дополнительная попытка.
    """
    token = await get_token(redis, settings)

    for attempt in range(settings.core_api_retry):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    SERVICE_URL,
                    json={"METHODNAME": method_name, "XML_DATA": xml_data},
                    headers={"Authorization": token},
                    timeout=settings.core_api_timeout,
                )
                if resp.status_code == 401:
                    token = await refresh_token(redis, settings)
                    continue  # повторить с новым токеном
                resp.raise_for_status()
                return resp.json()

        except httpx.TimeoutException:
            if attempt == settings.core_api_retry - 1:
                raise CoreAPIUnavailableError(f"Timeout after {settings.core_api_retry} attempts")
            await asyncio.sleep([1, 3, 10][attempt])


# ── МЕТОД 1: Получить генеральный договор по номеру медкарточки ──────
async def get_contract(policy_number: str, redis: Redis, settings: Settings) -> dict:
    """
    METHODNAME: уточнить у владельца кор-системы
    Возвращает: текст генерального договора → используется для RAG индексации
    XML_DATA: { "PolicyNumber": policy_number }
    """
    result = await call_method(
        method_name="TODO_CONTRACT_METHOD",   # ← уточнить название метода
        xml_data={"PolicyNumber": policy_number},
        redis=redis,
        settings=settings,
    )
    return result


# ── МЕТОД 2: Получить риски, % покрытия, лимиты по медкарточке ────────
async def get_risks_and_limits(policy_number: str, redis: Redis, settings: Settings) -> dict:
    """
    METHODNAME: уточнить у владельца кор-системы
    Возвращает:
      - список рисков (RiskID, название, процент покрытия)
      - общий лимит и остаток
    XML_DATA: { "PolicyNumber": policy_number }
    """
    result = await call_method(
        method_name="TODO_RISKS_METHOD",      # ← уточнить название метода
        xml_data={"PolicyNumber": policy_number},
        redis=redis,
        settings=settings,
    )
    return result


# ── МЕТОД 3: Получить справочник диагнозов ICD10 ──────────────────────
async def get_icd10_list(redis: Redis, settings: Settings) -> list[dict]:
    """
    METHODNAME: уточнить у владельца кор-системы
    Справочник кэшируется в Redis на 24 часа (меняется редко).
    Возвращает: список { DiagnosID, code, name }
    """
    cached = await redis.get("core:icd10_list")
    if cached:
        import json
        return json.loads(cached)

    result = await call_method(
        method_name="TODO_ICD10_METHOD",      # ← уточнить название метода
        xml_data={},
        redis=redis,
        settings=settings,
    )
    await redis.setex("core:icd10_list", 86400, json.dumps(result))
    return result


# ── МЕТОД 4: Получить справочник провайдеров ─────────────────────────
async def get_providers(redis: Redis, settings: Settings) -> list[dict]:
    """
    METHODNAME: уточнить у владельца кор-системы
    Возвращает: список { PersID, Name, INN }
    PersID используется для ClaimParsing_UNI.
    Кэшируется в Redis на 24 часа.
    Маппинг на заявку: fuzzy-match названия учреждения из документов → PersID.
    XML_DATA: {} (без параметров — полный справочник)
    """
    cached = await redis.get("lite_group:providers")
    if cached:
        import json
        return json.loads(cached)

    result = await call_method(
        method_name="TODO_PROVIDERS_METHOD",   # ← уточнить название метода
        xml_data={},
        redis=redis,
        settings=settings,
    )
    await redis.setex("lite_group:providers", 86400, json.dumps(result))
    return result


# ── МЕТОД 5 (ФИНАЛЬНЫЙ): Создать убыток ClaimParsing_UNI ─────────────
async def submit_claim(
    policy_number: str,
    diagnosid: int,               # DiagnosID из справочника ICD10
    event_start_date: str,        # "YYYY-MM-DD"
    event_end_date: str,
    pers_id: int,                 # код провайдера (медучреждения)
    config_kind: int,             # вид направления
    risks_list: list[dict],       # [{ RiskID, FinalAmount, ServDate, serviceid, ServName }]
    file_fields: list[dict],      # [{ file_data: base64, file_name, fkind }]
    comment: str,
    redis: Redis,
    settings: Settings,
) -> SubmitClaimResult:
    """
    Финальный шаг — создать убыток в кор-системе.
    Вызывается ВСЕГДА, независимо от уровня уверенности AI.
    Comment содержит полный вердикт Claude: решение, обоснование, уровень уверенности, флаги.

    Документы передаются в base64 в поле file_data.
    fkind — тип файла (уточнить справочник у владельца кор-системы).

    Коды ответа:
      0 → успех
      1 → не заполнен номер медкарточки
      2 → не заполнен код диагноза
      3 → не заполнен код партнёра
      4 → не заполнен вид направления
      5 → полис не существует
      6 → не указан банковский счёт получателя
      7 → данные получателя пустые
      8 → системное сообщение
      9 → нет завершённого направления
    """
    result = await call_method(
        method_name="ClaimParsing_UNI",
        xml_data={
            "PolicyNumber":  policy_number,
            "DiagnosID":     diagnosid,
            "EventStartDate": event_start_date,
            "EventEndDate":   event_end_date,
            "PersID":        pers_id,
            "ConfigKind":    config_kind,
            "Comment":       comment,
            "RisksList":     risks_list,
            "file_fields":   file_fields,
        },
        redis=redis,
        settings=settings,
    )

    # Разбор ответа
    response_data = result.get("responseData", [{}])[0]
    status_code = int(response_data.get("status", -1))

    if status_code != 0:
        raise CoreAPISubmitError(
            status_code=status_code,
            message=response_data.get("StatusText", "Unknown error"),
        )

    return SubmitClaimResult(
        innum=response_data.get("Innum", ""),   # номер направления в кор-системе
        status=status_code,
        status_text=response_data.get("StatusText", ""),
    )
```

### Подготовка документов для ClaimParsing_UNI

```python
# layers/core_adapter/file_helpers.py

import base64

async def documents_to_file_fields(
    documents: list[ClaimDocument],
    storage: StorageClient,
) -> list[dict]:
    """
    Конвертировать документы заявки в формат file_fields для ClaimParsing_UNI.
    file_data — base64-encoded содержимое файла.
    fkind — тип файла (уточнить справочник: форма 100 / ID / чек).
    """
    FKIND_MAP = {
        "form_100":    1,   # ← уточнить реальные коды у владельца кор-системы
        "id_document": 2,
        "receipt":     3,
    }

    file_fields = []
    for doc in documents:
        raw_bytes = await storage.download(doc.storage_path)
        file_fields.append({
            "file_data": base64.b64encode(raw_bytes).decode(),
            "file_name": doc.storage_path.split("/")[-1],
            "fkind":     FKIND_MAP.get(doc.doc_type, 1),
        })

    return file_fields
```

### MockCoreAdapter для dev-окружения

```python
# layers/core_adapter/mock_adapter.py
# Используется когда CORE_API_BASE_URL=http://mock или ENVIRONMENT=development

class MockCoreAdapter:
    """Возвращает тестовые данные без реального обращения к кор-системе."""

    async def get_contract(self, policy_number):
        return {"contract_text": "Тестовый договор. Покрываются все риски."}

    async def get_risks_and_limits(self, policy_number):
        return {
            "risks": [
                {"RiskID": 1, "name": "Амбулаторное лечение", "coverage_pct": 80, "limit": 1000, "remaining": 750},
                {"RiskID": 2, "name": "Диагностика",          "coverage_pct": 100, "limit": 500,  "remaining": 500},
            ],
            "annual_limit": 5000,
            "remaining":    4250,
        }

    async def get_icd10_list(self):
        return [
            {"DiagnosID": 101, "code": "J06.9", "name": "Острая инфекция верхних дыхательных путей"},
            {"DiagnosID": 102, "code": "Z00.0", "name": "Общий медицинский осмотр"},
        ]

    async def submit_claim(self, **kwargs):
        return SubmitClaimResult(innum="MOCK-001", status=0, status_text="OK")
```

### Открытые вопросы по кор-системе (уточнить у владельца)

```
TODO_CONTRACT_METHOD   ← название метода для получения генерального договора
TODO_RISKS_METHOD      ← название метода для получения рисков и лимитов
TODO_ICD10_METHOD      ← название метода для получения справочника диагнозов
TODO_PROVIDERS_METHOD  ← название метода для получения справочника провайдеров
fkind коды             ← справочник типов файлов (form_100, id_document, receipt)
ConfigKind             ← справочник видов направлений
```

---

## Слой 7 — Decision Engine

**Файл:** `layers/decision/service.py`

**Задача:** принять решение по заявке. Два уровня: детерминированные правила → Claude API.

```python
DECISION_SYSTEM_PROMPT = """
Ты — эксперт по страховым выплатам ДМС. Принимай точные обоснованные решения.

ВАЖНЫЕ ПРАВИЛА:
1. Если в тексте договора нет ЯВНОГО указания что случай покрывается — верни requires_manual_review=true
2. Не принимай решение об ОТКАЗЕ без явного пункта договора
3. При любом сомнении — ручная проверка, не отказ
4. Цитируй КОНКРЕТНЫЙ пункт договора для каждого решения
5. Рассчитывай сумму с учётом франшизы и остатка лимита из financial_data
6. Отвечай строго в формате JSON-инструмента
"""

DECISION_TOOL = {
    "name": "make_claim_decision",
    "description": "Принять решение по страховой заявке",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnoses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "icd10_code":           {"type": "string"},
                        "is_covered":           {"type": "boolean"},
                        "approved_amount":      {"type": "number"},
                        "rejection_reason":     {"type": ["string", "null"]},
                        "contract_reference":   {"type": "string"},
                        "confidence":           {"type": "number"},
                    },
                    "required": ["icd10_code", "is_covered", "approved_amount", "confidence"]
                }
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description":     {"type": "string"},
                        "claimed_amount":  {"type": "number"},
                        "approved_amount": {"type": "number"},
                        "linked_icd10":    {"type": "string"},
                    }
                }
            },
            "total_approved":           {"type": "number"},
            "deductible_applied":       {"type": "number"},
            "final_payout":             {"type": "number"},
            "requires_manual_review":   {"type": "boolean"},
            "manual_review_reason":     {"type": ["string", "null"]},
            "overall_confidence":       {"type": "number"},
            "summary":                  {"type": "string", "description": "Краткое обоснование на русском языке"},
        },
        "required": [
            "diagnoses", "total_approved", "deductible_applied",
            "final_payout", "requires_manual_review", "overall_confidence"
        ]
    }
}

async def make_decision(
    claim: Claim,
    extraction: ExtractionResult,
    limits: PolicyLimits,
    contract_chunks: list[ContractChunk],
    db: AsyncSession,
) -> ClaimDecision:
    """
    УРОВЕНЬ 1 — Детерминированные проверки (без AI):
      - Полис активен на дату события?
      - Заявка подана не позже (event_date + X дней)?
      - Все обязательные документы есть?
      - Остаток лимита > 0?
      → Провал → немедленный отказ без вызова Claude

    УРОВЕНЬ 2 — Claude API:
      Контекст = extraction данные + contract_chunks + limits
      tool_choice = "required"
      temperature = settings.claude_decision_temperature (0.1)

    АНТИФРОД-ПРОВЕРКИ (параллельно с уровнем 2):
      - duplicate_check: тот же personal_id + event_date + institution
      - frequency_check: количество заявок за FRAUD_FREQUENCY_WINDOW_DAYS
      - amount_anomaly: сумма > mean + FRAUD_AMOUNT_SIGMA_THRESHOLD * std

    audit_log: step=decision, rag_chunks=[chunk_ids], prompt_version=..., model_version=...
    """

def build_decision_prompt(
    extraction: ExtractionResult,
    limits: PolicyLimits,
    chunks: list[ContractChunk],
) -> str:
    """
    Собрать промпт из трёх частей:
    1. ## Данные заявки — JSON из extraction
    2. ## Финансовые данные — limits из кор-системы
    3. ## Релевантные пункты договора — текст чанков с section_type
    """
```

---

## Слой 8 — Routing Service

**Файл:** `layers/routing/service.py`

```python
async def route_claim(
    claim: Claim,
    decision: ClaimDecision,
    core_result: SubmitClaimResult,    # результат ClaimParsing_UNI (всегда есть)
    settings: Settings,
    db: AsyncSession,
    notifier: NotificationService,
) -> RoutingResult:
    """
    Роутинг выполняется ПОСЛЕ отправки ClaimParsing_UNI (не блокирует её).
    ClaimParsing_UNI всегда вызывается с Comment = AI-вердикт.
    Роутинг отвечает только за: статус заявки, внутреннюю очередь, уведомления.

    1. FRAUD_FLAG:
       Условие: fraud_flags непустой
       → статус FRAUD_FLAG
       → запись в manual_review_queue с priority=urgent
       → уведомление менеджеру безопасности
       (ClaimParsing_UNI уже вызван, Innum зафиксирован в Comment)

    2. ПРИНЯТО / РУЧНАЯ ПРОВЕРКА:
       Условие: core_result.status == 0
       → если overall_confidence ≥ CONFIDENCE_AUTO_APPROVE → статус AUTO_APPROVED
       → если overall_confidence < CONFIDENCE_AUTO_APPROVE → статус MANUAL_REVIEW
         + запись в manual_review_queue (оператор может проверить решение кор-системы)

    3. ОШИБКА КОР-СИСТЕМЫ:
       Условие: core_result.status != 0
       → статус по коду ошибки (POLICY_NOT_FOUND, DOCS_REQUESTED и т.д.)
       → уведомление клиенту с причиной

    Во всех случаях:
    → уведомление клиенту о результате
    → запись usage_event (для биллинга) при status==0
    audit_log: step=routing, route=..., core_innum=..., core_status=...
    """
```

---

## Слой 9 — Celery Worker (оркестрация)

**Файл:** `services/worker/tasks.py`

```python
@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="process_claim",
)
async def process_claim(self, claim_id: str, tenant_id: str):
    """
    Главная задача — последовательно запускает все слои:

    try:
        claim = await db.get_claim(claim_id)
        await update_status(claim, "PREPROCESSING")

        # Шаг 2: Preprocessing
        preprocessed = await preprocess_all_documents(claim)

        # Шаг 3: OCR (параллельно по всем документам)
        await update_status(claim, "OCR_PROCESSING")
        ocr_results = await ocr_all_documents(preprocessed)

        # Шаг 4: Extraction
        await update_status(claim, "EXTRACTING")
        extraction = await extract_claim_data(ocr_results, claim_id)

        # Шаг 6: Три параллельных запроса к кор-системе
        await update_status(claim, "CORE_DATA_FETCH")
        contract_data, risks_limits, icd10_list = await asyncio.gather(
            core_adapter.get_contract(claim.policy_number),
            core_adapter.get_risks_and_limits(claim.policy_number),
            core_adapter.get_icd10_list(),
        )

        # Шаг 6a: Онбординг/обновление контракта в RAG если изменился
        await update_status(claim, "RAG_SEARCH")
        rag_query = build_rag_query(extraction)
        chunks = await get_contract_chunks_with_freshness_check(
            tenant_id, claim.policy_number, claim.event_date,
            contract_data, rag_query
        )

        # Шаг 7: Decision — Claude анализирует всё вместе
        await update_status(claim, "DECISION_PENDING")
        decision = await make_decision(
            claim=claim,
            extraction=extraction,
            risks_limits=risks_limits,   # ← из кор-системы (не из контракта)
            icd10_list=icd10_list,       # ← для маппинга DiagnosID
            contract_chunks=chunks,      # ← из RAG
        )

        # Шаг 8 (ФИНАЛЬНЫЙ): Отправка убытка в кор-систему
        # Вызывается ВСЕГДА — независимо от уровня уверенности и флагов manual_review.
        # Comment содержит полный вердикт: решение + обоснование + уверенность + флаги.
        await update_status(claim, "SUBMITTING_TO_CORE")
        file_fields = await documents_to_file_fields(claim.documents, storage)

        core_result = await core_adapter.submit_claim(
            policy_number=claim.policy_number,
            diagnosid=decision.diagnosid,               # маппинг из icd10_list
            event_start_date=str(claim.event_date),
            event_end_date=str(claim.event_date),
            pers_id=decision.pers_id,                   # код провайдера из документов
            config_kind=decision.config_kind,           # вид направления
            risks_list=decision.risks_list,             # [{ RiskID, FinalAmount, ... }]
            file_fields=file_fields,                    # документы в base64
            comment=decision.summary,                   # полный AI-вердикт для оператора кор-системы
        )

        # Шаг 9: Routing (внутренний — статус, очередь, уведомления)
        core_result = await route_claim(
            claim=claim,
            decision=decision,
            core_result=core_result,
            settings=settings,
            db=db,
            notifier=notifier,
        )

        # Сохранить Innum из кор-системы — это номер убытка для отслеживания
        await finalize_claim(
            claim_id=claim_id,
            core_innum=core_result.innum,
            approved_amount=decision.final_payout,
        )

    except DocumentQualityError as e:
        await request_better_documents(claim_id, e.reason, e.detail)

    except PolicyNotFoundError:
        await reject_claim(claim_id, reason="policy_not_found")

    except CoreAPISubmitError as e:
        # Ошибка при создании убытка в кор-системе
        logger.error("Core submit failed", claim_id=claim_id, status=e.status_code)
        await route_to_manual_review(claim_id, reason=f"core_error_{e.status_code}")

    except CoreAPIUnavailableError:
        await queue_for_retry(claim_id, delay_minutes=5)
        raise self.retry()

    except Exception as e:
        logger.error("Unexpected error", claim_id=claim_id, error=str(e))
        await route_to_manual_review(claim_id, reason="system_error")
```

---

## Аутентификация Google Cloud — Application Default Credentials (ADC)

> Система использует ADC вместо JSON-ключей. Это безопаснее и не требует файла credentials в репозитории.

### Как работает ADC в этом проекте

```
gcloud auth application-default login
    │
    ▼
Создаётся файл на хост-машине:
  Windows: C:\Users\<user>\AppData\Roaming\gcloud\application_default_credentials.json
    │
    ▼
docker-compose монтирует папку gcloud в контейнер:
  volumes:
    - ${APPDATA}/gcloud:/root/.config/gcloud:ro
    │
    ▼
Переменная окружения указывает путь внутри контейнера:
  GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json
    │
    ▼
google-cloud-vision и google-cloud-documentai
подхватывают credentials автоматически — никакого кода не нужно
```

### Первоначальная настройка (один раз)

```powershell
# 1. Установи Google Cloud SDK если ещё нет
# https://cloud.google.com/sdk/docs/install-sdk → GoogleCloudSDKInstaller.exe

# 2. Войди в аккаунт
gcloud auth login

# 3. Установи проект
gcloud config set project insurance-claims-dev

# 4. Создай Application Default Credentials
gcloud auth application-default login
# Откроется браузер → войди → разреши доступ

# 5. Проверь что файл создался
ls $env:APPDATA\gcloud\application_default_credentials.json
```

### Обновление credentials (когда истекут — раз в ~60 дней)

```powershell
gcloud auth application-default login
# Повтори вход — новый токен запишется в тот же файл
# Перезапуск Docker не нужен — файл монтируется напрямую
```

### Проверка что Google API доступен из контейнера

```powershell
# После docker compose up:
docker compose exec api python -c "
import google.auth
credentials, project = google.auth.default()
print('OK — project:', project)
print('Credentials type:', type(credentials).__name__)
"
# Ожидаемый вывод:
# OK — project: insurance-claims-dev
# Credentials type: UserCredentials
```

### Важно для продакшна

В продакшне (Kubernetes / Cloud Run) ADC работает через **Workload Identity** —  
контейнер автоматически получает credentials от GCP без каких-либо файлов.  
Код менять не нужно — google-cloud библиотеки используют тот же механизм ADC.

---

## Слой 10 — FastAPI (API Gateway)

**Файл:** `services/api/main.py`

```python
app = FastAPI(
    title="Insurance Claims API",
    version="1.0.0",
    docs_url="/docs" if settings.environment == "development" else None,
)

# Middleware
app.add_middleware(TenantMiddleware)      # извлекает tenant_id из API-ключа
app.add_middleware(RateLimitMiddleware)   # rate limiting по tenant
app.add_middleware(RequestLogMiddleware) # логирование всех запросов

# Роуты
app.include_router(claims_router,    prefix="/v1/claims")
app.include_router(contracts_router, prefix="/v1/contracts")
app.include_router(webhooks_router,  prefix="/v1/webhooks")
app.include_router(analytics_router, prefix="/v1/analytics")
app.include_router(internal_router,  prefix="/internal")  # для webhook от кор-системы
```

**Роуты:**

```
POST   /v1/claims                           Создать заявку
GET    /v1/claims/{claim_id}                Статус заявки
GET    /v1/claims/{claim_id}/audit          Аудит-лог заявки
POST   /v1/claims/{claim_id}/appeal        Апелляция

POST   /v1/contracts                        Загрузить контракт
GET    /v1/contracts/{policy_number}        Статус индексации

POST   /v1/webhooks                         Зарегистрировать webhook

GET    /v1/analytics/summary               Статистика
GET    /v1/analytics/accuracy              Метрики точности

POST   /internal/hooks/contract-updated    Webhook от кор-системы
POST   /internal/hooks/policy-status-changed
```

---

## Слой 11 — React Portal

**Файл:** `services/portal/`

```
services/portal/
├── src/
│   ├── pages/
│   │   ├── SubmitClaim.tsx      ← загрузка документов (drag & drop)
│   │   ├── ClaimStatus.tsx      ← статус в реальном времени
│   │   ├── ClaimHistory.tsx     ← история заявок
│   │   └── Appeal.tsx           ← форма апелляции
│   ├── components/
│   │   ├── DocumentUploader/    ← drag & drop, preview, quality hint
│   │   ├── StatusTracker/       ← прогресс обработки
│   │   └── DecisionCard/        ← результат с обоснованием
│   ├── hooks/
│   │   └── useClaimStatus.ts    ← polling каждые 5 сек
│   └── api/
│       └── client.ts            ← обёртка над fetch с авторизацией
```

**Ключевые требования к UI:**
- Drag & drop загрузка с предпросмотром документов
- Показывать клиенту какой тип документа нужен (форма 100, ID, чек)
- Прогресс-бар обработки с polling каждые 5 секунд
- При `DOCS_REQUESTED` — показать конкретную причину и кнопку повторной загрузки
- Результат: сумма к выплате + краткое обоснование + список диагнозов

---

## Dockerfile для каждого сервиса

### services/api/Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для OpenCV
RUN apt-get update && apt-get install -y \
    libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### requirements.txt

```
# Web
fastapi==0.111.0
uvicorn[standard]==0.30.0
python-multipart==0.0.9

# Database
sqlalchemy[asyncio]==2.0.30
asyncpg==0.29.0
pgvector==0.3.0
alembic==1.13.1

# Cache & Queue
celery[redis]==5.4.0
redis==5.0.4

# AI
anthropic==0.28.0
google-cloud-vision==3.7.2
google-cloud-documentai==2.25.0

# Embeddings (локальная модель — RU + KA + EN)
sentence-transformers==3.0.1   # multilingual-e5-large
torch==2.3.0                   # CPU-версия достаточна для inference
                               # при старте скачивается ~1.1 GB модель

# Image processing
opencv-python-headless==4.9.0.80
pillow==10.3.0
numpy==1.26.4
pymupdf==1.24.5          # PDF text extraction
pdfplumber==0.11.0       # fallback PDF extraction

# Config & validation
pydantic==2.7.1
pydantic-settings==2.3.0

# Logging
structlog==24.2.0

# HTTP client
httpx==0.27.0

# Security
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4

# Testing
pytest==8.2.0
pytest-asyncio==0.23.7
pytest-httpx==0.30.0
```

---

## Порядок разработки

```
✅ Шаг 1:  docker-compose up → все контейнеры подняты
✅ Шаг 2:  Миграции применены → схема БД проверена в psql
✅ Шаг 3:  core/config.py + core/exceptions.py реализованы
✅ Шаг 4:  Слой 1 (intake/) — реализован, unit тесты написаны
✅ Шаг 5:  Слой 2 (preprocessing/) — реализован, unit тесты написаны
✅ Шаг 6:  Слой 3 (ocr/) — реализован, unit тесты написаны
✅ Шаг 7:  Слой 4 (extraction/) — реализован, unit тесты написаны
✅ Шаг 8:  Слой 6 (core_adapter/) — MockCoreAdapter работает
✅ Шаг 9:  Слой 5 (rag/indexer.py) — реализован
✅ Шаг 10: Слой 5 (rag/searcher.py) — реализован, unit тесты написаны
✅ Шаг 11: Слой 7 (decision/) — реализован, unit тесты написаны
✅ Шаг 12: Слой 8 (routing/) — реализован, unit тесты написаны
✅ Шаг 13: Слой 9 (Celery worker) — оркестрация реализована
✅ Шаг 14: Слой 10 (FastAPI) — все роуты реализованы, Swagger на :8000/docs

✅ Шаг 15: Слои обновлены под новую архитектуру:
           - Слой 1 (intake): policy_number обязательный параметр
           - Слой 6 (core_adapter): JWT + MockCoreAdapter + get_providers()
           - Слой 7 (decision): summary = полный вердикт, find_pers_id() по справочнику
           - Слой 8 (routing): выполняется ПОСЛЕ ClaimParsing_UNI
           - Слой 9 (worker): ClaimParsing_UNI вызывается всегда

✅ Шаг 15а: Технический рефакторинг:
           - asyncio.run() вместо get_event_loop().run_until_complete() в tasks.py
           - AsyncAnthropic + await везде (extraction, decision, rag/indexer)
           - OCR-клиенты Google стали синхронными, запускаются через run_in_executor
           - gcp_document_ai_processor вынесен в settings (не хардкод)

✅ Шаг 15б: Исправление багов (приоритет: критические → средние → минорные):
           - #11 ИСПРАВЛЕН: index_contract_from_text() добавлена в rag/indexer.py
             (searcher.py импортировал несуществующую функцию → ImportError на первой заявке)
           - #6  ИСПРАВЛЕН: fraud_task = asyncio.create_task(check_fraud(...))
             (было: корутина создавалась но не запускалась — антифрод работал последовательно)
           - #7  ИСПРАВЛЕН: REQUIRED_DOC_TYPES проверяется в receive_claim()
             (было: заявка с одним файлом проходила без валидации комплектности)

✅ Шаг 15г: Классификатор типов документов по OCR-тексту:
           - layers/extraction/classifier.py — regex-классификатор (RU + KA + EN)
           - layers/extraction/training_exporter.py — экспорт обучающей выборки
           - db/migrations/002_doc_type_training.sql — поля doc_type_source, doc_type_confirmed
           - core/models/claim.py — ClaimDocument получил doc_type_source + doc_type_confirmed
           - layers/extraction/service.py — reclassify_documents() вызывается до Claude
           - layers/routing/service.py — AUTO_APPROVED помечает документы как confirmed=True
           Цель: накопить ~600 примеров → обучить ML-классификатор (Шаг 20)

   Шаг 15в: Оставшиеся баги (исправить перед Шагом 16):
           - #16 🟡 N+1 запросов в RAG searcher (_semantic_search, _keyword_search)
                    → заменить db.get() в цикле на один SELECT ... WHERE id IN (...)
           - #17 🟡 get_embedding() блокирует event loop (синхронный CPU-вызов в async)
                    → перенести в run_in_executor
           - #4  🟡 Retry-цикл rest_adapter._call(): 401 не уменьшает счётчик попыток,
                    last_error=None при исчерпании через 401
           - #3  🟡 Contract hash-mismatch: только log.warning, переиндексация не запущена
           - #10 🟢 CORS allow_origins=[] в production → портал заблокирован
           - #9  🟢 Сталый TODO-комментарий в core/schemas/decision.py

   Шаг 16: Уточнить у владельца кор-системы:
           - TODO_CONTRACT_METHOD, TODO_RISKS_METHOD, TODO_ICD10_METHOD
           - TODO_PROVIDERS_METHOD (список провайдеров: PersID, название, ИНН)
           - Коды fkind (типы файлов для ClaimParsing_UNI)
           - Справочник ConfigKind (виды направлений)
           → Убрать заглушки TODO в LiteGroupAdapter
           → Протестировать ClaimParsing_UNI на тестовом полисе

   Шаг 17: Слой 11 (Portal) — создать React приложение
           При реализации формы загрузки — добавить подтверждение типа документа оператором
           в UI ручной проверки → устанавливать doc_type_source='operator', doc_type_confirmed=True

   Шаг 18: Integration tests — покрыть полный flow end-to-end

   Шаг 19: Prometheus + Grafana мониторинг

   Шаг 20: ML-классификатор типов документов (после накопления данных)
           Условие старта: python -m layers.extraction.training_exporter --stats
                           показывает ≥200 примеров на каждый из 3 классов
           Реализация: multilingual-e5-large embeddings + LogisticRegression
           Интеграция: заменить regex в classifier.py на ML-модель
                       (интерфейс classify_by_ocr_text() остаётся прежним)
```

---

## Известные ограничения (TODO)

### Ожидают информации от владельца кор-системы

1. **Четыре метода кор-системы не уточнены** — названия не получены от владельца:
   ```
   TODO_CONTRACT_METHOD   → layers/core_adapter/rest_adapter.py
   TODO_RISKS_METHOD      → layers/core_adapter/rest_adapter.py
   TODO_ICD10_METHOD      → layers/core_adapter/rest_adapter.py
   TODO_PROVIDERS_METHOD  → layers/core_adapter/rest_adapter.py
   ```
   Логика разбора ответов уже написана, нужно только подставить реальные имена методов.

2. **fkind коды — заглушки** (`layers/core_adapter/file_helpers.py`)  
   `form_100=1, id_document=2, receipt=3` — уточнить реальные коды у владельца.

3. **ConfigKind не определён** (`layers/decision/service.py::build_risks_list`)  
   Берётся из первого сервиса первого риска. Уточнить справочник у владельца.

### Технические TODO (запланированы в Шаге 15в)

4. **N+1 запросов в RAG searcher** (`layers/rag/searcher.py::_semantic_search`, `_keyword_search`)  
   `db.get(ContractChunk, row.id)` в цикле → заменить на `SELECT ... WHERE id IN (...)`.

5. **get_embedding() блокирует event loop** (`layers/rag/searcher.py:216`)  
   Синхронный CPU-вызов sentence-transformers в async-контексте → перенести в `run_in_executor`.

6. **Retry-цикл `_call()` при 401** (`layers/core_adapter/rest_adapter.py`)  
   `continue` после token refresh не уменьшает счётчик попыток; `last_error=None` при исчерпании через 401.

7. **Contract reindex при hash-mismatch не реализован** (`layers/rag/searcher.py:301`)  
   При изменении контракта — только `log.warning`, переиндексация не запускается.

8. **CORS `allow_origins=[]` в production** (`services/api/main.py:36`)  
   React Portal заблокирован в production-окружении.

### Функциональные TODO

9. **Amount anomaly fraud detection — заглушка** (`layers/decision/service.py`)  
   Требует накопленной статистики по суммам заявок.

10. **React Portal не реализован** (`services/portal/`)  
    При реализации: добавить подтверждение типа документа оператором → `doc_type_source='operator'`.

11. **Integration tests отсутствуют** (`tests/integration/`)

12. **Оператор не может подтвердить тип документа** (ждёт Portal, Шаг 17)  
    `doc_type_source='operator'` и `doc_type_confirmed=True` сейчас устанавливаются только  
    при `AUTO_APPROVED`. Для MANUAL_REVIEW нужен UI в Portal.

13. **ML-классификатор не обучен** (ждёт накопления данных, Шаг 20)  
    Сейчас работает regex (`classifier.py`). После ~600 подтверждённых документов  
    запустить `training_exporter --stats` и начать Шаг 20.



```python
# ❌ Никогда так:
if confidence > 0.85:
if amount > 500:
model = "claude-sonnet-4-20250514"
url = "http://192.168.0.249:8077"

# ✅ Всегда так:
if confidence > settings.confidence_auto_approve:
if amount > settings.manual_review_amount_threshold:
model = settings.claude_model
url = settings.core_api_base_url
```

---

## Обязательные проверки перед merge

```powershell
# 1. Все тесты зелёные
docker compose exec api pytest tests/ -v

# 2. Нет хардкода (должно быть пусто кроме config.py и тестов)
docker compose exec api grep -r "0\.85\|0\.80\|500\.00\|claude-sonnet" layers/ services/ --include="*.py"

# 3. Каждый вызов внешнего API обёрнут в retry
docker compose exec api grep -rl "core_api\|vision\|anthropic" layers/ --include="*.py"
# Для каждого найденного файла убедись что есть retry-логика

# 4. Каждый шаг пишет в audit_log
docker compose exec api grep -rl "write_audit_entry\|audit_log" layers/ --include="*.py"
# Должны быть все 8 слоёв

# 5. tenant_id в каждом запросе к БД
docker compose exec api grep -r "\.execute\|\.scalar\|\.fetchall" layers/ --include="*.py"
# Каждый запрос должен содержать tenant_id в WHERE
```