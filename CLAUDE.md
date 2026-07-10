# CLAUDE.md — Insurance Claims Processing System

> Инструкция для Claude Code по построению системы автоматизированной обработки страховых требований (ДМС).
> Читай этот файл полностью перед началом любой задачи.

---

## Обзор проекта

Система является **middleware** между внешней медицинской системой (где пользователь подаёт заявку) и кор-системой страховщика (Lite GROUP).

```
Внешняя медицинская     Наша система (middleware)        Кор-система Lite GROUP
система (API-клиент) →  ┌─────────────────────────┐  →   LiteMed API
                        │  Download → OCR → AI    │       /api/Client/getpolicylist
JSON: policy_number +   │  → решение              │
URL-ссылки на файлы     │  → ClaimParsing_UNI     │  →   Claims API
(pre-signed URLs)       └─────────────────────────┘       /LiteApi/LiteServiceJSON
```

**Что делает система:**
1. Принимает от внешней системы: JSON с `policy_number` и массивом ссылок на документы (pre-signed URLs)
2. Скачивает документы из внешней системы в наш storage (worker, шаг 0)
3. Запрашивает из кор-системы: генеральный договор, список рисков, лимиты и остатки
4. Распознаёт документы через OCR → извлекает диагнозы, даты, суммы
5. Анализирует через LLM: соответствие документов → условиям договора → доступным рискам
6. Вызывает `ClaimParsing_UNI` в кор-системе для создания убытка с прикреплёнными документами

**Языки документов:** Русский, Грузинский, Английский  
**Объём:** 50–300 заявок в сутки  
**Приоритет:** качество обработки важнее скорости.  
**Время обработки:** целевое ≤ 15 минут (p90), жёсткого SLA нет.  
**AI:** Google Vision API (OCR) + LLM (Anthropic Claude **или** Google Gemini — через `LLM_PROVIDER`)  
**Развёртывание:** Docker Compose (dev) / Kubernetes (prod)

---

## Структура проекта

