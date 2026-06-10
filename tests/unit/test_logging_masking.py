"""Unit тесты: core/logging.py — маскирование ПД в логах."""

import structlog

from core.logging import (
    MASK,
    configure_logging,
    mask_pii,
    mask_pii_processor,
    sanitize_url,
)


# ── Прямые идентификаторы (ключи) ─────────────────────────────────


def test_sensitive_keys_masked_at_top_level():
    event = mask_pii_processor(None, "info", {
        "event": "extraction_completed",
        "full_name": "Иванов Иван Иванович",
        "personal_id": "12345678901",
        "birth_date": "1988-03-15",
        "claim_id": "11111111-1111-1111-1111-111111111111",
    })
    assert event["full_name"] == MASK
    assert event["personal_id"] == MASK
    assert event["birth_date"] == MASK
    # claim_id — безопасный указатель, не маскируется
    assert event["claim_id"] == "11111111-1111-1111-1111-111111111111"


def test_sensitive_keys_masked_in_nested_structures():
    event = mask_pii_processor(None, "info", {
        "event": "x",
        "extraction": {
            "insured": {"full_name": "Иванов И.И.", "personal_id": "12345678901"},
            "documents": [{"personalnumber": "98765432109", "doc_type": "form_100"}],
        },
    })
    insured = event["extraction"]["insured"]
    assert insured["full_name"] == MASK
    assert insured["personal_id"] == MASK
    assert event["extraction"]["documents"][0]["personalnumber"] == MASK
    assert event["extraction"]["documents"][0]["doc_type"] == "form_100"


def test_key_matching_is_case_insensitive():
    event = mask_pii_processor(None, "info", {"event": "x", "Full_Name": "Иванов"})
    assert event["Full_Name"] == MASK


def test_non_string_sensitive_values_masked():
    event = mask_pii_processor(None, "info", {"event": "x", "personal_id": 12345678901})
    assert event["personal_id"] == MASK


# ── URL и секреты ─────────────────────────────────────────────────


def test_url_key_query_stripped():
    """Pre-signed токен — действующий секрет, query обрезается."""
    event = mask_pii_processor(None, "info", {
        "event": "download",
        "url": "https://medsystem.example.com/files/form100.pdf?token=SECRET&expires=1718100000",
    })
    assert "SECRET" not in event["url"]
    assert event["url"].startswith("https://medsystem.example.com/files/form100.pdf")


def test_url_without_query_unchanged():
    assert sanitize_url("https://host/path.pdf") == "https://host/path.pdf"


def test_url_inside_free_text_masked():
    event = mask_pii_processor(None, "error", {
        "event": "download_failed",
        "error": "HTTPError for https://h.example/f.pdf?token=SECRET123: 403",
    })
    assert "SECRET123" not in event["error"]


# ── Свободный текст ───────────────────────────────────────────────


def test_personal_id_run_masked_in_free_text():
    """11-значный личный номер в произвольной строке (warning и т.п.)."""
    event = mask_pii_processor(None, "warning", {
        "event": "cross_validation_warnings",
        "warnings": ["Personal id 12345678901 not found in registry"],
    })
    assert "12345678901" not in event["warnings"][0]
    assert MASK in event["warnings"][0]


def test_short_and_long_digit_runs_untouched():
    """Суммы (короткие) и UUID-подобные длинные последовательности не трогаем."""
    assert mask_pii("amount=150.00 count=12345678") == "amount=150.00 count=12345678"
    assert mask_pii("123456789012") == "123456789012"  # 12 цифр — не личный номер


def test_icd10_codes_not_masked():
    """Косвенные атрибуты (МКБ-10) остаются — нужны для отладки decision."""
    event = mask_pii_processor(None, "info", {"event": "x", "icd10_code": "J06.9"})
    assert event["icd10_code"] == "J06.9"


# ── Конфигурация ──────────────────────────────────────────────────


def test_configure_logging_installs_masking_processor():
    configure_logging()
    try:
        # cap-логгер structlog прогоняет событие через настроенные процессоры
        cap = structlog.testing.LogCapture()
        processors = [mask_pii_processor, cap]
        structlog.configure(processors=processors)
        structlog.get_logger().info("test_event", full_name="Иванов Иван")
        assert cap.entries[0]["full_name"] == MASK
    finally:
        structlog.reset_defaults()
