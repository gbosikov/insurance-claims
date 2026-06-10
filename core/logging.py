"""
core/logging.py — центральная конфигурация structlog + маскирование ПД.

ПРИНЦИП: логи — для диагностики, не для данных. Прямые идентификаторы
субъекта (ФИО, личный номер, дата рождения) и секреты (query-string
pre-signed URL) в логи не попадают. Для расследования достаточно claim_id —
полные данные авторизованно доступны в БД (audit_log, claim_documents).

Косвенные атрибуты (коды МКБ-10, суммы, даты событий) НЕ маскируются —
они нужны для отладки decision-логики и без прямых идентификаторов
не указывают на субъекта.

audit_log этим модулем не затрагивается: там ПД хранятся по назначению,
под контролем доступа (core/auth.py) и retention-политикой регулятора.

Подключение: configure_logging() вызывается при старте api (main.py)
и worker/beat (celery_app.py).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse

import structlog

MASK = "***"

# Ключи событий, значения которых маскируются целиком (сравнение без регистра).
# Прямые идентификаторы субъекта — независимо от уровня вложенности.
SENSITIVE_KEYS = frozenset({
    "full_name",
    "insured_name",
    "patient_name",
    "personal_id",
    "personal_id_number",
    "personalnumber",
    "personal_number",
    "birth_date",
    "birthdate",
})

# Ключи с URL: обрезается query-string (pre-signed токены — действующие секреты)
URL_KEYS = frozenset({"url", "source_url", "pdf_url", "href"})

# Свободный текст: последовательности 9-11 цифр = личные номера (Грузия — 11)
_PERSONAL_ID_RE = re.compile(r"\b\d{9,11}\b")
# Свободный текст: query-string у URL (строка распознаётся по '://')
_URL_QUERY_RE = re.compile(r"\?\S+")


def sanitize_url(url: str) -> str:
    """Обрезать query/fragment: pre-signed токены не должны попадать в логи."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return MASK
    if not parsed.query and not parsed.fragment:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", "")) + "?…"


def _mask_free_text(text: str) -> str:
    """Личные номера и токены URL в произвольных строках (warnings, error и т.п.)."""
    masked = _PERSONAL_ID_RE.sub(MASK, text)
    if "://" in masked:
        masked = _URL_QUERY_RE.sub("?…", masked)
    return masked


def mask_pii(value: Any, key: str | None = None) -> Any:
    """Рекурсивное маскирование значения с учётом имени ключа."""
    key_lower = key.lower() if isinstance(key, str) else ""

    if key_lower in SENSITIVE_KEYS:
        return MASK

    if isinstance(value, dict):
        return {k: mask_pii(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_pii(v, key) for v in value]
    if isinstance(value, str):
        if key_lower in URL_KEYS:
            return sanitize_url(value)
        return _mask_free_text(value)
    return value


def mask_pii_processor(logger, method_name, event_dict: dict) -> dict:
    """structlog-процессор: применяется к каждому событию до рендера."""
    return {k: mask_pii(v, k) for k, v in event_dict.items()}


def configure_logging() -> None:
    """
    Конфигурация structlog для всех сервисов.

    development → читаемый консольный вывод; иначе — JSON (правило CLAUDE.md).
    Маскирование ПД включено всегда.
    """
    from core.config import get_settings  # lazy: env может быть не готов при импорте

    dev_mode = get_settings().environment == "development"
    renderer = (
        structlog.dev.ConsoleRenderer()
        if dev_mode
        else structlog.processors.JSONRenderer(ensure_ascii=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            mask_pii_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        cache_logger_on_first_use=True,
    )
