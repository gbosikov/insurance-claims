"""
core/config.py — централизованная конфигурация системы.

Все пороговые значения, URL и параметры берутся ТОЛЬКО отсюда.
Никогда не хардкоди значения в бизнес-логике.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── База данных ────────────────────────────────────────────────
    database_url: str
    redis_url: str

    # ── AI APIs ────────────────────────────────────────────────────
    anthropic_api_key: str = ""  # обязателен при LLM_PROVIDER=anthropic (дефолт)

    # LLM_PROVIDER=gemini → переключиться на Google Gemini вместо Claude
    llm_provider: str = "anthropic"   # anthropic | gemini
    gemini_api_key: str = ""          # обязателен при LLM_PROVIDER=gemini
    gemini_model: str = "gemini-2.0-flash"  # extraction + decision + indexer

    # Google: аутентификация через ADC.
    # В docker-compose передаётся через GOOGLE_APPLICATION_CREDENTIALS.
    # В коде google-cloud библиотеки подхватывают ADC автоматически — не читать вручную.

    # ── Storage ────────────────────────────────────────────────────
    storage_bucket: str
    storage_provider: str = "local"  # local | gcs | s3  (local только для dev)

    # ── Кор-система Lite GROUP ─────────────────────────────────────
    # LiteMed API (данные клиента и полисов): /api/Client/getpolicylist
    # Аутентификация: POST /api/User/authenticate → Bearer-токен, кэшируется в Redis
    core_api_base_url: str = "http://192.168.0.249:8077"
    core_api_username: str = "webplatform"
    core_api_password: str = ""      # только через .env, не хардкодить
    core_api_timeout: int = 10
    core_api_retry: int = 3

    # Отдельный auth-сервер (прод: 10.0.204.10:1010; dev: пусто = core_api_base_url)
    core_api_auth_url: str = ""

    # Claims API (отдельный сервис Lite GROUP, другой порт, HTTPS):
    #   ТЕСТ: https://192.168.0.249:4443
    #   ПРОД: https://192.168.0.250:7777
    # Auth: POST {core_api_claims_base_url}/LiteApi/LiteAuthJSON  (своя аутентификация)
    # Data: POST {core_api_claims_base_url}/LiteApi/LiteServiceJSON  (ClaimParsing_UNI)
    # Пусто → fallback на core_api_base_url (для dev/mock окружений).
    core_api_claims_base_url: str = ""

    # Claims API может иметь отдельные учётные данные (отличные от LiteMed).
    # Пусто → использовать core_api_username / core_api_password (те же что для LiteMed).
    core_api_claims_username: str = ""
    core_api_claims_password: str = ""

    # Claims API использует самоподписанный SSL-сертификат (внутренняя сеть).
    # False — отключить проверку SSL (нужно для корпоративных серверов без CA).
    core_api_claims_verify_ssl: bool = False

    # Если True — пропускать ClaimParsing_UNI и записывать успешный mock-результат.
    # Использовать только в dev пока Claims API не настроен.
    # При True claim получает AUTO_APPROVED/MANUAL_REVIEW по AI-решению, Innum="SKIPPED".
    core_api_skip_claims_submit: bool = False

    # Схема Authorization-заголовка: "Bearer" или "" (сырой токен).
    # Документация getpolicylist показывает сырой GUID без префикса;
    # тестовый сервер ранее принимал Bearer. При 401 — переключить через .env.
    core_api_auth_scheme: str = "Bearer"

    # Отбираются ТОЛЬКО полисы этих продуктов (Policy.ProductName) —
    # имущественные/прочие полисы того же клиента игнорируются.
    core_api_medical_product_names: list[str] = ["სამედიცინო (ჯანმრთელობის) დაზღვევა"]

    # TypeID медицинского страхования в InsuranceTypeList (23 = სამედიცინო).
    # Риски берутся из этих типов; если ни один не найден — из всех (с warning).
    core_api_medical_type_ids: list[int] = [23]

    # Маркер в Objects.ObjectData: объект освобождён от периода ожидания
    core_api_waiting_period_exempt_marker: str = "არ ეკუთვნის მოცდის პერიოდი"

    # Fallback-значения для ClaimParsing_UNI если данные не найдены в документах
    core_api_diagnosid_fallback: str = "N145"  # МКБ-10 "неклассифицированный" если не найден в документах
    core_api_pers_id_fallback: int = 914450    # PersID клиники по умолчанию (Cliniks.csv)
    core_api_comment_max_length: int = 250     # MEDNOTE в PHEPOBJRISK ограничена (SQL Server NVARCHAR)

    # ── Пороги принятия решений ────────────────────────────────────
    confidence_auto_approve: float = 0.85
    confidence_manual_review: float = 0.80
    confidence_request_docs: float = 0.70
    manual_review_amount_threshold: float = 500.00
    manual_review_currency: str = "GEL"

    # ── Хранение данных ────────────────────────────────────────────
    document_retention_months: int = 84   # 7 лет
    audit_log_retention_months: int = 84  # 7 лет (требование регулятора)

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

    # ── API-аутентификация (platform.api_keys) ─────────────────────
    api_key_header: str = "X-API-Key"
    # Лимит запросов/мин если у ключа не задан rate_limit_rpm
    api_rate_limit_default_rpm: int = 60

    # ── Webhook Security ────────────────────────────────────────────
    # Секретный ключ для подписи webhook от CoreAPI
    # Используется для HMAC-SHA256 верификации
    # Должен быть сгенерирован случайно и согласован с CoreAPI
    # В production: generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    webhook_secret_key: str = ""  # пусто = отключить проверку (только для dev)
    webhook_signature_header: str = "x-webhook-signature"  # заголовок с подписью
    webhook_signature_algorithm: str = "sha256"  # алгоритм подписи
    webhook_signature_version: str = "v1"  # версия формата подписи

    # ── Эмбеддинги (локальная модель) ─────────────────────────────
    embedding_model: str = "intfloat/multilingual-e5-large"
    transformers_cache: str = "/app/.cache/huggingface"

    # ── Claude API ─────────────────────────────────────────────────
    # Не менять без обновления prompts/ и записи в changelog
    # 2026-06-10: claude-sonnet-4-20250514 → claude-sonnet-4-6
    # (старая модель прекращает работу 15.06.2026; промпты совместимы,
    #  temperature и forced tool_choice работают без изменений)
    claude_model: str = "claude-sonnet-4-6"
    claude_extraction_temperature: float = 0.0   # детерминированность
    claude_decision_temperature: float = 0.1     # небольшая вариативность
    claude_extraction_max_tokens: int = 2000
    claude_decision_max_tokens: int = 4000
    claude_chunking_max_tokens: int = 4096

    # ── OCR ────────────────────────────────────────────────────────
    ocr_max_retries: int = 3
    ocr_min_confidence: float = 0.70
    ocr_language_hints: list[str] = ["ru", "ka", "en"]
    # Полный путь к процессору Document AI:
    # projects/{project_id}/locations/{location}/processors/{processor_id}
    gcp_document_ai_processor: str = "projects/insurance-claims-dev/locations/us/processors/FORM_PARSER"
    # Максимум символов текста на блок при сохранении в claim_documents.ocr_blocks
    # (полный текст уже хранится в ocr_text; блоки — для анализа confidence/регионов)
    ocr_block_text_max_chars: int = 200

    # ── Кросс-документная согласованность (Шаг 25) ────────────────
    extraction_name_match_threshold: float = 0.90        # fuzzy ФИО form_100 vs id_document
    extraction_institution_match_threshold: float = 0.70 # fuzzy название учреждения
    extraction_institution_mismatch_penalty: float = 0.85 # множитель confidence при расхождении
    extraction_date_mismatch_max_days: int = 3            # допустимое расхождение дат между документами
    extraction_amount_mismatch_pct: float = 0.01          # допуск расхождения сумм (±1%)

    # ── Quality Gate ───────────────────────────────────────────────
    quality_min_resolution_dpi: int = 150
    quality_max_blur_score: float = 100.0
    quality_min_brightness: float = 40.0
    quality_max_brightness: float = 220.0
    quality_max_skew_angle_deg: float = 45.0

    # ── RAG ────────────────────────────────────────────────────────
    rag_top_k: int = 12   # отдельные запросы по диагнозам + исключения
    rag_rrf_k: int = 60   # константа Reciprocal Rank Fusion

    # ── Enterprise: качество решений (Шаги 21–28) ─────────────────
    decision_coherence_check_enabled: bool = True
    # Штраф к overall_confidence при медицинской несогласованности (Шаг 21)
    decision_coherence_confidence_penalty: float = 0.10
    decision_chain_of_thought_enabled: bool = True
    decision_extended_thinking_enabled: bool = True
    decision_extended_thinking_threshold: float = 300.0   # GEL
    decision_extended_thinking_budget_tokens: int = 2000  # transitional; Sonnet 4.6 → adaptive
    # Порог extraction_confidence ниже которого включается thinking (Шаг 26)
    decision_extended_thinking_extraction_conf_threshold: float = 0.85
    # max_tokens для пути с thinking (reasoning-токены считаются в лимит)
    claude_decision_max_tokens_thinking: int = 8000
    # Усечение reasoning при записи в audit_log.output_data["reasoning"]
    decision_reasoning_audit_max_chars: int = 4000
    decision_second_pass_confidence_threshold: float = 0.65
    decision_stochastic_qa_rate: float = 0.05   # 5% AUTO_APPROVED → случайная проверка
    decision_waiting_period_enabled: bool = True   # False — отключить проверку периода ожидания
    decision_default_waiting_period_days: int = 30
    # Дефолт калибровочного фактора; рабочее значение живёт в
    # platform.tenant_configs['confidence_calibration_factor'] и читается в make_decision()
    decision_confidence_calibration_factor: float = 1.0
    fraud_amount_benchmark_enabled: bool = False   # включить после 3+ месяцев данных

    # ── Петля обучения (Шаги 29–35) ───────────────────────────────
    learning_feedback_loop_enabled: bool = True
    learning_calibration_window_days: int = 30      # окно расчёта точности
    learning_min_samples_for_calibration: int = 30  # минимум проверенных заявок
    learning_calibration_significant_diff: float = 0.05  # |actual - claimed| для обновления
    # Кламп фактора — одна плохая неделя не должна обнулить автоодобрение
    learning_calibration_factor_min: float = 0.5
    learning_calibration_factor_max: float = 1.2

    # ── Rule-based Extraction (альтернатива Claude в Слое 4) ───────
    # Включить после тестирования на реальных документах.
    # Цель: снизить стоимость и latency extraction без потери качества.
    extraction_use_rules: bool = False
    extraction_rules_min_confidence: float = 0.60  # ниже → manual_review

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Возвращает singleton настроек (кэшируется)."""
    return Settings()
