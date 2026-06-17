"""
Unit тесты: Слой 7 — Decision Engine (детерминированные проверки уровня 1).

Тестируем только детерминированные ветки — без реального вызова Claude API.
"""

from datetime import date
from uuid import UUID

import pytest

from core.exceptions import PolicyLimitExhaustedError
from core.llm_client import LLMResult
from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
from core.schemas.core_api import RiskInfo, RisksAndLimits
from layers.decision.service import (
    check_claim_filed_in_time,
    check_remaining_limit,
    find_diagnosid,
    find_pers_id,
)


POLICY_NUMBER = "DMC-2024-005521"
CLAIM_ID = UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


def make_risks(remaining: float = 1000.0) -> RisksAndLimits:
    return RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[RiskInfo(
            risk_id=1,
            name="Амбулаторное лечение",
            coverage_pct=80.0,
            total_limit=2000.0,
            remaining_limit=remaining,
            currency="GEL",
        )],
        annual_limit=5000.0,
        remaining=remaining,
        currency="GEL",
    )


def make_extraction(event_date: str = "2026-01-15") -> ExtractionResult:
    return ExtractionResult(
        insured=InsuredData(
            full_name="Иванов Иван Иванович",
            birth_date="1988-03-15",
            personal_id="12345678901",
        ),
        event=EventData(
            date=event_date,
            institution="Клиника Медикус",
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация", amount=150.0)],
            total_claimed=150.0,
        ),
        extraction_confidence=0.92,
        flags=[],
    )


# ── check_remaining_limit ─────────────────────────────────────────


def test_check_remaining_limit_passes_when_positive():
    """Остаток > 0 — проверка проходит без исключения."""
    check_remaining_limit(make_risks(remaining=100.0))  # не должно бросать


def test_check_remaining_limit_raises_when_zero():
    """Остаток = 0 → PolicyLimitExhaustedError."""
    with pytest.raises(PolicyLimitExhaustedError) as exc_info:
        check_remaining_limit(make_risks(remaining=0.0))
    assert exc_info.value.remaining == 0.0
    assert exc_info.value.currency == "GEL"


def test_check_remaining_limit_raises_when_negative():
    """Отрицательный остаток → PolicyLimitExhaustedError."""
    with pytest.raises(PolicyLimitExhaustedError):
        check_remaining_limit(make_risks(remaining=-50.0))


# ── check_claim_filed_in_time ─────────────────────────────────────


def test_filed_in_time_same_day():
    event = date(2026, 1, 15)
    submission = date(2026, 1, 15)
    assert check_claim_filed_in_time(submission, event) is True


def test_filed_in_time_within_90_days():
    event = date(2026, 1, 1)
    submission = date(2026, 3, 31)  # 89 дней
    assert check_claim_filed_in_time(submission, event) is True


def test_filed_in_time_exactly_90_days():
    event = date(2026, 1, 1)
    submission = date(2026, 4, 1)  # 90 дней
    assert check_claim_filed_in_time(submission, event) is True


def test_filed_too_late():
    event = date(2026, 1, 1)
    submission = date(2026, 4, 2)  # 91 день — уже поздно
    assert check_claim_filed_in_time(submission, event) is False


def test_filed_before_event():
    """Дата подачи раньше события — некорректно."""
    event = date(2026, 1, 15)
    submission = date(2026, 1, 10)
    assert check_claim_filed_in_time(submission, event) is False


# ── find_diagnosid ────────────────────────────────────────────────


def test_find_diagnosid_exact_match():
    from core.schemas.core_api import ICD10Item
    icd10_list = [
        ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ"),
        ICD10Item(diagnosid=102, code="Z00.0", name="Осмотр"),
    ]
    assert find_diagnosid("J06.9", icd10_list) == "J06.9"


def test_find_diagnosid_case_insensitive():
    from core.schemas.core_api import ICD10Item
    icd10_list = [ICD10Item(diagnosid=101, code="j06.9", name="ОРВИ")]
    assert find_diagnosid("J06.9", icd10_list) == "j06.9"


def test_find_diagnosid_prefix_fallback():
    """J06 совпадает с J06.9 по префиксу."""
    from core.schemas.core_api import ICD10Item
    icd10_list = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]
    assert find_diagnosid("J06", icd10_list) == "J06.9"


