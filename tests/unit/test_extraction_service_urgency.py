"""Unit-тесты для service_urgency в extraction service."""

import pytest

from core.schemas.claim import DiagnoisItem
from layers.extraction.service import (
    resolve_service_urgency,
    should_require_manual_review_for_unknown_urgency,
)


class TestResolveServiceUrgency:
    """Тесты для функции resolve_service_urgency."""

    def test_explicit_urgency_returned_as_is(self):
        """Если urgency явно указана, возвращаем как есть."""
        diagnoses = [DiagnoisItem(icd10_code="J06.9", description="ОРВИ")]

        assert resolve_service_urgency("urgent", diagnoses) == "urgent"
        assert resolve_service_urgency("diagnostic", diagnoses) == "diagnostic"
        assert resolve_service_urgency("planned", diagnoses) == "planned"

    def test_none_with_no_diagnoses(self):
        """Если нет диагнозов, возвращаем None."""
        assert resolve_service_urgency(None, []) is None

    def test_acute_diagnosis_resolves_to_urgent(self):
        """Острые диагнозы (J0, B1, R0) → urgent."""
        diagnoses = [
            DiagnoisItem(icd10_code="J06.9", description="ОРВИ"),
            DiagnoisItem(icd10_code="R06.0", description="Одышка"),
        ]
        assert resolve_service_urgency(None, diagnoses) == "urgent"

    def test_chronic_diagnosis_resolves_to_planned(self):
        """Хронические диагнозы (N18, I10, E11) → planned."""
        diagnoses = [
            DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная недостаточность"),
            DiagnoisItem(icd10_code="I10", description="Гипертония"),
        ]
        assert resolve_service_urgency(None, diagnoses) == "planned"

    def test_mixed_diagnoses_return_none(self):
        """Если диагнозы смешанные, возвращаем None."""
        diagnoses = [
            DiagnoisItem(icd10_code="J06.9", description="ОРВИ"),  # острая
            DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная"),  # хроническая
        ]
        assert resolve_service_urgency(None, diagnoses) is None

    def test_unknown_icd10_prefix_not_counted(self):
        """Неизвестные префиксы МКБ-10 не учитываются."""
        diagnoses = [
            DiagnoisItem(icd10_code="Z00.0", description="Общий осмотр"),  # Z-код (неизвестен)
            DiagnoisItem(icd10_code="J06.9", description="ОРВИ"),  # острая
        ]
        assert resolve_service_urgency(None, diagnoses) == "urgent"

    def test_single_acute_diagnosis(self):
        """Один острый диагноз → urgent."""
        diagnoses = [DiagnoisItem(icd10_code="B16.9", description="Гепатит B")]
        assert resolve_service_urgency(None, diagnoses) == "urgent"

    def test_single_chronic_diagnosis(self):
        """Один хронический диагноз → planned."""
        diagnoses = [DiagnoisItem(icd10_code="E11.9", description="Диабет 2 типа")]
        assert resolve_service_urgency(None, diagnoses) == "planned"


class TestShouldRequireManualReviewForUnknownUrgency:
    """Тесты для функции should_require_manual_review_for_unknown_urgency."""

    def test_known_urgency_no_manual_review(self):
        """Если urgency известна, manual_review не требуется."""
        diagnoses = [DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная")]

        result, reason = should_require_manual_review_for_unknown_urgency(
            service_urgency="urgent",
            diagnoses=diagnoses,
            has_carveout_exclusions=True,
        )
        assert result is False
        assert reason is None

    def test_unknown_urgency_with_carveout_requires_review(self):
        """Если urgency неизвестна И есть CARVEOUT → требуется manual_review."""
        diagnoses = [DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная")]

        result, reason = should_require_manual_review_for_unknown_urgency(
            service_urgency=None,
            diagnoses=diagnoses,
            has_carveout_exclusions=True,
        )
        assert result is True
        assert "unknown_service_urgency_with_carveout_exclusion" in reason

    def test_unknown_urgency_without_carveout_no_review(self):
        """Если urgency неизвестна НО нет CARVEOUT → manual_review не требуется."""
        diagnoses = [DiagnoisItem(icd10_code="J06.9", description="ОРВИ")]

        result, reason = should_require_manual_review_for_unknown_urgency(
            service_urgency=None,
            diagnoses=diagnoses,
            has_carveout_exclusions=False,
        )
        assert result is False
        assert reason is None

    def test_reason_message_for_carveout_case(self):
        """Проверить что reason содержит полезную информацию."""
        diagnoses = [DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная")]

        _, reason = should_require_manual_review_for_unknown_urgency(
            service_urgency=None,
            diagnoses=diagnoses,
            has_carveout_exclusions=True,
        )

        assert "врач не указал тип услуги" in reason
        assert "контракте" in reason


class TestServiceUrgencyIntegration:
    """Интеграционные тесты для service_urgency."""

    def test_realistic_scenario_urgent_kidney_disease(self):
        """Реальный сценарий: неотложное вмешательство при хроническом заболевании почек."""
        diagnoses = [DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная")]

        urgency = resolve_service_urgency("urgent", diagnoses)
        assert urgency == "urgent"

        urgency_resolved = resolve_service_urgency(None, diagnoses)
        assert urgency_resolved == "planned"

        requires_review, _ = should_require_manual_review_for_unknown_urgency(
            urgency, diagnoses, has_carveout_exclusions=True
        )
        assert requires_review is False

    def test_realistic_scenario_diagnostic_for_hepatitis(self):
        """Реальный сценарий: первичная диагностика гепатита B."""
        diagnoses = [DiagnoisItem(icd10_code="B16.9", description="Гепатит B")]

        urgency = resolve_service_urgency("diagnostic", diagnoses)
        assert urgency == "diagnostic"

        requires_review, _ = should_require_manual_review_for_unknown_urgency(
            urgency, diagnoses, has_carveout_exclusions=True
        )
        assert requires_review is False

    def test_realistic_scenario_unknown_urgency_with_carveout(self):
        """Реальный сценарий: неизвестный тип услуги + CARVEOUT-исключение."""
        diagnoses = [DiagnoisItem(icd10_code="N18.3", description="Хроническая почечная")]

        urgency = resolve_service_urgency(None, diagnoses)
        assert urgency == "planned"

        diagnoses_unknown = [DiagnoisItem(icd10_code="Z99.9", description="Неизвестное")]
        urgency = resolve_service_urgency(None, diagnoses_unknown)
        assert urgency is None

        requires_review, reason = should_require_manual_review_for_unknown_urgency(
            urgency, diagnoses_unknown, has_carveout_exclusions=True
        )
        assert requires_review is True
        assert reason is not None
