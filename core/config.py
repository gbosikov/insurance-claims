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
    anthropic_api_key: str
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

    # URL сервера для ClaimParsing_UNI (может отличаться от LiteMed API).
    # Пусто = использовать core_api_base_url.
    # Формат вызова: POST {core_api_claims_base_url}/LiteApi/LiteServiceJSON
    # Тело: {"METHODNAME": "ClaimParsing_UNI", "XML_DATA": {...}}
    # Уточнить реальный URL у владельца кор-системы.
    core_api_claims_base_url: str = ""

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

    # ── Эмбеддинги (локальная модель) ─────────────────────────────
    embedding_model: str = "intfloat/multilingual-e5-large"
    transformers_cache: str = "/app/.cache/huggingface"

    # ── Claude API ─────────────────────────────────────────────────
    # Не менять без обновления prompts/ и записи в changelog
    claude_model: str = "claude-sonnet-4-20250514"
    claude_extraction_temperature: float = 0.0   # детерминированность
    claude_decision_temperature: float = 0.1     # небольшая вариативность
    claude_extraction_max_tokens: int = 1000
    claude_decision_max_tokens: int = 4000
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
    rag_top_k: int = 12   # отдельные запросы по диагнозам + исключения
    rag_rrf_k: int = 60   # константа Reciprocal Rank Fusion

    # ── Enterprise: качество решений (Шаги 21–28) ─────────────────
    decision_coherence_check_enabled: bool = True
    decision_chain_of_thought_enabled: bool = True
    decision_extended_thinking_enabled: bool = True
    decision_extended_thinking_threshold: float = 300.0   # GEL
    decision_extended_thinking_budget_tokens: int = 2000
    decision_second_pass_confidence_threshold: float = 0.65
    decision_stochastic_qa_rate: float = 0.05   # 5% AUTO_APPROVED → случайная проверка
    decision_default_waiting_period_days: int = 30
    decision_confidence_calibration_factor: float = 1.0
    fraud_amount_benchmark_enabled: bool = False   # включить после 3+ месяцев данных

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Возвращает singleton настроек (кэшируется)."""
    return Settings()