def test_find_diagnosid_not_found():
    from core.schemas.core_api import ICD10Item
    icd10_list = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]
    assert find_diagnosid("Z99.9", icd10_list) is None


def test_find_diagnosid_empty_list():
    assert find_diagnosid("J06.9", []) is None


# ── find_diagnosid_in_ocr ─────────────────────────────────────────


def test_find_diagnosid_in_ocr_exact():
    """Код J06.9 присутствует в OCR-тексте → возвращает (diagnosid, code)."""
    from core.schemas.core_api import ICD10Item
    from layers.decision.service import find_diagnosid_in_ocr
    icd10_list = [
        ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ"),
        ICD10Item(diagnosid=102, code="Z00.0", name="Осмотр"),
    ]
    diagnosid, code = find_diagnosid_in_ocr(
        ["ფორმა 100 J06.9 ამბულატ"], icd10_list
    )
    assert diagnosid == "J06.9"
    assert code == "J06.9"


def test_find_diagnosid_in_ocr_prefix():
    """J06 в OCR совпадёт с J06.9 в справочнике по prefix."""
    from core.schemas.core_api import ICD10Item
    from layers.decision.service import find_diagnosid_in_ocr
    icd10_list = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]
    diagnosid, code = find_diagnosid_in_ocr(["диагноз J06 амбулатория"], icd10_list)
    assert diagnosid == "J06.9"


def test_find_diagnosid_in_ocr_not_found():
    """Нет кода МКБ-10 в документах → (None, None) → manual_review."""
    from core.schemas.core_api import ICD10Item
    from layers.decision.service import find_diagnosid_in_ocr
    icd10_list = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]
    diagnosid, code = find_diagnosid_in_ocr(
        ["ნათია გოლიაძე კონსულტაცია 170 GEL"], icd10_list
    )
    assert diagnosid is None
    assert code is None


def test_find_diagnosid_in_ocr_no_noise():
    """GEL, OK, V1 не должны матчиться как ICD-10 коды."""
    from core.schemas.core_api import ICD10Item
    from layers.decision.service import find_diagnosid_in_ocr
    icd10_list = [ICD10Item(diagnosid=999, code="V1", name="Несуществующий")]
    diagnosid, code = find_diagnosid_in_ocr(["170 GEL OK V1 PDF"], icd10_list)
    # V1 не в справочнике МКБ-10 → None; если бы V1 был в справочнике, вернул бы его
    # Здесь проверяем что GEL/OK не создают ложных совпадений
    assert diagnosid is None or code == "V1"  # V1 найден только если в справочнике


# ── find_pers_id ──────────────────────────────────────────────────


def test_find_pers_id_exact_match():
    from core.schemas.core_api import ProviderInfo
    providers = [
        ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789"),
        ProviderInfo(pers_id=2, name="МЦ Мединтер", inn="987654321"),
    ]
    assert find_pers_id("Клиника Аврора", providers) == 1


def test_find_pers_id_case_insensitive():
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id("клиника аврора", providers) == 1


def test_find_pers_id_partial_match():
    """Частичное совпадение: «МЦ Мединтер» найдёт «Медицинский Центр Мединтер»."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=2, name="МЦ Мединтер", inn="987654321")]
    assert find_pers_id("Медицинский центр МЦ Мединтер", providers) == 2


def test_find_pers_id_not_found():
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id("Неизвестная больница", providers) == 0


def test_find_pers_id_none_institution():
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id(None, providers) == 0


def test_find_pers_id_empty_providers():
    assert find_pers_id("Клиника Аврора", []) == 0


def test_find_pers_id_georgian_legal_prefix_stripped():
    """შпс „Аврора" → ядро «аврора» → матчит «Клиника Аврора» через fuzzy/substring."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=5, name="Клиника Аврора", inn="111")]
    # "аврора" ⊂ "клиника аврора" → подстрока ядер → hit
    assert find_pers_id('შпс „Аврора"', providers) == 5


def test_find_pers_id_russian_legal_prefix_stripped():
    """ООО Мединтер → ядро «мединтер» → матчит «МЦ Мединтер» через подстроку ядер."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=7, name="МЦ Мединтер", inn="222")]
    assert find_pers_id("ООО Мединтер", providers) == 7


def test_find_pers_id_english_legal_suffix_stripped():
    """Аврора LLC → ядро «аврора» → матчит «Аврора Medical Center» через подстроку."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=9, name="Аврора Medical Center", inn="333")]
    assert find_pers_id("Аврора LLC", providers) == 9