```
insurance-claims/
├── CLAUDE.md
├── docker-compose.yml
├── docker-compose.prod.yml
├── .env.example
├── Makefile
│
├── services/
│   ├── api/                     ← FastAPI gateway
│   │   ├── routers/
│   │   │   ├── claims.py
│   │   │   ├── contracts.py
│   │   │   ├── reviews.py
│   │   │   ├── analytics.py
│   │   │   ├── appeals.py
│   │   │   ├── auth.py          ← JWT-аутентификация портала (POST /auth/login)
│   │   │   ├── dashboard.py     ← Данные портала с трекингом затрат
│   │   │   ├── dead_letter.py   ← Dead Letter Queue (упавшие задачи)
│   │   │   ├── devtools.py      ← HTML форма ручной подачи (только dev)
│   │   │   └── webhooks.py      ← Webhook от кор-системы
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── entrypoint.sh        ← Загрузка справочников при старте
│   ├── worker/
│   │   ├── tasks.py             ← Оркестрация pipeline (process_claim)
│   │   ├── tasks_analytics.py   ← Петля обучения (calibrate_confidence)
│   │   └── celery_app.py
│   └── portal/                  ← React портал (клиентский веб-интерфейс)
│       ├── index.html
│       ├── vite.config.ts       ← Proxy → api:8000
│       └── src/
│           ├── pages/
│           │   ├── Login.tsx
│           │   ├── ClaimsList.tsx
│           │   └── ClaimDetail.tsx
│           ├── api/client.ts
│           └── types/index.ts
│
├── core/
│   ├── config.py
│   ├── database.py
│   ├── auth.py                  ← X-API-Key middleware, rate limit
│   ├── portal_auth.py           ← JWT для веб-портала (platform.users)
│   ├── llm_client.py            ← Провайдер-агностичный LLM-клиент (Anthropic + Gemini)
│   ├── logging.py
│   ├── storage.py
│   ├── tenant_config.py
│   ├── exceptions.py
│   ├── models/
│   │   ├── claim.py
│   │   ├── contract.py          ← ContractChunk, PositiveListProcedure
│   │   ├── icd10.py
│   │   ├── platform.py          ← Tenant, ApiKey, DeadLetterItem
│   │   └── user.py              ← PortalUser (platform.users)
│   └── schemas/
│       ├── claim.py
│       ├── core_api.py
│       └── decision.py
│
├── scripts/
│   ├── create_api_key.py        ← Генерация X-API-Key
│   └── create_user.py           ← Создание пользователя портала
│
├── alembic/
│   └── versions/
│       ├── 0001_initial_schema.py          ← SQL 001-007 (guard: пропуск если схема есть)
│       ├── 0002_learning_loop_and_metrics.py ← SQL 008-010 (идемпотентны)
│       ├── 0003_exclusion_rules.py         ← Таблица exclusion_rules (CARVEOUT)
│       ├── 0004_providers_taxpayer_varchar200.py ← VARCHAR(200) для TAXPAYER
│       ├── 0005_dead_letter_queue.py       ← platform.dead_letter_queue
│       └── 0006_portal_users.py            ← platform.users (JWT портала)
│
├── layers/
│   ├── intake/
│   ├── preprocessing/
│   ├── ocr/
│   ├── extraction/
│   │   ├── service.py           ← LLM-извлечение + кросс-валидация
│   │   ├── rule_extractor.py    ← Детерминированное извлечение (альтернатива LLM)
│   │   ├── classifier.py
│   │   └── training_exporter.py
│   ├── rag/
│   ├── core_adapter/
│   │   ├── rest_adapter.py
│   │   ├── risk_matcher.py
│   │   └── file_helpers.py
│   ├── decision/
│   │   ├── service.py           ← Decision engine (exclusion rules, CARVEOUT, positive list, три сигнала)
│   │   └── icd10_enricher.py
│   └── routing/
│
├── db/
│   ├── migrations/              ← SQL legacy бутстрап (001-010, заморожен)
│   ├── migration_utils.py
│   ├── loaders/
│   │   ├── load_icd10.py
│   │   ├── load_providers.py
│   │   └── load_exclusions.py   ← Загрузчик exclusion_rules из Excel
│   └── data/
│       ├── ICD10.csv            ← Справочник МКБ-10 (12 435 записей). В репозитории.
│       └── Cliniks.csv          ← Справочник провайдеров из Lite GROUP. В репозитории.
│                                   entrypoint.sh принимает оба имени: Cliniks.csv и providers.csv.
│
└── tests/
    ├── unit/                    ← 29 test-файлов
    └── integration/             ← 3 test-файла
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
7. **Structured output** при вызовах LLM — только tool use, никогда не парси свободный текст
8. **При любой неопределённости** в данных заявки — маршрут `manual_review`, не отказ
9. **ClaimParsing_UNI вызывается всегда** — даже при низкой уверенности AI.
   Только технические ошибки (quality gate, policy not found, system error) останавливают отправку.
   `core_api_skip_claims_submit=True` — временный обход для dev без Claims API.
10. **Comment для ClaimParsing_UNI** строится через `_build_structured_comment()` в `tasks.py`.
    Формат на грузинском: `სანდოობა: 85% | დიაგნოზები: E55.9 ✓ | თანხა: 128 GEL | მომსახურება: ... | <summary>`.
    Max 250 символов (`core_api_comment_max_length`).

### Стиль кода

```python
# async/await везде где есть I/O
# LLM — только через core/llm_client.py (не напрямую anthropic/google)
# Синхронные клиенты Google (Vision, DocAI) → run_in_executor
# Из Celery — asyncio.run()
# Типизация через Pydantic v2
# Логирование через structlog (JSON)
# ПД в логах запрещены (ФИО, личный номер, pre-signed URL)
# Ошибки — core/exceptions.py
# Тесты — pytest + pytest-asyncio
```

---

## Слой 0.2 — Общий код (core/)

### core/llm_client.py — провайдер-агностичный LLM-клиент

```
Переключение: LLM_PROVIDER=anthropic (дефолт) | gemini
Интерфейс BaseLLMClient:
  - call_tool(system, user, tool, *, temperature, max_tokens, ...) → LLMResult
  - call_text(system, user, *, temperature, max_tokens) → LLMResult
  - supports_thinking → bool (только AnthropicClient)

LLMResult:
  - tool_input: dict | None
  - text: str | None
  - input_tokens: int
  - output_tokens: int
  - reasoning: str | None  (только Anthropic extended thinking)

