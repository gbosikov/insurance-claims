"""Unit тесты: петля обучения (Шаг 29 — калибровка confidence)."""

import pytest

from services.worker.tasks_analytics import compute_calibration_factor


def test_no_update_when_diff_within_threshold():
    """|actual − claimed| ≤ 0.05 → фактор не меняется."""
    new_factor, updated = compute_calibration_factor(
        actual_accuracy=0.88, mean_claimed=0.90, old_factor=1.0,
    )
    assert updated is False
    assert new_factor == 1.0


def test_update_when_overconfident():
    """AI переоценивает себя: accuracy 0.80 при claimed 0.95 → фактор ≈ 0.842."""
    new_factor, updated = compute_calibration_factor(
        actual_accuracy=0.80, mean_claimed=0.95, old_factor=1.0,
    )
    assert updated is True
    assert new_factor == pytest.approx(0.80 / 0.95)


def test_update_when_underconfident_clamped_to_max():
    """AI недооценивает себя: accuracy 1.0 при claimed 0.70 → кламп до 1.2."""
    new_factor, updated = compute_calibration_factor(
        actual_accuracy=1.0, mean_claimed=0.70, old_factor=1.0,
    )
    assert updated is True
    assert new_factor == 1.2  # learning_calibration_factor_max


def test_extreme_overconfidence_clamped_to_min():
    """Катастрофическая точность → фактор не падает ниже 0.5 (кламп)."""
    new_factor, updated = compute_calibration_factor(
        actual_accuracy=0.10, mean_claimed=0.95, old_factor=1.0,
    )
    assert updated is True
    assert new_factor == 0.5  # learning_calibration_factor_min


def test_zero_claimed_confidence_no_update():
    """mean_claimed = 0 → деление невозможно, фактор не меняется."""
    new_factor, updated = compute_calibration_factor(
        actual_accuracy=0.90, mean_claimed=0.0, old_factor=1.0,
    )
    assert updated is False
    assert new_factor == 1.0