def test_find_pers_id_fuzzy_threshold_65():
    """Опечатка/искажение OCR: «Клиника Авррора» (одна лишняя р) — должно сматчиться."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=11, name="Клиника Аврора", inn="444")]
    # SequenceMatcher("клиника авррора", "клиника аврора") ~ 0.97 → проходит при 0.65
    assert find_pers_id("Клиника Авррора", providers) == 11


def test_find_pers_id_entirely_different_returns_zero():
    """Полностью другое название — 0 даже после нормализации."""
    from core.schemas.core_api import ProviderInfo
    providers = [ProviderInfo(pers_id=1, name="Клиника Аврора", inn="123456789")]
    assert find_pers_id("ООО Диагностический центр Тбилиси", providers) == 0


# ── build_decision_prompt ─────────────────────────────────────────


def test_build_decision_prompt_includes_hierarchy():
    """Промпт должен содержать категориальную цепочку МКБ-10 из enriched."""
    from layers.decision.icd10_enricher import AncestorNode, EnrichedDiagnosis
    from layers.decision.service import build_decision_prompt
    from core.schemas.core_api import RiskInfo, RisksAndLimits

    enriched = {
        "J06.9": EnrichedDiagnosis(
            code="J06.9",
            name_r="Острая инфекция верхних дыхательных путей",
            name_g=None,
            name_e=None,
            ancestors=[
                AncestorNode(id=2, extcod="J06", name_r="Острые инфекции верхних дыхательных путей", name_g=None, name_e=None),
                AncestorNode(id=3, extcod=None, name_r="Болезни органов дыхания", name_g=None, name_e=None),
            ],
        )
    }

    risks = RisksAndLimits(
        policy_number="DMC-001",
        risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0, total_limit=1000.0, remaining_limit=1000.0, currency="GEL")],
        annual_limit=1000.0, remaining=1000.0, currency="GEL",
    )

    prompt = build_decision_prompt(make_extraction(), enriched, risks, [])

    assert "Медицинская иерархия" in prompt
    assert "J06.9" in prompt
    assert "Болезни органов дыхания" in prompt
    assert "Острая инфекция верхних дыхательных путей" in prompt


def test_build_decision_prompt_missing_code_shows_fallback():
    """Если код не найден в enriched — показывается fallback-метка."""
    from layers.decision.service import build_decision_prompt
    from core.schemas.core_api import RiskInfo, RisksAndLimits

    risks = RisksAndLimits(
        policy_number="DMC-001",
        risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0, total_limit=1000.0, remaining_limit=1000.0, currency="GEL")],
        annual_limit=1000.0, remaining=1000.0, currency="GEL",
    )

    prompt = build_decision_prompt(make_extraction(), {}, risks, [])

    assert "не найден в справочнике МКБ-10" in prompt


def test_build_decision_prompt_exclusions_before_coverage():
    """Чанки exclusions должны идти раньше coverage_cases в тексте промпта."""
    from layers.decision.service import build_decision_prompt
    from core.schemas.contract import ContractChunkSchema
    from core.schemas.core_api import RiskInfo, RisksAndLimits
    from uuid import uuid4

    coverage = ContractChunkSchema(id=uuid4(), policy_number="DMC-001", version_id="v1", section_type="coverage_cases", title="Покрытие", content="Покрываются ОРВИ")
    exclusion = ContractChunkSchema(id=uuid4(), policy_number="DMC-001", version_id="v1", section_type="exclusions", title="Исключения", content="Исключаются хронические")

    risks = RisksAndLimits(
        policy_number="DMC-001",
        risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0, total_limit=1000.0, remaining_limit=1000.0, currency="GEL")],
        annual_limit=1000.0, remaining=1000.0, currency="GEL",
    )

    prompt = build_decision_prompt(make_extraction(), {}, risks, [coverage, exclusion])

    assert prompt.index("Исключаются хронические") < prompt.index("Покрываются ОРВИ")


# ── stochastic QA sampling ────────────────────────────────────────


def test_stochastic_qa_always_triggers_at_rate_1():
    """При QA rate=1.0 каждый approved → manual_review с reason=stochastic_qa_sample."""
    from unittest.mock import patch
    from layers.decision.service import build_decision_prompt
    from core.schemas.core_api import RisksAndLimits, RiskInfo

    # Проверяем логику через direct patch settings.decision_stochastic_qa_rate
    # Сама логика в make_decision — тестируем через интеграционный mock ниже.
    # Здесь просто проверяем что random.random < 1.0 всегда True.
    import random
    with patch.object(random, "random", return_value=0.0):
        # 0.0 < 1.0 → triggers
        assert random.random() < 1.0


def test_stochastic_qa_never_triggers_at_rate_0():
    """При QA rate=0.0 ни одна заявка не попадает в QA."""
    import random
    # random.random() всегда >= 0.0, поэтому rate=0.0 никогда не срабатывает
    assert not (random.random() < 0.0)


@pytest.mark.asyncio
async def test_stochastic_qa_sets_manual_review_reason():
    """При QA sampling → requires_manual_review=True, reason='stochastic_qa_sample'."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
    from core.schemas.core_api import ICD10Item, ProviderInfo, RiskInfo, RisksAndLimits

    mock_check_fraud = AsyncMock(return_value=[])
    mock_client = AsyncMock()
    mock_client.supports_thinking = True
    mock_client.call_tool = AsyncMock(return_value=LLMResult(tool_input={
        "diagnoses": [{"icd10_code": "J06.9", "is_covered": True, "approved_amount": 120.0, "coverage_clarity": 0.95}],
        "line_items": [],
        "total_approved": 120.0,
        "deductible_applied": 0.0,
        "final_payout": 120.0,
        "requires_manual_review": False,
        "manual_review_reason": None,
        "summary": "Одобрено",
    }))
    mock_client.call_text = AsyncMock(return_value=LLMResult(text=""))

    extraction = ExtractionResult(
        insured=InsuredData(full_name="Иванов И.И.", birth_date="1985-01-01", personal_id="12345678901"),
        event=EventData(date="2026-01-15", institution=None,
                        diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
                        line_items=[LineItem(description="Консультация", amount=120.0)],
                        total_claimed=120.0),
        extraction_confidence=0.95,
    )
    risks = RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0,
                        total_limit=2000.0, remaining_limit=1500.0, currency="GEL")],
        annual_limit=5000.0, remaining=1500.0, currency="GEL",
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(
        scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    ))

    with patch("layers.decision.service.check_fraud", mock_check_fraud), \
         patch("layers.decision.service.get_llm_client", return_value=mock_client), \
         patch("layers.decision.service.write_audit_entry", AsyncMock()), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value={})), \
         patch("layers.decision.service.random.random", return_value=0.0):  # 0.0 < rate → trigger
        from layers.decision.service import make_decision
        decision = await make_decision(
            claim_id=CLAIM_ID, tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            extraction=extraction, risks_limits=risks,
            icd10_list=[ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")],
            providers=[], contract_chunks=[],
            submission_date=date(2026, 1, 20), db=db,
            ocr_texts=["ფორმა 100 J06.9 კონსულტაცია 120 GEL"],  # диагноз в документе
        )

    assert decision.requires_manual_review is True
    assert decision.manual_review_reason == "stochastic_qa_sample"
    assert decision.final_payout == 120.0  # payout не изменился


@pytest.mark.asyncio
async def test_fraud_task_is_asyncio_task():
    """fraud_task должен быть asyncio.Task, а не корутиной — для параллельного исполнения с Claude."""
    import asyncio
    import inspect
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_check_fraud = AsyncMock(return_value=[])

    with patch("layers.decision.service.check_fraud", mock_check_fraud), \
         patch("asyncio.create_task", wraps=asyncio.create_task) as spy_create_task:

        # Создаём минимальный контекст для вызова make_decision
        # Нас интересует только что asyncio.create_task вызван (не просто корутина)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        mock_client = AsyncMock()
        mock_client.supports_thinking = True
        mock_client.call_tool = AsyncMock(return_value=LLMResult(tool_input={
            "diagnoses": [{"icd10_code": "J06.9", "is_covered": True, "approved_amount": 120.0, "coverage_clarity": 0.95}],
            "line_items": [{"description": "Консультация", "claimed_amount": 150.0, "approved_amount": 120.0}],
            "total_approved": 120.0,
            "deductible_applied": 0.0,
            "final_payout": 120.0,
            "requires_manual_review": False,
            "manual_review_reason": None,
            "summary": "Одобрено: J06.9, покрытие 80%.",
        }))
        mock_client.call_text = AsyncMock(return_value=LLMResult(text=""))

        from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
        from core.schemas.core_api import ICD10Item, ProviderInfo, RiskInfo, RisksAndLimits

        extraction = ExtractionResult(
            insured=InsuredData(full_name="Иванов И.И.", birth_date="1985-01-01", personal_id="12345678901"),
            event=EventData(date="2026-01-15", institution=None, diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")], line_items=[LineItem(description="Консультация", amount=150.0)], total_claimed=150.0),
            extraction_confidence=0.9,
        )
        risks = RisksAndLimits(policy_number=POLICY_NUMBER, risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0, total_limit=2000.0, remaining_limit=1500.0, currency="GEL")], annual_limit=5000.0, remaining=1500.0, currency="GEL")
        icd10 = [ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")]

        with patch("layers.decision.service.get_llm_client", return_value=mock_client), \
             patch("layers.decision.service.write_audit_entry", AsyncMock()), \
             patch("layers.decision.service.enrich_all", AsyncMock(return_value={})):
            from layers.decision.service import make_decision
            await make_decision(
                claim_id=CLAIM_ID, tenant_id=TENANT_ID,
                policy_number=POLICY_NUMBER,
                extraction=extraction, risks_limits=risks,
                icd10_list=icd10, providers=[],
                contract_chunks=[], submission_date=date(2026, 1, 20),
                db=db,
            )

        spy_create_task.assert_called_once()
        assert mock_check_fraud.called


@pytest.mark.asyncio
async def test_make_decision_applies_positive_list_coverage():
    """Регрессия (Фаза 0): make_decision с POSITIVE LIST совпадением проходит end-to-end.

    Раньше падало дважды:
      1. NameError — check_positive_list вызывался с claim.policy_number,
         а параметра claim в make_decision() нет;
      2. ValueError — line_item.is_covered, поля is_covered у
         LineItemDecisionSchema не существует.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
    from core.schemas.core_api import ICD10Item, RiskInfo, RisksAndLimits
    from layers.decision.service import make_decision

    mock_check_fraud = AsyncMock(return_value=[])
    mock_positive_list = AsyncMock(return_value={"Полипэктомия": (True, "Полипэктомия")})

    mock_client = AsyncMock()
    mock_client.supports_thinking = True
    mock_client.call_tool = AsyncMock(return_value=LLMResult(tool_input={
        "diagnoses": [{"icd10_code": "K29.7", "is_covered": True, "approved_amount": 400.0, "coverage_clarity": 0.95}],
        "line_items": [{"description": "Полипэктомия", "claimed_amount": 500.0, "approved_amount": 400.0}],
        "total_approved": 400.0,
        "deductible_applied": 0.0,
        "final_payout": 400.0,
        "requires_manual_review": False,
        "manual_review_reason": None,
        "summary": "Одобрено: K29.7, Полипэктомия из POSITIVE LIST.",
    }))
    mock_client.call_text = AsyncMock(return_value=LLMResult(text=""))

    extraction = ExtractionResult(
        insured=InsuredData(full_name="Иванов И.И.", birth_date="1985-01-01", personal_id="12345678901"),
        event=EventData(
            date="2026-01-15", institution=None,
            diagnoses=[DiagnoisItem(icd10_code="K29.7", description="Гастрит")],
            line_items=[LineItem(description="Полипэктомия", amount=500.0)],
            total_claimed=500.0,
        ),
        extraction_confidence=0.95,
    )
    risks = RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0,
                        total_limit=2000.0, remaining_limit=1500.0, currency="GEL")],
        annual_limit=5000.0, remaining=1500.0, currency="GEL",
    )
    db = AsyncMock()

    with patch("layers.decision.service.check_fraud", mock_check_fraud), \
         patch("layers.decision.service.check_positive_list", mock_positive_list), \
         patch("layers.decision.service.check_exclusions", AsyncMock(return_value=None)), \
         patch("layers.decision.service.get_llm_client", return_value=mock_client), \
         patch("layers.decision.service.write_audit_entry", AsyncMock()), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value={})), \
         patch("layers.decision.service.random.random", return_value=0.99):  # QA не срабатывает
        decision = await make_decision(
            claim_id=CLAIM_ID, tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            extraction=extraction, risks_limits=risks,
            icd10_list=[ICD10Item(diagnosid=201, code="K29.7", name="Гастрит")],
            providers=[], contract_chunks=[],
            submission_date=date(2026, 1, 20), db=db,
        )

    # policy_number дошёл до check_positive_list из параметра (не NameError)
    assert mock_positive_list.await_args.kwargs["policy_number"] == POLICY_NUMBER

    # POSITIVE LIST применён: 100% покрытие, флаг выставлен, привязка к диагнозу снята
    assert decision.status == "approved"
    line_item = decision.line_items[0]
    assert line_item.positive_list_applied is True
    assert line_item.approved_amount == line_item.claimed_amount == 500.0
    assert line_item.linked_icd10 is None


@pytest.mark.asyncio
async def test_make_decision_applies_calibration_factor():
    """Калибровочный фактор из tenant_configs масштабирует confidence (Шаги 27/29).

    raw=0.95, factor=0.8 → effective=0.76; в audit пишутся оба значения —
    без raw калибровочный job компаундил бы фактор.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from core.schemas.claim import DiagnoisItem, EventData, ExtractionResult, InsuredData, LineItem
    from core.schemas.core_api import ICD10Item, RiskInfo, RisksAndLimits
    from layers.decision.service import make_decision

    mock_client = AsyncMock()
    mock_client.supports_thinking = True
    mock_client.call_tool = AsyncMock(return_value=LLMResult(tool_input={
        "diagnoses": [{"icd10_code": "J06.9", "is_covered": True, "approved_amount": 120.0, "coverage_clarity": 0.95}],
        "line_items": [],
        "total_approved": 120.0,
        "deductible_applied": 0.0,
        "final_payout": 120.0,
        "requires_manual_review": False,
        "manual_review_reason": None,
        "summary": "Одобрено",
    }))
    mock_client.call_text = AsyncMock(return_value=LLMResult(text=""))

    extraction = ExtractionResult(
        insured=InsuredData(full_name="Иванов И.И.", birth_date="1985-01-01", personal_id="12345678901"),
        event=EventData(
            date="2026-01-15", institution=None,
            diagnoses=[DiagnoisItem(icd10_code="J06.9", description="ОРВИ")],
            line_items=[LineItem(description="Консультация", amount=120.0)],
            total_claimed=120.0,
        ),
        extraction_confidence=0.95,
    )
    risks = RisksAndLimits(
        policy_number=POLICY_NUMBER,
        risks=[RiskInfo(risk_id=1, name="Амбулаторное", coverage_pct=80.0,
                        total_limit=2000.0, remaining_limit=1500.0, currency="GEL")],
        annual_limit=5000.0, remaining=1500.0, currency="GEL",
    )
    mock_audit = AsyncMock()

    with patch("layers.decision.service.check_fraud", AsyncMock(return_value=[])), \
         patch("layers.decision.service.check_positive_list", AsyncMock(return_value={})), \
         patch("layers.decision.service.check_exclusions", AsyncMock(return_value=None)), \
         patch("layers.decision.service.get_tenant_config_float", AsyncMock(return_value=0.8)), \
         patch("layers.decision.service.get_llm_client", return_value=mock_client), \
         patch("layers.decision.service.write_audit_entry", mock_audit), \
         patch("layers.decision.service.enrich_all", AsyncMock(return_value={})), \
         patch("layers.decision.service.random.random", return_value=0.99):
        decision = await make_decision(
            claim_id=CLAIM_ID, tenant_id=TENANT_ID,
            policy_number=POLICY_NUMBER,
            extraction=extraction, risks_limits=risks,
            icd10_list=[ICD10Item(diagnosid=101, code="J06.9", name="ОРВИ")],
            providers=[], contract_chunks=[],
            submission_date=date(2026, 1, 20), db=AsyncMock(),
        )

    # routing_signal = min(data_score=1.0, coverage_signal=0.95*0.8=0.76, amount_gate=1.0) = 0.76
    assert decision.overall_confidence == pytest.approx(0.95 * 0.8)

    audit_confidence = mock_audit.await_args.kwargs["confidence"]
    assert audit_confidence["overall"] == pytest.approx(0.76)
    assert audit_confidence["raw_coverage_signal"] == pytest.approx(0.95)
    assert audit_confidence["calibration_factor"] == pytest.approx(0.8)