ВАЖНО: все вызовы LLM в extraction/service.py, decision/service.py и rag/indexer.py
идут через get_llm_client() — не импортируй AsyncAnthropic напрямую.
```

**Gemini-специфика:**
- `gemini-2.0-flash` значительно дешевле Claude Sonnet
- Не поддерживает extended thinking (CARVEOUT и Chain-of-Thought работают через text-only второй проход)
- MALFORMED_FUNCTION_CALL — транзиентная ошибка, ретраится автоматически (`_TOOL_CALL_RETRIES=2`)

### core/config.py (актуальные поля)

```python
# ── LLM провайдер ──────────────────────────────────────────────────
llm_provider: str = "anthropic"        # anthropic | gemini
anthropic_api_key: str = ""            # обязателен при llm_provider=anthropic
gemini_api_key: str = ""               # обязателен при llm_provider=gemini
gemini_model: str = "gemini-2.0-flash"

claude_model: str = "claude-sonnet-4-6"
claude_extraction_temperature: float = 0.0
claude_decision_temperature: float = 0.1
claude_extraction_max_tokens: int = 2000
claude_decision_max_tokens: int = 4000
claude_decision_max_tokens_thinking: int = 8000
claude_chunking_max_tokens: int = 4096

# ── Кор-система ────────────────────────────────────────────────────
core_api_base_url: str = "http://192.168.0.249:8077"
core_api_auth_url: str = ""            # пусто = core_api_base_url
core_api_claims_base_url: str = ""     # пусто = core_api_base_url; прод: https://192.168.0.250:7777
core_api_claims_username: str = ""
core_api_claims_password: str = ""
core_api_claims_verify_ssl: bool = False
core_api_skip_claims_submit: bool = False   # True → mock, Innum="SKIPPED" (только dev)
core_api_auth_scheme: str = "Bearer"
core_api_diagnosid_fallback: str = "N145"
core_api_pers_id_fallback: int = 914450     # PersID из Cliniks.csv если клиника не найдена
core_api_comment_max_length: int = 250      # MEDNOTE в PHEPOBJRISK (NVARCHAR)

# ── Extraction ─────────────────────────────────────────────────────
extraction_use_rules: bool = False          # детерминированный rule_extractor вместо LLM
extraction_rules_min_confidence: float = 0.60  # ниже → fallback на LLM
extraction_doc_type_low_confidence_threshold: float = 0.60  # documents[].doc_type_confidence

# ── Decision: три сигнала маршрутизации ────────────────────────────
# routing_signal = min(coverage_signal, data_score, amount_gate)
decision_amount_gate_high_pct: float = 0.30    # claim/limit > 30% → gate=0.60
decision_amount_gate_medium_pct: float = 0.10  # claim/limit > 10% → gate=0.80
decision_amount_gate_high_score: float = 0.60
decision_amount_gate_medium_score: float = 0.80
decision_coherence_confidence_penalty: float = 0.10

# ── Петля обучения ─────────────────────────────────────────────────
learning_calibration_significant_diff: float = 0.05
learning_calibration_factor_min: float = 0.5
learning_calibration_factor_max: float = 1.2

# ── Портал (JWT) ───────────────────────────────────────────────────
portal_jwt_expire_hours: int = 8

# ── Webhook ────────────────────────────────────────────────────────
webhook_secret_key: str = ""           # пусто = проверка выключена (dev)
webhook_signature_header: str = "x-webhook-signature"
```

### .env.example (дополнительные поля к существующим)

```bash
# ── LLM Provider ──────────────────────────────────────────────────
LLM_PROVIDER=anthropic           # anthropic | gemini
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=                  # заполнить при LLM_PROVIDER=gemini

# ── Claims API (если отличается от LiteMed) ───────────────────────
CORE_API_CLAIMS_BASE_URL=        # пусто = dev; прод: https://192.168.0.250:7777
CORE_API_CLAIMS_USERNAME=
CORE_API_CLAIMS_PASSWORD=
CORE_API_SKIP_CLAIMS_SUBMIT=false  # true только когда нет доступа к Claims API

# ── Webhook от кор-системы ────────────────────────────────────────
WEBHOOK_SECRET_KEY=              # generate: python -c "import secrets; print(secrets.token_urlsafe(32))"

# ── Extraction: rule-based ────────────────────────────────────────
EXTRACTION_USE_RULES=false       # true → детерминированный экстрактор (дешевле)
```

---

## Слой 4 — Extraction Service (обновлён)

**Два пути извлечения данных:**

```
extraction_use_rules = False (дефолт):
  OCR text → Claude/Gemini API → ExtractionResult
  Классификация типа документа — часть ЭТОГО ЖЕ tool-call (см. ниже), без
  отдельного LLM-вызова и без regex-прохода до вызова.

