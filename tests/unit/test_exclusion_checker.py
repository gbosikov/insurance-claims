"""
Unit тесты: layers/decision/exclusion_checker.py

Проверяет:
  - get_insured_type() — тип застрахованного из суффикса CardNumber
  - icd10_matches() — матчинг кодов МКБ-10 к диапазонам вординга
  - apply_wording_carveout() — логика CARVEOUT-условий
  - check_exclusions() — интеграция с DB (mock)
  - db/loaders/load_exclusions.py — парсинг кодов и обнаружение carveout

Данные из реальной таблицы исключений Unison.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from layers.decision.exclusion_checker import (
    ExclusionResult,
    apply_wording_carveout,
    get_insured_type,
    icd10_matches,
)

TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
RULE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ── get_insured_type ──────────────────────────────────────────────


@pytest.mark.parametrize("card_number,expected", [
    ("UNI 700003/1",  "employee"),
    ("UNI 700003/2",  "family"),
    ("UNI 700003/3",  "family"),
    ("UNI 700003/4",  "family"),
    ("UNI 700003",    "employee"),   # без суффикса
    ("MED 536638/1",  "employee"),
    ("MED 536638/2",  "family"),
    ("",              "employee"),   # пустой → сотрудник по умолчанию
    ("/5",            "employee"),   # неизвестный суффикс → сотрудник
])
def test_get_insured_type(card_number, expected):
    assert get_insured_type(card_number) == expected


# ── icd10_matches ─────────────────────────────────────────────────


class TestIcd10Matches:
    """Матчинг к точным кодам."""

    def test_exact_match(self):
        assert icd10_matches("N18", "N18") is True

    def test_subcategory_matches_prefix(self):
        """N18.0 входит в N18."""
        assert icd10_matches("N18.0", "N18") is True
        assert icd10_matches("N18.5", "N18") is True

    def test_different_code_not_matches_prefix(self):
        assert icd10_matches("N19", "N18") is False

    def test_prefix_partial_not_match(self):
        """N1 не является допустимым префиксом для N18."""
        assert icd10_matches("N18", "N1") is False   # нет точки-разделителя

    class TestRanges:
        """Диапазоны вида start-end."""

        def test_psychiatric_range(self):
            """F00-F99: психиатрические состояния."""
            assert icd10_matches("F00", "F00-F99") is True
            assert icd10_matches("F10.3", "F00-F99") is True
            assert icd10_matches("F99", "F00-F99") is True
            assert icd10_matches("G00", "F00-F99") is False

        def test_oncology_range(self):
            """C00-C97: онкология."""
            assert icd10_matches("C34.1", "C00-C97") is True
            assert icd10_matches("C00", "C00-C97") is True
            assert icd10_matches("C97", "C00-C97") is True
            assert icd10_matches("C98", "C00-C97") is False

        def test_benign_neoplasms_range(self):
            """D00-D48."""
            assert icd10_matches("D10", "D00-D48") is True
            assert icd10_matches("D49", "D00-D48") is False

        def test_subcategory_range(self):
            """N18.0-N18.9: хроническая почечная недостаточность."""
            assert icd10_matches("N18.0", "N18.0-N18.9") is True
            assert icd10_matches("N18.5", "N18.0-N18.9") is True
            assert icd10_matches("N18.9", "N18.0-N18.9") is True
            assert icd10_matches("N19",   "N18.0-N18.9") is False
            assert icd10_matches("N17",   "N18.0-N18.9") is False

        def test_en_dash_normalized(self):
            """en-dash (–) нормализуется к ASCII дефису."""
            assert icd10_matches("F10", "F00–F99") is True   # en-dash
            assert icd10_matches("C50", "C00—C97") is True   # em-dash

        def test_congenital_malformations_range(self):
            """Q00-Q99."""
            assert icd10_matches("Q21.1", "Q00-Q99") is True
            assert icd10_matches("R00",   "Q00-Q99") is False

        def test_codes_from_real_exclusion_table(self):
            """Коды из реальной таблицы исключений Unison."""
            # Наркотическая зависимость (включая алкоголь)
            assert icd10_matches("F10.2", "F10-F19") is True
            # ВИЧ/СПИД
            assert icd10_matches("B20",   "B20-B24") is True
            assert icd10_matches("B25",   "B20-B24") is False
            # Врождённые пороки (все)
            assert icd10_matches("Q21.3", "Q00-Q99") is True


# ── apply_wording_carveout ────────────────────────────────────────


def _make_excl(carveout_conditions: list[str]) -> ExclusionResult:
    return ExclusionResult(
        rule_id=RULE_ID,
        description="Хроническая почечная недостаточность",
        scope="all",
        carveout_conditions=carveout_conditions,
    )


class TestApplyWordingCarveout:

    def test_no_carveout_always_excluded(self):
        """Безусловное исключение — всегда отклоняется."""
        excl = _make_excl([])
        is_excluded, reason = apply_wording_carveout(excl, service_urgency=None)
        assert is_excluded is True
        assert reason is not None
        assert "Исключено" in reason

    def test_carveout_urgent_applies(self):
        """CARVEOUT: ургентное → исключение снимается."""
        excl = _make_excl(["urgent", "diagnostic"])
        is_excluded, reason = apply_wording_carveout(excl, service_urgency="urgent")
        assert is_excluded is False
        assert reason is None

    def test_carveout_diagnostic_applies(self):
        """CARVEOUT: диагностика → исключение снимается."""
        excl = _make_excl(["urgent", "diagnostic"])
        is_excluded, reason = apply_wording_carveout(excl, service_urgency="diagnostic")
        assert is_excluded is False

    def test_carveout_planned_does_not_apply(self):
        """CARVEOUT: плановая услуга → исключение остаётся."""
        excl = _make_excl(["urgent", "diagnostic"])
        is_excluded, reason = apply_wording_carveout(excl, service_urgency="planned")
        assert is_excluded is True
        assert reason is not None
        assert "CARVEOUT" in reason or "carveout" in reason.lower()

    def test_carveout_unknown_urgency_not_excluded(self):
        """CARVEOUT + неизвестная срочность → manual_review (не отказ)."""
        excl = _make_excl(["urgent"])
        is_excluded, reason = apply_wording_carveout(excl, service_urgency=None)
        assert is_excluded is False
        assert reason is None


# ── check_exclusions (DB mock) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_exclusions_employee_only_all_scope():
    """Сотрудник → только правила scope='all'."""
    from layers.decision.exclusion_checker import check_exclusions
    from core.models.exclusion import ExclusionRule

    rule_all = MagicMock(spec=ExclusionRule)
    rule_all.id = RULE_ID
    rule_all.scope = "all"
    rule_all.description = "Психиатрия"
    rule_all.icd10_codes = ["F00-F99"]
    rule_all.carveout_conditions = []

    rule_family = MagicMock(spec=ExclusionRule)
    rule_family.scope = "family"
    rule_family.icd10_codes = ["C00-C97"]
    rule_family.carveout_conditions = []

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [rule_all]
    db.execute = AsyncMock(return_value=mock_result)

    result = await check_exclusions("F10.3", "employee", TENANT_ID, db)
    assert result is not None
    assert result.scope == "all"
    assert result.description == "Психиатрия"


@pytest.mark.asyncio
async def test_check_exclusions_family_matches_family_scope():
    """Член семьи → правила scope='family' тоже применяются."""
    from layers.decision.exclusion_checker import check_exclusions
    from core.models.exclusion import ExclusionRule

    rule_family = MagicMock(spec=ExclusionRule)
    rule_family.id = RULE_ID
    rule_family.scope = "family"
    rule_family.description = "Онкология (члены семьи)"
    rule_family.icd10_codes = ["C00-C97"]
    rule_family.carveout_conditions = []

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [rule_family]
    db.execute = AsyncMock(return_value=mock_result)

    result = await check_exclusions("C34.1", "family", TENANT_ID, db)
    assert result is not None
    assert result.scope == "family"


@pytest.mark.asyncio
async def test_check_exclusions_no_match_returns_none():
    """Неисключённый диагноз → None."""
    from layers.decision.exclusion_checker import check_exclusions
    from core.models.exclusion import ExclusionRule

    rule_all = MagicMock(spec=ExclusionRule)
    rule_all.scope = "all"
    rule_all.icd10_codes = ["F00-F99"]
    rule_all.carveout_conditions = []

    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [rule_all]
    db.execute = AsyncMock(return_value=mock_result)

    result = await check_exclusions("J06.9", "employee", TENANT_ID, db)
    assert result is None


# ── load_exclusions._parse_codes, _detect_carveout ───────────────


def test_parse_codes_comma_separated():
    from db.loaders.load_exclusions import _parse_codes
    result = _parse_codes("F00-F99, G00, H00-H59")
    assert result == ["F00-F99", "G00", "H00-H59"]


def test_parse_codes_normalizes_en_dash():
    from db.loaders.load_exclusions import _parse_codes
    result = _parse_codes("N18.0–N18.9")  # en-dash
    assert result == ["N18.0-N18.9"]


def test_parse_codes_semicolon_separated():
    from db.loaders.load_exclusions import _parse_codes
    result = _parse_codes("C00-C97; D00-D48")
    assert result == ["C00-C97", "D00-D48"]


def test_detect_carveout_urgent_georgian():
    from db.loaders.load_exclusions import _detect_carveout
    desc = "გარდა ურგენტული ჩარევისა"
    result = _detect_carveout(desc)
    assert "urgent" in result


def test_detect_carveout_diagnostic_russian():
    from db.loaders.load_exclusions import _detect_carveout
    desc = "исключается, кроме первичной диагностики"
    result = _detect_carveout(desc)
    assert "diagnostic" in result


def test_detect_carveout_empty_when_no_conditions():
    from db.loaders.load_exclusions import _detect_carveout
    desc = "Психические расстройства и расстройства поведения"
    result = _detect_carveout(desc)
    assert result == []
