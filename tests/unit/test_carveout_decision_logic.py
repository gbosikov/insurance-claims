"""Unit-тесты для применения CARVEOUT-условий в decision engine."""

from unittest.mock import MagicMock

import pytest

from core.schemas.contract import ContractChunkSchema
from layers.decision.service import apply_carveout_exclusion_logic


class TestApplyCarveoutExclusionLogic:
    """Тесты для функции apply_carveout_exclusion_logic."""

    def test_no_carveout_chunks_returns_false(self):
        """Если нет CARVEOUT-чанков, не отказываем."""
        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="urgent",
            carveout_chunks=[],
        )
        assert should_reject is False
        assert reason is None

    def test_carveout_with_matching_service_urgency_allows(self):
        """Если service_urgency совпадает CARVEOUT-условию, диагноз покрыт."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18", "N19"],
            "carveout_conditions": [
                {
                    "type": "service_urgency",
                    "value": "urgent",
                    "ka_marker": "ურგენტული ჩარევა"
                }
            ],
            "general_exceptions": []
        }
        chunk.title = "4.1"
        chunk.content = "თირკმლის ქრონიკულ უკმარისობა..."

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="urgent",
            carveout_chunks=[chunk],
        )
        assert should_reject is False
        assert reason is None

    def test_carveout_with_non_matching_service_urgency_rejects(self):
        """Если service_urgency не совпадает CARVEOUT-условию, диагноз исключён."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18", "N19"],
            "carveout_conditions": [
                {"type": "service_urgency", "value": "urgent"}
            ],
            "general_exceptions": []
        }
        chunk.title = "4.1"
        chunk.content = "Текст исключения"

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="planned",  # ← не совпадает
            carveout_chunks=[chunk],
        )
        assert should_reject is True
        assert "service_urgency=planned" in reason

    def test_carveout_with_null_service_urgency_returns_false(self):
        """Если service_urgency=None и есть CARVEOUT, не отказываем (manual_review позже)."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18"],
            "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
            "general_exceptions": []
        }

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency=None,  # неизвестна
            carveout_chunks=[chunk],
        )
        # Не отказываем здесь, дождёмся проверки should_require_manual_review_for_unknown_urgency
        assert should_reject is False
        assert reason is None

    def test_general_exception_overrides_exclusion(self):
        """Если диагноз в general_exceptions, он не исключён (гепатит A)."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["B15", "B16", "B17", "B18", "B19"],  # все гепатиты
            "carveout_conditions": [{"type": "service_urgency", "value": "diagnostic"}],
            "general_exceptions": ["B15"],  # ← гепатит A НЕ исключён!
        }
        chunk.title = "4.2"
        chunk.content = "Гепатиты..."

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="B15.9",  # гепатит A
            service_urgency="planned",
            carveout_chunks=[chunk],
        )
        assert should_reject is False
        assert reason is None

    def test_diagnosis_not_excluded_returns_false(self):
        """Если диагноз не в excluded_icd10, не отказываем."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18"],
            "carveout_conditions": [],
            "general_exceptions": []
        }

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="J06.9",  # не в excluded_icd10
            service_urgency="urgent",
            carveout_chunks=[chunk],
        )
        assert should_reject is False
        assert reason is None

    def test_carveout_without_conditions_rejects(self):
        """Если диагноз в excluded_icd10, но нет carveout_conditions → отказ."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18"],
            "carveout_conditions": [],  # нет условий!
            "general_exceptions": []
        }
        chunk.title = "4.1"
        chunk.content = "Простое исключение без условий"

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="urgent",
            carveout_chunks=[chunk],
        )
        assert should_reject is True
        assert "Исключение по договору" in reason


class TestCarveoutWithRealScenarios:
    """Интеграционные тесты с реальными сценариями из контракта."""

    def test_real_scenario_n18_urgent_covered(self):
        """Реальный сценарий: N18 + urgent → ПОКРЫТО."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18", "N19"],
            "carveout_conditions": [
                {"type": "service_urgency", "value": "urgent"}
            ],
            "general_exceptions": []
        }

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="urgent",
            carveout_chunks=[chunk],
        )
        assert should_reject is False

    def test_real_scenario_n18_planned_rejected(self):
        """Реальный сценарий: N18 + planned → ИСКЛЮЧЕНО."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18", "N19"],
            "carveout_conditions": [
                {"type": "service_urgency", "value": "urgent"}
            ],
            "general_exceptions": []
        }

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="planned",
            carveout_chunks=[chunk],
        )
        assert should_reject is True
        assert "service_urgency=planned" in reason

    def test_real_scenario_hepatitis_a_not_excluded(self):
        """Реальный сценарий: Гепатит B (B16) + diagnostic → ПОКРЫТО,
        но Гепатит A (B15) + planned → ПОКРЫТО (исключение из исключения)."""

        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["B15", "B16", "B17", "B18", "B19"],
            "carveout_conditions": [
                {"type": "service_urgency", "value": "diagnostic"}
            ],
            "general_exceptions": ["B15"],  # Гепатит A
        }

        # Гепатит B + diagnostic → ПОКРЫТО (condition совпадает)
        should_reject_b16, _ = apply_carveout_exclusion_logic(
            icd10_code="B16.9",
            service_urgency="diagnostic",
            carveout_chunks=[chunk],
        )
        assert should_reject_b16 is False

        # Гепатит A + planned → ПОКРЫТО (general_exception)
        should_reject_b15, _ = apply_carveout_exclusion_logic(
            icd10_code="B15.9",
            service_urgency="planned",
            carveout_chunks=[chunk],
        )
        assert should_reject_b15 is False

    def test_icd10_prefix_matching(self):
        """Проверить что префикс-матчинг работает (N18.3 совпадает с N18)."""
        chunk = MagicMock(spec=ContractChunkSchema)
        chunk.section_type = "exclusion_with_carveout"
        chunk.chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18"],  # без точки
            "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
            "general_exceptions": []
        }

        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",  # с точкой
            service_urgency="urgent",
            carveout_chunks=[chunk],
        )
        assert should_reject is False