extraction_use_rules = True:
  OCR text → classify_by_ocr_text() (regex, единственный классификатор здесь)
           → rule_extractor.extract_by_rules() → ExtractionResult
  confidence < extraction_rules_min_confidence (0.60) → fallback → LLM
```

### Классификация типа документа (documents[])

`DocType`: `form_100` | `id_document` | `receipt` | `discharge_summary` |
`lab_result` | `prescription` | `other` (обязательный fallback — неуверенная
классификация, всегда ведёт к `manual_review`, никогда не оставляется молча).

В LLM-режиме (основной путь) Claude/Gemini классифицирует КАЖДЫЙ документ как
часть `EXTRACTION_TOOL["documents"]` — массив `{doc_index, doc_type,
doc_type_confidence}` по числу документов в пакете. `_build_user_message()`
нумерует документы нейтрально (`ДОКУМЕНТ #1, #2...`), не предполагая тип —
раньше regex-классификатор навязывал лейбл (`ФОРМА 100`/`ЧЕК`) ДО того, как
LLM видел текст, что могло закрепить неверную догадку.

`doc_index` (нейтральная нумерация документов в пакете) и `doc_source`
(семантическая нумерация ЧЕКОВ в `line_items`/`receipt_summaries` — `receipt_1`,
`receipt_2`, `form_100`) — это ДВЕ РАЗНЫЕ системы, не путать между собой.

**Форма 100 vs рецепт**: форма 100 часто содержит внутри себя назначенные
медикаменты — это НЕ делает документ рецептом. `prescription` — только для
самостоятельных аптечных бланков без признаков формы 100 (направление, диагноз,
врач, учреждение). Классификация — по доминирующему типу содержимого всего
документа, не по наличию отдельных полей.

Результат классификации пишется в `ClaimDocument.doc_type` +
`doc_type_source="llm"`. `doc_type=other` или `doc_type_confidence` ниже
`extraction_doc_type_low_confidence_threshold` → флаг `unclassified_document` /
`low_confidence_doc_type` → штраф `data_score` (см. Слой 7) → `manual_review`.

`layers/extraction/classifier.py` (regex по OCR-тексту, `MIN_MATCHES=2`) теперь
работает ТОЛЬКО в rule-based режиме (`extraction_use_rules=True`, LLM не
вызывается вообще). При недостаточном совпадении паттернов возвращает
`DocType.OTHER`, а не молча оставляет текущий тип.

`core/doc_type_hint.py` — Layer 1 (intake): грубая догадка по имени файла
(`doc_type_source="filename_hint"`) до OCR, переопределяется LLM/regex позже.

### layers/extraction/rule_extractor.py

Детерминированное извлечение без LLM:
- `personal_id`: regex 11-цифр (Georgian format)
- `icd10_code`: regex `[A-Z]\d{1,2}(\.\d{1,2})?`
- `amounts`: regex число + GEL/₾/ლ
- `dates`: regex DD.MM.YYYY / YYYY-MM-DD
- `full_name`: keyword-context + Georgian Unicode fallback
- `institution`: keyword-context (კლინიკა / клиника)
- `service_urgency`: keyword detection ("urg" / "emer" / "план")

**Аудит-лог** пишется в `extract_claim_data()` в `service.py` — оба пути попадают в одну запись с `prompt_version="extraction/rules/v1.0.0"` или `"extraction/llm/v1.x.x"`.

**Новое поле ExtractionResult:**
```python
service_urgency: str | None  # "urgent" | "diagnostic" | "planned" | None
```
Используется Decision Engine для CARVEOUT-условий.

---

## Слой 7 — Decision Engine (обновлён)

### Три сигнала маршрутизации

Финальный `routing_signal` = **min**(coverage_signal, data_score, amount_gate).

| Сигнал | Что измеряет | Источник |
|---|---|---|
| `coverage_signal` | Уверенность LLM что диагноз покрыт договором | `overall_confidence` из tool-call |
| `data_score` | Качество извлечённых данных | штрафы за флаги extraction |
| `amount_gate` | Размер заявки относительно годового лимита | `total_claimed / annual_limit` |

`data_score` штрафы (только по флагам extraction):
- `low_confidence_name`, `missing_date`, `amount_mismatch` → −0.10 каждый
- `cross_doc_mismatch.*` → −0.15
- `unclassified_document` (doc_type=other) → −0.20
- `low_confidence_doc_type` (классификация ниже порога) → −0.10
- Clamp [0, 1]

