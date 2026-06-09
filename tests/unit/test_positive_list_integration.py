"""Integration tests for POSITIVE LIST in decision engine."""

import json
from uuid import UUID

import pytest

from layers.decision.service import build_decision_prompt
from core.schemas.claim import EventData, LineItem, Diagnosis
from core.schemas.extraction import ExtractionResult
from core.models.icd10 import EnrichedDiagnosis
from core.schemas.risks import RisksAndLimits, RiskInfo
from core.schemas.contract import ContractChunkSchema


class TestPositiveListInPrompt:
    """Тесты для включения POSITIVE LIST в Claude промпт."""

    def test_build_decision_prompt_includes_positive_list(self):
        """Если есть совпадения в POSITIVE LIST, они должны быть в промпте."""
        extraction = ExtractionResult(
            insured={
                "full_name": "ვაჟა ქართველი",
                "birth_date": "1990-01-01",
                "personal_id": "12345678901",
                "policy_number": "POL-001",
            },
            event=EventData(
                date="2024-06-09",
                institution="Clinic ABC",
                service_urgency="diagnostic",
                diagnoses=[
                    Diagnosis(icd10_code="K29.7", description="Gastritis"),
                ],
                line_items=[
                    LineItem(description="Полипэктомия", amount=500.0),
                    LineItem(description="анализ крови", amount=150.0),
                ],
                total_claimed=650.0,
            ),
            extraction_confidence=0.95,
            flags=[],
        )

        enriched = {
            "K29.7": EnrichedDiagnosis(
                code="K29.7",
                name_r="Гастрит",
                name_g="გასტრიტი",
                category_chain_ru="Болезни органов пищеварения → Болезни желудка",
                ancestors=[],
            )
        }

        risks_limits = RisksAndLimits(
            annual_limit=10000.0,
            remaining=8000.0,
            currency="GEL",
            risks=[
                RiskInfo(
                    risk_id="ambulatory",
                    name="Амбулаторное лечение",
                    coverage_pct=80.0,
                    remaining_limit=5000.0,
                )
            ],
        )

        positive_list_match = {
            "Полипэктомия": (True, "Полипэктомия"),  # in POSITIVE LIST
            "анализ крови": (False, None),  # not in POSITIVE LIST
        }

        prompt = build_decision_prompt(
            extraction,
            enriched,
            risks_limits,
            chunks=[],
            positive_list_match=positive_list_match,
        )

        # Проверяем что POSITIVE LIST включена в промпт
        assert "POSITIVE LIST" in prompt
        assert "Полипэктомия" in prompt
        assert "явно покрытые процедуры" in prompt.lower() or "positive list" in prompt.lower()

    def test_build_decision_prompt_no_positive_list_items(self):
        """Если нет совпадений в POSITIVE LIST, секция не должна быть в промпте."""
        extraction = ExtractionResult(
            insured={
                "full_name": "ვაჟა ქართველი",
                "birth_date": "1990-01-01",
                "personal_id": "12345678901",
                "policy_number": "POL-001",
            },
            event=EventData(
                date="2024-06-09",
                institution="Clinic",
                diagnoses=[Diagnosis(icd10_code="J06.9", description="ORVI")],
                line_items=[LineItem(description="анализ крови", amount=100.0)],
                total_claimed=100.0,
            ),
            extraction_confidence=0.95,
            flags=[],
        )

        enriched = {
            "J06.9": EnrichedDiagnosis(
                code="J06.9",
                name_r="ОРВИ",
                name_g="მწვავე ტყვილი ფილტვის ინფექცია",
                category_chain_ru="Болезни органов дыхания",
                ancestors=[],
            )
        }

        risks_limits = RisksAndLimits(
            annual_limit=10000.0,
            remaining=9000.0,
            currency="GEL",
            risks=[],
        )

        positive_list_match = {
            "анализ крови": (False, None),  # не в POSITIVE LIST
        }

        prompt = build_decision_prompt(
            extraction,
            enriched,
            risks_limits,
            chunks=[],
            positive_list_match=positive_list_match,
        )

        # POSITIVE LIST секция не должна быть в промпте, если нет совпадений
        assert "явно покрытые процедуры" not in prompt.lower()

    def test_build_decision_prompt_multiple_positive_list_items(self):
        """Несколько процедур в POSITIVE LIST должны быть выведены все."""
        extraction = ExtractionResult(
            insured={
                "full_name": "ვაჟა ქართველი",
                "birth_date": "1990-01-01",
                "personal_id": "12345678901",
                "policy_number": "POL-001",
            },
            event=EventData(
                date="2024-06-09",
                institution="Clinic",
                diagnoses=[Diagnosis(icd10_code="M54.5", description="Back pain")],
                line_items=[
                    LineItem(description="Полипэктомия", amount=500.0),
                    LineItem(description="Аденоидэктомия", amount=400.0),
                    LineItem(description="анализ крови", amount=100.0),
                ],
                total_claimed=1000.0,
            ),
            extraction_confidence=0.95,
            flags=[],
        )

        enriched = {}

        risks_limits = RisksAndLimits(
            annual_limit=10000.0,
            remaining=8000.0,
            currency="GEL",
            risks=[],
        )

        positive_list_match = {
            "Полипэктомия": (True, "Полипэктомия"),
            "Аденоидэктомия": (True, "Аденoидэктомия"),
            "анализ крови": (False, None),
        }

        prompt = build_decision_prompt(
            extraction,
            enriched,
            risks_limits,
            chunks=[],
            positive_list_match=positive_list_match,
        )

        # Обе процедуры должны быть в промпте
        assert "Полипэктомия" in prompt
        assert "Аденоидэктомия" in prompt or "Аденoidектомия" in prompt


