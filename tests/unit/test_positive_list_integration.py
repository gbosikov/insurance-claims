"""Integration tests for POSITIVE LIST in decision engine (build_decision_prompt)."""

from uuid import uuid4

from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
from core.schemas.contract import ContractChunkSchema
from core.schemas.core_api import RiskInfo, RisksAndLimits
from layers.decision.icd10_enricher import EnrichedDiagnosis
from layers.decision.service import build_decision_prompt

POLICY_NUMBER = "POL-001"


def make_extraction(
    diagnoses: list[DiagnoisItem],
    line_items: list[LineItem],
    total_claimed: float,
    service_urgency: str | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        insured=InsuredData(
            full_name="ვაჟა ქართველი",
            birth_date="1990-01-01",
            personal_id="12345678901",
            policy_number=POLICY_NUMBER,
        ),
        event=EventData(
            date="2024-06-09",
            institution="Clinic ABC",
            service_urgency=service_urgency,
            diagnoses=diagnoses,
            line_items=line_items,
            total_claimed=total_claimed,
        ),
        extraction_confidence=0.95,
        flags=[],
    )


def make_risks(remaining: float = 8000.0) -> RisksAndLimits:
    return RisksAndLimits(
        policy_number=POLICY_NUMBER,
        annual_limit=10000.0,
        remaining=remaining,
        currency="GEL",
        risks=[RiskInfo(
            risk_id=1,
            name="Амбулаторное лечение",
            coverage_pct=80.0,
            total_limit=10000.0,
            remaining_limit=remaining,
            currency="GEL",
        )],
    )


def make_carveout_chunk() -> ContractChunkSchema:
    return ContractChunkSchema(
        id=uuid4(),
        policy_number=POLICY_NUMBER,
        version_id="v1",
        section_type="exclusion_with_carveout",
        title="N18 исключение",
        content="N18 исключена кроме urgent",
        key_terms=["N18"],
        chunk_structure={
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18"],
            "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
            "general_exceptions": [],
        },
    )


class TestPositiveListInPrompt:
    """Тесты для включения POSITIVE LIST в Claude промпт."""

    def test_build_decision_prompt_includes_positive_list(self):
        """Если есть совпадения в POSITIVE LIST, они должны быть в промпте."""
        extraction = make_extraction(
            diagnoses=[DiagnoisItem(icd10_code="K29.7", description="Gastritis")],
            line_items=[
                LineItem(description="Полипэктомия", amount=500.0),
                LineItem(description="анализ крови", amount=150.0),
            ],
            total_claimed=650.0,
            service_urgency="diagnostic",
        )
        enriched = {
            "K29.7": EnrichedDiagnosis(
                code="K29.7",
                name_r="Гастрит",
                name_g="გასტრიტი",
                name_e="Gastritis",
            )
        }
        positive_list_match = {
            "Полипэктомия": (True, "Полипэктомия"),   # in POSITIVE LIST
            "анализ крови": (False, None),            # not in POSITIVE LIST
        }

        prompt = build_decision_prompt(
            extraction, enriched, make_risks(),
            chunks=[],
            positive_list_match=positive_list_match,
        )

        assert "POSITIVE LIST" in prompt
        assert "Полипэктомия" in prompt
        assert "явно покрытые процедуры" in prompt.lower()

    def test_build_decision_prompt_no_positive_list_items(self):
        """Если нет совпадений в POSITIVE LIST, секция не должна быть в промпте."""
        extraction = make_extraction(
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ORVI")],
            line_items=[LineItem(description="анализ крови", amount=100.0)],
            total_claimed=100.0,
        )
        positive_list_match = {
            "анализ крови": (False, None),  # не в POSITIVE LIST
        }

        prompt = build_decision_prompt(
            extraction, {}, make_risks(remaining=9000.0),
            chunks=[],
            positive_list_match=positive_list_match,
        )

        assert "POSITIVE LIST" not in prompt
        assert "явно покрытые процедуры" not in prompt.lower()

    def test_build_decision_prompt_multiple_positive_list_items(self):
        """Несколько процедур в POSITIVE LIST должны быть выведены все."""
        extraction = make_extraction(
            diagnoses=[DiagnoisItem(icd10_code="M54.5", description="Back pain")],
            line_items=[
                LineItem(description="Полипэктомия", amount=500.0),
                LineItem(description="Аденоидэктомия", amount=400.0),
                LineItem(description="анализ крови", amount=100.0),
            ],
            total_claimed=1000.0,
        )
        positive_list_match = {
            "Полипэктомия": (True, "Полипэктомия"),
            "Аденоидэктомия": (True, "Аденоидэктомия"),
            "анализ крови": (False, None),
        }

        prompt = build_decision_prompt(
            extraction, {}, make_risks(),
            chunks=[],
            positive_list_match=positive_list_match,
        )

        assert "Полипэктомия" in prompt
        assert "Аденоидэктомия" in prompt


class TestPositiveListWithCarveout:
    """Тесты для взаимодействия POSITIVE LIST и CARVEOUT."""

    def test_positive_list_and_carveout_are_separate_sections(self):
        """POSITIVE LIST должна быть выделена отдельно от CARVEOUT.

        Это помогает Claude не путать явно покрытые процедуры с CARVEOUT-исключениями.
        """
        extraction = make_extraction(
            diagnoses=[DiagnoisItem(icd10_code="N18.3", description="CKD")],
            line_items=[
                LineItem(description="Полипэктомия", amount=500.0),
                LineItem(description="анализ мочи", amount=200.0),
            ],
            total_claimed=700.0,
        )
        positive_list_match = {
            "Полипэктомия": (True, "Полипэктомия"),  # in POSITIVE LIST
            "анализ мочи": (False, None),
        }

        prompt = build_decision_prompt(
            extraction, {}, make_risks(remaining=9300.0),
            chunks=[make_carveout_chunk()],
            positive_list_match=positive_list_match,
        )

        # Оба раздела присутствуют как отдельные секции
        assert "POSITIVE LIST" in prompt
        assert "CARVEOUT" in prompt
        # Структура CARVEOUT передана Claude (исключённые коды видны)
        assert "N18" in prompt

    def test_carveout_chunk_structure_survives_schema_validation(self):
        """ContractChunkSchema.model_validate сохраняет chunk_structure (регрессия).

        Раньше поле отсутствовало в схеме: model_validate() молча отбрасывал
        структуру, а apply_carveout_exclusion_logic падал с AttributeError.
        """
        from layers.decision.service import apply_carveout_exclusion_logic

        chunk = make_carveout_chunk()
        revalidated = ContractChunkSchema.model_validate(chunk.model_dump())
        assert revalidated.chunk_structure is not None
        assert revalidated.chunk_structure["excluded_icd10"] == ["N18"]

        # planned не совпадает с carveout-условием urgent → отказ
        should_reject, reason = apply_carveout_exclusion_logic(
            icd10_code="N18.3",
            service_urgency="planned",
            carveout_chunks=[revalidated],
        )
        assert should_reject is True
        assert reason is not None