`amount_gate`:
- `claim/limit > 0.30` → 0.60 (крупная заявка → строгая проверка)
- `claim/limit > 0.10` → 0.80
- иначе → 1.0

Все три значения записываются в `audit_log.confidence`:
```json
{"coverage_signal": 0.87, "data_score": 0.85, "amount_gate": 0.80, "routing_signal": 0.80}
```

### Исключения по вордингу (exclusion_rules)

Детерминированные проверки уровня 1 на таблице `exclusion_rules`:

```
exclusion_rules:
  scope          — 'all' | 'family'
  description    — текст из вординга страховых условий
  icd10_codes    — массив кодов/диапазонов: ['E10', 'E11', 'E10-E14']
  carveout_conditions — условия исключения из исключения: ['urgent', 'diagnostic', 'first_test']
```

**CARVEOUT-логика:**
- Исключение без carveout → ОТКАЗ
- Исключение с carveout → проверяем `service_urgency` из extraction:
  - `service_urgency` совпадает с условием → **ПОКРЫТО** (carveout применяется)
  - не совпадает → ИСКЛЮЧЕНО
  - `service_urgency=null` → `manual_review` (не можем применить условие)

**Загрузка:**
```powershell
docker compose exec api python -m db.loaders.load_exclusions \
    --file db/data/exclusions.xlsx \
    --tenant-id 00000000-0000-0000-0000-000000000001
```

Excel формат: два листа (General / Family), колонки A = описание, B = коды МКБ-10, C = смежные коды.

### Positive List (positive_list_procedures)

Если в контракте есть явно перечисленные покрытые процедуры — `check_positive_list()` сверяет `line_items` с таблицей `positive_list_procedures`. Результат передаётся в `build_decision_prompt()` отдельной секцией `## Positive List`.

---

## Слой 9 — Celery Worker (обновлён)

### Идемпотентность ClaimParsing_UNI

Перед вызовом `submit_claim()` worker проверяет `audit_log` на наличие записи `step=core_submit` с тем же `claim_id`. Если запись есть — вызов пропускается, `innum` берётся из предыдущего результата. Это защищает от дублирования убытков при Celery-ретраях.

```python
# Лог-событие при повторном вызове:
log.info("core_submit_idempotency_hit", claim_id=..., innum=prior_submit.innum)
```

### Dead Letter Queue

После `max_retries=3` неудачных попыток задача не исчезает — она записывается в `platform.dead_letter_queue` (Celery on_failure signal). Операторы работают с DLQ через API:

```
GET  /v1/dead-letter              — список неразрешённых элементов
POST /v1/dead-letter/{id}/requeue — перезапустить задачу заново
POST /v1/dead-letter/{id}/dismiss — закрыть без перезапуска
```

### Comment для ClaimParsing_UNI

```python
# tasks.py → _build_structured_comment()
# Формат на грузинском (читает оператор кор-системы):
"სანდოობა: 85% | დიაგნოზები: E55.9 Грипп ✓ | თანხა: 128 GEL (80% / 160-დან) | მომსახურება: Консультация | <AI summary>"
# Максимум core_api_comment_max_length (250) символов — усекается с "…"
```

---

## Слой 10 — FastAPI (API Gateway, обновлён)

### Роуты

```
# Machine-to-machine (X-API-Key)
POST   /v1/claims                           Создать заявку
GET    /v1/claims/{claim_id}                Статус заявки
GET    /v1/claims/{claim_id}/audit          Аудит-лог заявки
POST   /v1/claims/{claim_id}/appeal         Апелляция
POST   /v1/contracts                        Загрузить контракт
GET    /v1/contracts/{policy_number}        Статус индексации
GET    /v1/reviews                          Открытые элементы очереди
POST   /v1/reviews/{claim_id}/outcome       Результат ручной проверки
GET    /v1/analytics/summary                Статистика
GET    /v1/analytics/accuracy               Метрики точности

# Dead Letter Queue (X-API-Key + scopes:admin)
GET    /v1/dead-letter                      Список упавших задач
POST   /v1/dead-letter/{id}/requeue         Перезапустить задачу
POST   /v1/dead-letter/{id}/dismiss         Закрыть без перезапуска

# Web Portal (JWT Bearer — platform.users)
POST   /auth/login                          Войти в портал
GET    /auth/me                             Текущий пользователь
GET    /v1/dashboard/claims                 История заявок с затратами
GET    /v1/dashboard/claims/{id}/cost       Детализация затрат по заявке
GET    /v1/dashboard/stats                  Агрегированная статистика

# Webhooks (HMAC-SHA256)
POST   /internal/hooks/contract-updated
POST   /internal/hooks/policy-status-changed

# Dev only (environment=development)
GET    /devtools                            HTML форма ручной подачи заявки
POST   /devtools/upload                     Загрузить файлы напрямую
```

