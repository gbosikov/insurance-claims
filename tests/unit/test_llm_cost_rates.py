"""Unit тесты: тарифы LLM выбираются по МОДЕЛИ (активной или из model_version)."""

from core.config import get_settings


# ── llm_cost_per_mtok: по активной модели (из .env) ──────────────────

def test_active_cost_gemini_25(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "gemini")
    monkeypatch.setattr(s, "gemini_model", "gemini-2.5-flash")
    assert s.llm_cost_per_mtok() == (0.30, 2.50)


def test_active_cost_gemini_35(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "gemini")
    monkeypatch.setattr(s, "gemini_model", "gemini-3.5-flash")
    assert s.llm_cost_per_mtok() == (1.50, 9.00)


# ── cost_for_model: по КОНКРЕТНОЙ модели (из audit_log.model_version) ──

def test_cost_for_model_exact():
    """Каждая модель считается по своей цене — независимо от активной."""
    s = get_settings()
    assert s.cost_for_model("gemini-2.5-flash") == (0.30, 2.50)
    assert s.cost_for_model("gemini-3.5-flash") == (1.50, 9.00)
    assert s.cost_for_model("claude-sonnet-4-6") == (3.00, 15.00)


def test_cost_for_model_unknown_gemini_fallback(monkeypatch):
    """Неизвестная gemini-модель → per-provider fallback из .env."""
    s = get_settings()
    monkeypatch.setattr(s, "gemini_input_cost_per_mtok", 0.11)
    monkeypatch.setattr(s, "gemini_output_cost_per_mtok", 0.22)
    assert s.cost_for_model("gemini-experimental-999") == (0.11, 0.22)


def test_cost_for_model_null_uses_active(monkeypatch):
    """null model_version (старые записи) → тариф активной модели."""
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "gemini")
    monkeypatch.setattr(s, "gemini_model", "gemini-2.5-flash")
    assert s.cost_for_model(None) == (0.30, 2.50)


def test_mixed_history_prices_each_model_independently():
    """Смешанная история: 2.5 и 3.5 считаются каждая по своей цене
    независимо от того, какая модель активна сейчас."""
    s = get_settings()
    assert s.cost_for_model("gemini-2.5-flash") != s.cost_for_model("gemini-3.5-flash")