class TestPositiveListWithCarveout:
    """Тесты для взаимодействия POSITIVE LIST и CARVEOUT."""

    def test_positive_list_overrides_carveout_in_prompt(self):
        """POSITIVE LIST должна быть выделена отдельно от CARVEOUT.

        Это помогает Claude не путать явно покрытые процедуры с CARVEOUT-исключениями.
        """
        extraction = ExtractionResult(
            insured={
                "full_name": "ვაჟა ქართველი",
                "birth_date": "1990-01-01",
                "personal_id": "12345678901",
                "policy_number": "POL-001",
            },
            event=EventData(
                date="2024-06-09",
                institution="Clinic",
                diagnoses=[Diagnosis(icd10_code="N18.3", description="CKD")],
                line_items=[
                    LineItem(description="Полипэктомия", amount=500.0),
                    LineItem(description="анализ мочи", amount=200.0),
                ],
                total_claimed=700.0,
            ),
            extraction_confidence=0.95,
            flags=[],
        )

        enriched = {}

        risks_limits = RisksAndLimits(
            annual_limit=10000.0,
            remaining=9300.0,
            currency="GEL",
            risks=[],
        )

        # Создадим CARVEOUT чанк
        carveout_chunk = ContractChunkSchema(
            id="chunk-1",
            section_type="exclusion_with_carveout",
            title="N18 исключение",
            content="N18 исключена кроме urgent",
            chunk_structure={
                "type": "exclusion_with_carveout",
                "excluded_icd10": ["N18"],
                "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
                "general_exceptions": [],
            },
            embedding=None,
            key_terms=["N18"],
        )

        positive_list_match = {
            "Полипэктомия": (True, "Полипэктомия"),  # in POSITIVE LIST
            "анализ мочи": (False, None),
        }

        prompt = build_decision_prompt(
            extraction,
            enriched,
            risks_limits,
            chunks=[carveout_chunk],
            positive_list_match=positive_list_match,
        )

        # Оба раздела должны быть в промпте
        assert "POSITIVE LIST" in prompt
        assert "CARVEOUT" in prompt
        # POSITIVE LIST должна быть раньше остальных (явный приоритет)
        pos_list_pos = prompt.find("POSITIVE LIST")
        carveout_pos = prompt.find("CARVEOUT")
        assert pos_list_pos < carveout_pos, "POSITIVE LIST should appear before CARVEOUT in prompt"