### Dashboard: трекинг затрат (core/portal)

Стоимость отображается **с наценкой ×4** от себестоимости. Тарифы AI-токенов
берутся по **модели, которая реально обработала заявку** (`audit_log.model_version`)
через `settings.cost_for_model(model)` → таблица `_LLM_TOKEN_COST_PER_MTOK` в
`config.py`. Каждая заявка считается по цене своей модели независимо от активной
модели в `.env` — смешанная история (часть на 2.5, часть на 3.5) считается
корректно. Модель не в таблице → эвристика по имени (gemini/claude) →
per-provider fallback; null/старые записи → активная модель.

Себестоимость $/1M токенов (`_LLM_TOKEN_COST_PER_MTOK`, ×4 клиенту):
| Модель | Input | Output |
|---|---|---|
| gemini-2.5-flash | $0.30 | $2.50 |
| gemini-3.5-flash | $1.50 | $9.00 |
| gemini-2.0-flash | $0.10 | $0.40 |
| claude-sonnet-4-6 | $3.00 | $15.00 |
| OCR (Vision, per page) | $0.0015 | — |

`_CLIENT_MARKUP = 4.0` в `dashboard.py`. Данные — из `audit_log.output_data`
(`input_tokens`, `output_tokens`, `ocr_cost_usd`, `pages_count`).

**Ограничения:** для Gemini `input_tokens` включают кешированные токены по полной
цене (`cached_content_token_count` пока не учитывается — кеш-скидка $0.03/1M для 2.5
не отражается). Цены в таблице — ВЕРИФИЦИРОВАТЬ на ai.google.dev/gemini-api/docs/pricing.

---

## Слой 11 — React Portal ✅ (реализован)

**Аутентификация:** JWT Bearer, хранится в `localStorage`. Роли: `viewer` | `operator` | `admin`.

**Создание пользователя:**
```powershell
docker compose exec api python -m scripts.create_user `
    --tenant-slug default `
    --email user@example.com `
    --full-name "Operator Name" `
    --role operator
```

**Страницы:**

| Файл | Маршрут | Описание |
|---|---|---|
| `Login.tsx` | `/login` | Форма входа, JWT-аутентификация |
| `ClaimsList.tsx` | `/claims` | Список заявок + панель агрегированных затрат |
| `ClaimDetail.tsx` | `/claims/:id` | Детализация: Overview, Cost Summary, Processing Steps |

**Техническая платформа:**
- React 18 + Vite 5 + TypeScript
- Без внешних UI-библиотек (только inline styles)
- Шрифт Inter (Google Fonts), фоновый цвет `#f8fafc`, nav `#0f172a`
- Vite proxy: `/auth` и `/v1/dashboard` → `http://api:8000` (устраняет CORS)
- Hot reload через volume mount `./services/portal:/app`

**Статусы заявок** (отображаются на портале):
`AUTO_APPROVED` | `MANUAL_REVIEW` | `FRAUD_FLAG` | `REJECTED` | `PAID` | `DOCS_REQUESTED` | `RECEIVED` | `PREPROCESSING` | `OCR_PROCESSING` | `EXTRACTING` | `IDENTITY_CHECK` | `DECISION_PENDING`

---

## Слой 6 — Core System Adapter (без изменений архитектуры)

### Справочник провайдеров

Файл **`db/data/Cliniks.csv`** — справочник клиник из Lite GROUP (в репозитории).  
`entrypoint.sh` принимает оба имени: `Cliniks.csv` и `providers.csv` (первый найденный).

**Fallback**: если клиника из OCR не найдена fuzzy-matching (≥0.70) → `PersID = core_api_pers_id_fallback` (914450).

### Правила выбора риска (верифицированы)

```
ДОПУСТИМО:   RiskParentId=0 (корневой)
ДОПУСТИМО:   RiskParentId≠0 AND hasChild=1 (промежуточный)
ЗАПРЕЩЕНО:   RiskParentId≠0 AND hasChild=0 (чистый листовой — кор отвергает)

PARENT LIMIT RULE: если у выбранного риска LimitAmount=0 →
  remaining_limit берётся у родителя (RiskParentId → LimitAmountLeft родителя)

ConfigKind=2 (акт возмещения) — приоритет:
  a) Категория + маркер "თავისუფალი არჩევანი" + remaining > 0 → is_exact=True
  b) Без маркера → needs_manual_review
```

