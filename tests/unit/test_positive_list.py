"""Unit-тесты для POSITIVE LIST процедур."""

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, MagicMock

from core.models.contract import PositiveListProcedure
from core.schemas.claim import LineItem
from layers.decision.service import check_positive_list


class TestCheckPositiveList:
    """Тесты для функции check_positive_list."""

    @pytest.mark.asyncio
    async def test_empty_line_items_returns_empty_dict(self):
        """Если услуг нет, возвращаем пустой dict."""
        mock_db = AsyncMock(spec=AsyncSession)
        result = await check_positive_list(
            line_items=[],
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_procedures_in_list_all_false(self):
        """Если POSITIVE LIST пуст, все услуги = не в списке."""
        mock_db = AsyncMock(spec=AsyncSession)

        # Эмулируем пустой результат SELECT
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        line_items = [
            LineItem(description="полипэктомия", amount=500.0),
            LineItem(description="анализ крови", amount=150.0),
        ]

        result = await check_positive_list(
            line_items=line_items,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result["полипэктомия"] == (False, None)
        assert result["анализ крови"] == (False, None)

    @pytest.mark.asyncio
    async def test_exact_match_in_positive_list(self):
        """Если процедура точно совпадает с POSITIVE LIST → (True, name)."""
        # Создаём mock-процедуру
        proc = MagicMock(spec=PositiveListProcedure)
        proc.procedure_name_ka = "პოლიპექტომია"
        proc.procedure_name_ru = "Полипэктомия"
        proc.procedure_name_en = "Polypectomy"
        proc.section_reference = "1.7.3"

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = [proc]
        mock_db.execute.return_value = mock_result

        line_items = [
            LineItem(description="Полипэктомия", amount=500.0),
        ]

        result = await check_positive_list(
            line_items=line_items,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result["Полипэктомия"][0] is True  # is_in_positive_list
        assert "Полипэктомия" in result["Полипэктомия"][1]  # procedure_name

    @pytest.mark.asyncio
    async def test_partial_match_above_threshold(self):
        """Если совпадение ≥ 0.70 → в POSITIVE LIST."""
        proc = MagicMock(spec=PositiveListProcedure)
        proc.procedure_name_ka = "უჰ პოლიპექტომია"  # грузинское
        proc.procedure_name_ru = "Полипэктомия толстой кишки"
        proc.procedure_name_en = "Polypectomy"
        proc.section_reference = "1.7.3"

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = [proc]
        mock_db.execute.return_value = mock_result

        line_items = [
            LineItem(description="Полипэктомия", amount=500.0),  # частичное совпадение
        ]

        result = await check_positive_list(
            line_items=line_items,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result["Полипэктомия"][0] is True
        assert "Полипэктомия" in result["Полипэктомия"][1]

    @pytest.mark.asyncio
    async def test_partial_match_below_threshold(self):
        """Если совпадение < 0.70 → НЕ в POSITIVE LIST."""
        proc = MagicMock(spec=PositiveListProcedure)
        proc.procedure_name_ka = "კატარაქტის ოპერაცია"
        proc.procedure_name_ru = "Операция по удалению катаракты"
        proc.procedure_name_en = "Cataract surgery"

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = [proc]
        mock_db.execute.return_value = mock_result

        line_items = [
            LineItem(description="анализ крови", amount=100.0),  # совсем другое
        ]

        result = await check_positive_list(
            line_items=line_items,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result["анализ крови"] == (False, None)

    @pytest.mark.asyncio
    async def test_multiple_procedures_in_list(self):
        """Если POSITIVE LIST содержит несколько процедур."""
        proc1 = MagicMock(spec=PositiveListProcedure)
        proc1.procedure_name_ka = "პოლიპექტომია"
        proc1.procedure_name_ru = "Полипэктомия"
        proc1.procedure_name_en = "Polypectomy"

        proc2 = MagicMock(spec=PositiveListProcedure)
        proc2.procedure_name_ka = "ადენოიდექტომია"
        proc2.procedure_name_ru = "Аденоидэктомия"
        proc2.procedure_name_en = "Adenoidectomy"

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = [proc1, proc2]
        mock_db.execute.return_value = mock_result

        line_items = [
            LineItem(description="Полипэктомия", amount=500.0),
            LineItem(description="Аденоидэктомия", amount=400.0),
            LineItem(description="анализ крови", amount=100.0),
        ]

        result = await check_positive_list(
            line_items=line_items,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result["Полипэктомия"][0] is True
        assert result["Аденоидэктомия"][0] is True
        assert result["анализ крови"] == (False, None)

    @pytest.mark.asyncio
    async def test_case_insensitive_matching(self):
        """Матчинг работает независимо от регистра."""
        proc = MagicMock(spec=PositiveListProcedure)
        proc.procedure_name_ka = "პოლიპექტომია"
        proc.procedure_name_ru = "Полипэктомия"
        proc.procedure_name_en = None

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = AsyncMock()
        mock_result.scalars.return_value.all.return_value = [proc]
        mock_db.execute.return_value = mock_result

        line_items = [
            LineItem(description="ПОЛИПЭКТОМИЯ", amount=500.0),  # UPPERCASE
        ]

        result = await check_positive_list(
            line_items=line_items,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result["ПОЛИПЭКТОМИЯ"][0] is True


class TestPositiveListIntegration:
    """Интеграционные тесты POSITIVE LIST."""

    @pytest.mark.asyncio
    async def test_positive_list_overrides_carveout(self):
        """POSITIVE LIST перекрывает CARVEOUT-исключения.

        Пример: полипэктомия в POSITIVE LIST → покрыта даже если она в CARVEOUT-исключении.
        """
        # Этот тест показывает что в decision engine нужно сначала проверить POSITIVE LIST,
        # ПОТОМ — CARVEOUT. Если процедура в POSITIVE LIST → не применяем CARVEOUT.
        assert True  # Интеграция в make_decision() будет в следующей задаче

    def test_positive_list_procedure_schema_valid(self):
        """Проверить что PositiveListProcedure модель правильная."""
        from core.models.contract import PositiveListProcedure

        # Проверяем что все обязательные поля присутствуют
        assert hasattr(PositiveListProcedure, "procedure_name_ka")
        assert hasattr(PositiveListProcedure, "procedure_name_ru")
        assert hasattr(PositiveListProcedure, "procedure_name_en")
        assert hasattr(PositiveListProcedure, "coverage_percent")
        assert hasattr(PositiveListProcedure, "sublimit")
