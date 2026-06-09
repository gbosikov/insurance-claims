"""Unit-тесты для парсинга POSITIVE LIST из контрактов."""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from layers.rag.indexer import (
    parse_positive_list_with_claude,
    create_positive_list_records,
)


class TestPositiveListParsing:
    """Тесты для парсинга POSITIVE LIST через Claude."""

    @pytest.mark.asyncio
    async def test_parse_positive_list_success(self):
        """Тест успешного парсинга POSITIVE LIST."""
        contract_text = """
        1.7.3. აკმეზი დაფარული პროცედურები:
        - პოლიპექტომია (polypectomy) - კოდი 45.92
        - ადენოიდექტომია (adenoidectomy) - სრული ანესთეზია
        """

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {
                "procedure_name_ka": "პოლიპექტომია",
                "procedure_name_ru": "Полипэктомия",
                "procedure_name_en": "Polypectomy",
                "procedure_code": "45.92",
                "coverage_percent": 100.0,
                "sublimit": None,
                "section_reference": "1.7.3"
            },
            {
                "procedure_name_ka": "ადენოიდექტომია",
                "procedure_name_ru": "Аденоидэктомия",
                "procedure_name_en": "Adenoidectomy",
                "procedure_code": "28.3",
                "coverage_percent": 100.0,
                "sublimit": None,
                "section_reference": "1.7.3"
            }
        ]))]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_positive_list_with_claude(contract_text)

            assert len(result) == 2
            assert result[0]["procedure_name_ka"] == "პოლიპექტომია"
            assert result[0]["procedure_code"] == "45.92"
            assert result[1]["procedure_name_ru"] == "Аденоидэктомия"

    @pytest.mark.asyncio
    async def test_parse_positive_list_empty(self):
        """Если в контракте нет POSITIVE LIST, возвращаем пустой список."""
        contract_text = "Обычный текст контракта без POSITIVE LIST."

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_positive_list_with_claude(contract_text)

            assert result == []

    @pytest.mark.asyncio
    async def test_parse_positive_list_invalid_json(self):
        """Если Claude вернул невалидный JSON, логируем ошибку и возвращаем []."""
        contract_text = "Some contract text"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Not valid JSON {")]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_positive_list_with_claude(contract_text)

            assert result == []

    @pytest.mark.asyncio
    async def test_parse_positive_list_api_error(self):
        """При ошибке API логируем и возвращаем []."""
        contract_text = "Contract text"

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(side_effect=Exception("API Error"))

            result = await parse_positive_list_with_claude(contract_text)

            assert result == []


class TestCreatePositiveListRecords:
    """Тесты для сохранения POSITIVE LIST в БД."""

    @pytest.mark.asyncio
    async def test_create_positive_list_records_success(self):
        """Тест успешного сохранения процедур."""
        procedures = [
            {
                "procedure_name_ka": "პოლიპექტომია",
                "procedure_name_ru": "Полипэктомия",
                "procedure_name_en": "Polypectomy",
                "procedure_code": "45.92",
                "coverage_percent": 100.0,
                "sublimit": None,
                "section_reference": "1.7.3"
            }
        ]

        mock_db = AsyncMock()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        result = await create_positive_list_records(
            procedures,
            tenant_id=tenant_id,
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result == 1
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_positive_list_records_empty(self):
        """Если процедур нет, ничего не сохраняем."""
        mock_db = AsyncMock()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        result = await create_positive_list_records(
            [],
            tenant_id=tenant_id,
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result == 0
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_positive_list_records_skip_without_ka_name(self):
        """Пропускаем процедуры без грузинского названия."""
        procedures = [
            {
                "procedure_name_ka": "",  # пусто!
                "procedure_name_ru": "Полипэктомия",
                "section_reference": "1.7.3"
            },
            {
                "procedure_name_ka": "პოლიპექტომია",  # OK
                "procedure_name_ru": "Полипэктомия",
                "section_reference": "1.7.3"
            }
        ]

        mock_db = AsyncMock()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        result = await create_positive_list_records(
            procedures,
            tenant_id=tenant_id,
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        # Только одна процедура должна быть сохранена
        assert result == 1

    @pytest.mark.asyncio
    async def test_create_positive_list_records_multiple(self):
        """Тест сохранения нескольких процедур."""
        procedures = [
            {
                "procedure_name_ka": "პოლიპექტომია",
                "procedure_name_ru": "Полипэктомия",
                "section_reference": "1.7.3"
            },
            {
                "procedure_name_ka": "ადენოიდექტომია",
                "procedure_name_ru": "Аденоидэктомия",
                "section_reference": "1.7.3"
            },
            {
                "procedure_name_ka": "სტენტირება",
                "procedure_name_ru": "Стентирование",
                "section_reference": "1.7.4"
            }
        ]

        mock_db = AsyncMock()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        result = await create_positive_list_records(
            procedures,
            tenant_id=tenant_id,
            policy_number="POL-001",
            version_id="v1",
            db=mock_db,
        )

        assert result == 3
        mock_db.execute.assert_called()


class TestPositiveListIntegration:
    """Интеграционные тесты POSITIVE LIST."""

    def test_positive_list_structure_schema(self):
        """Проверить что структура процедуры соответствует ожиданиям."""
        procedure = {
            "procedure_name_ka": "პოლიპექტომია",
            "procedure_name_ru": "Полипэктомия",
            "procedure_name_en": "Polypectomy",
            "procedure_code": "45.92",
            "coverage_percent": 100.0,
            "sublimit": 5000.0,
            "section_reference": "1.7.3"
        }

        # Проверяем все обязательные поля
        assert procedure["procedure_name_ka"]
        assert procedure["coverage_percent"] == 100.0
        assert procedure["section_reference"]

    def test_positive_list_always_100_percent_coverage(self):
        """POSITIVE LIST процедуры всегда имеют 100% покрытие (по умолчанию)."""
        procedure = {
            "procedure_name_ka": "პოლიპექტომია",
            "procedure_name_ru": "Полипэктомия",
            "coverage_percent": 100.0,  # ← ВСЕГДА 100%
        }

        # Если in POSITIVE LIST → approved_amount = claimed_amount * coverage_percent
        claimed = 500.0
        approved = claimed * (procedure["coverage_percent"] / 100.0)
        assert approved == 500.0
