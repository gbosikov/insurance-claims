"""
Unit тесты: Слой 2 — Preprocessing / Quality Gate.
"""

import numpy as np
import pytest

from core.exceptions import DocumentQualityError
from core.config import get_settings
from layers.preprocessing.service import QualityReport, check_quality

settings = get_settings()


def make_gray_image(width=800, height=1100, brightness=128, blur=True) -> np.ndarray:
    """Создаёт тестовое изображение."""
    import cv2
    img = np.full((height, width, 3), brightness, dtype=np.uint8)
    if blur:
        img = cv2.GaussianBlur(img, (21, 21), 0)
    return img


def test_quality_gate_passes_good_image():
    """Нормальное изображение проходит quality gate."""
    import cv2
    # Создаём изображение с текстом (для blur score)
    img = np.zeros((1100, 800, 3), dtype=np.uint8)
    img[:] = 128  # серый фон
    # Добавляем контрастный текст — повышает Laplacian variance
    cv2.putText(img, "TEST DOCUMENT", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 0), 5)

    report = check_quality(img, dpi=200.0)
    # Проверяем что не все флаги выставлены
    assert "dark" not in report.flags
    assert "bright" not in report.flags


def test_quality_gate_fails_dark_image():
    """Очень тёмное изображение → флаг 'dark'."""
    img = np.full((1100, 800, 3), 10, dtype=np.uint8)  # почти чёрный
    report = check_quality(img, dpi=200.0)
    assert "dark" in report.flags
    assert not report.passed


def test_quality_gate_fails_bright_image():
    """Пересвеченное изображение → флаг 'bright'."""
    img = np.full((1100, 800, 3), 250, dtype=np.uint8)  # почти белый
    report = check_quality(img, dpi=200.0)
    assert "bright" in report.flags
    assert not report.passed


def test_quality_gate_fails_low_resolution():
    """Маленькое изображение → флаг 'low_resolution'."""
    img = np.full((200, 150, 3), 128, dtype=np.uint8)  # очень маленькое
    report = check_quality(img)  # dpi оценивается по размеру
    assert "low_resolution" in report.flags


def test_quality_report_score_decreases_with_flags():
    """Score уменьшается за каждый флаг."""
    img_dark = np.full((1100, 800, 3), 5, dtype=np.uint8)
    report = check_quality(img_dark, dpi=200.0)
    assert report.score < 1.0
    assert len(report.flags) > 0