**`match_risks()` возвращает 3-tuple**: `(risks_list, needs_manual_review, selected_risk)`.
`selected_risk` (`RiskInfo | None`) — риск, реально выбранный детерминированным матчером
(тот же, что уйдёт в `RiskID` для ClaimParsing_UNI). Decision Engine (`layers/decision/service.py`,
после вызова `match_risks()`) использует **`selected_risk.coverage_pct`** как единственный
источник процента покрытия для пересчёта `final_payout` и построчных `approved_amount` —
а не то, что Claude мог предположить по своему усмотрению среди всех рисков из промпта.
Это важно: risk_matcher.py выбирает риск по своим детерминированным правилам (категория +
свободный выбор), которые могут не совпасть с риском, который Claude имел в виду при расчёте
суммы в DECISION_TOOL. Позиции `positive_list_applied=True` (POSITIVE LIST, 100% покрытие по
бизнес-правилу) из этого пересчёта исключены.

---

## Порядок разработки

```
✅ Шаг 1:  docker-compose up
✅ Шаг 2:  Миграции применены
✅ Шаг 3:  core/config.py + core/exceptions.py
✅ Шаг 4:  Слой 1 (intake/)
✅ Шаг 5:  Слой 2 (preprocessing/)
✅ Шаг 6:  Слой 3 (ocr/)
✅ Шаг 7:  Слой 4 (extraction/) — LLM + rule_extractor.py
✅ Шаг 8:  Слой 6 (core_adapter/) — MockCoreAdapter + реальный API
✅ Шаг 9:  Слой 5 (rag/indexer.py)
✅ Шаг 10: Слой 5 (rag/searcher.py)
✅ Шаг 11: Слой 7 (decision/) — базовый pipeline
✅ Шаг 12: Слой 8 (routing/)
✅ Шаг 13: Слой 9 (Celery worker)
✅ Шаг 14: Слой 10 (FastAPI) — все роуты
✅ Шаг 15: Интеграция кор-системы, URL-based intake, classifier, downloader
✅ Шаг 16: Реальный LiteMed REST API + risk_matcher + верификация на реальных данных
✅ Шаг 16а: Правила выбора риска (верифицированы 2026-06-14)
✅ Шаг 17: React Portal (Login + ClaimsList + ClaimDetail)
            JWT-аутентификация (platform.users, alembic 0006)
            Dashboard API с трекингом затрат и наценкой ×4
✅ Шаг 18: Integration tests (32 тест-файла)

✅ Шаги 21–28: Enterprise-качество решений:
  ✅ 21: Медицинская согласованность (coherence_flags)
  ✅ 22: Проверка исключений через ICD10-дерево
  ✅ 23: Суб-лимиты и периоды ожидания (АКТИВНЫ)
  ✅ 24: Каркас бенчмаркинга суммы (таблица + job, activation pending)
  ✅ 25: Кросс-документная согласованность
  ✅ 26: Chain-of-Thought + Extended Thinking (Anthropic only)
  ✅ 27: Feedback Loop — калибровка confidence (Celery Beat)
  ✅ 28: Stochastic QA Sampling (5% AUTO_APPROVED → manual_review)

✅ Шаги 29–30: Петля обучения замкнута, детальная аналитика расхождений

✅ Дополнительные фичи (реализованы после основного pipeline):
  ✅ LLM-агностичный клиент (core/llm_client.py) — Anthropic + Gemini
  ✅ Три сигнала маршрутизации (coverage_signal, data_score, amount_gate)
  ✅ Rule-based extraction (layers/extraction/rule_extractor.py)
  ✅ Exclusion rules с CARVEOUT (exclusion_rules таблица, alembic 0003)
  ✅ Positive List (positive_list_procedures, check_positive_list())
  ✅ Dead Letter Queue (alembic 0005, GET/POST /v1/dead-letter)
  ✅ Идемпотентность ClaimParsing_UNI (защита от дублей при retry)
  ✅ Структурированный Georgian comment для кор-системы
  ✅ Devtools (/devtools — HTML форма ручной подачи, только dev)
  ✅ Автоклассификация типа документа встроена в extraction tool-call
     (7 типов DocType, alembic 0007) — заменяет regex-предположение до LLM

   Шаг 19: Prometheus + Grafana мониторинг
   Шаг 20: ML-классификатор типов документов (после расширения на 7 классов
           нужно ~1400 примеров: 200/класс × 7, было ~600 при 3 классах)
           Условие: python -m layers.extraction.training_exporter --stats ≥200/класс

   Шаг 33: Бенчмаркинг суммы [каркас готов, activation pending]
           Условие: fraud_amount_benchmark_enabled=True + ≥30 заявок на icd10_prefix

   Шаг 34: ML-классификатор [ждёт ~1400 подтверждённых примеров, см. Шаг 20]
   Шаг 35: Fine-tuning для Extraction [ждёт 2000 примеров]
```

---

## Известные ограничения и TODO

### Технические

1. **CORS `allow_origins=[]` в production** (`services/api/main.py`)
   Заполнить конкретными доменами внешних систем.

2. **Whitelist доменов для default tenant не настроен**  
   В production worker отклонит скачивание файлов. Добавить запись:
   ```sql
   INSERT INTO platform.tenant_configs (tenant_id, key, value)
   VALUES ('00000000-0000-0000-0000-000000000001', 'allowed_download_hosts',
           '["medsystem.example.com"]');
   ```
   В dev пустой whitelist разрешён с предупреждением.

3. **`exclusions.xlsx` не загружен**  
   Без данных `exclusion_rules` пуст — CARVEOUT-логика молча пропускается.
   Загрузить:
   ```powershell
   docker compose exec api python -m db.loaders.load_exclusions `
       --file db/data/exclusions.xlsx `
       --tenant-id 00000000-0000-0000-0000-000000000001
   ```

### Функциональные (ждут данных или решений)

4. **Amount anomaly fraud detection — заглушка**  
   Требует накопленной статистики (Шаг 33). Активировать:
   `fraud_amount_benchmark_enabled=True` после 3+ месяцев.

5. **Текст договора не доставляется из кор-системы**  
   `getpolicylist` не возвращает `ClauseList`. Договоры загружаются вручную через `POST /v1/contracts`.

6. **Prometheus + Grafana не реализованы** (Шаг 19)

### Миграции схемы

Схемой управляет Alembic. `make migrate` = `alembic upgrade head`.

- **Свежая БД**: ревизии 0001→0007 применяются последовательно
- **Существующая БД** (до введения Alembic): 0001 видит таблицу `claims` и пропускается; 0002 идемпотентна; 0003–0007 используют `IF NOT EXISTS`
- **Новые изменения схемы — только через Alembic**: `alembic revision -m "..."`  
  Каталог `db/migrations/` заморожен (legacy бутстрап для initdb)
- **Расширение native enum** (прецедент — `0007_expand_doc_type.py`): Postgres
  запрещает использовать новое значение `ENUM` в той же транзакции, где оно
  добавлено — `ALTER TYPE ... ADD VALUE` должен идти в
  `with op.get_context().autocommit_block():`. `downgrade()` для таких ревизий
  не реализуется (`raise NotImplementedError`) — Postgres не умеет удалять
  значения enum без пересоздания типа.
  `entrypoint.sh` всегда прогоняет `alembic upgrade head` после старта Postgres
  (и для свежей БД из `docker-entrypoint-initdb.d`, и для существующей) — отдельный
  legacy-файл в `db/migrations/` для расширения enum не нужен.

---

## Антипаттерны

```python
# ❌ Никогда так:
import anthropic; client = anthropic.AsyncAnthropic()     # не напрямую
if confidence > 0.85:                                       # хардкод порога
model = "claude-sonnet-4-6"                                 # хардкод модели
url = "http://192.168.0.249:8077"                           # хардкод URL

# ✅ Всегда так:
from core.llm_client import get_llm_client
llm = get_llm_client()
if confidence > settings.confidence_auto_approve:
model = settings.claude_model
url = settings.core_api_base_url
```

---

## Обязательные проверки перед merge

```powershell
# 1. Все тесты зелёные
docker compose exec api pytest tests/ -v

# 2. Нет хардкода (пусто кроме config.py и тестов)
docker compose exec api grep -r "0\.85\|0\.80\|500\.00\|claude-sonnet\|192\.168" layers/ services/ --include="*.py"

# 3. Каждый вызов внешнего API обёрнут в retry
# 4. Каждый шаг пишет в audit_log
# 5. tenant_id в каждом запросе к БД
```
