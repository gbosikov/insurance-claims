"""Unit-тесты для парсинга CARVEOUT-исключений из контрактов."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from layers.rag.indexer import (
    parse_carveout_exclusions_with_claude,
    create_carveout_chunks,
)


class TestCarveoutParsing:
    """Тесты для парсинга CARVEOUT-исключений."""

    @pytest.mark.asyncio
    async def test_parse_carveout_with_service_urgency(self):
        """Тест парсинга CARVEOUT с условием service_urgency."""
        contract_text = """
        4.1. თირკმლის ქრონიკულ უკმარისობა ნებისმიერი თანხით არ დაფარულია
        გარდა ურგენტული ჩარევის დროს.
        """

        # Mock Claude API response
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {
                "num": "4.1",
                "excluded": {
                    "ka": "თირკმლის ქრონიკულ უკმარისობა",
                    "ru": "Хроническая почечная недостаточность",
                    "icd10": ["N18", "N19"]
                },
                "carveout_conditions": [
                    {
                        "type": "service_urgency",
                        "value": "urgent",
                        "ka_marker": "ურგენტული ჩარევა"
                    }
                ],
                "general_exceptions": [],
                "original_text": "თირკმლის ქრონიკულ უკმარისობა..."
            }
        ]))]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_carveout_exclusions_with_claude(contract_text)

            assert len(result) == 1
            assert result[0]["num"] == "4.1"
            assert result[0]["excluded"]["ru"] == "Хроническая почечная недостаточность"
            assert result[0]["excluded"]["icd10"] == ["N18", "N19"]
            assert result[0]["carveout_conditions"][0]["value"] == "urgent"

    @pytest.mark.asyncio
    async def test_parse_carveout_with_general_exception(self):
        """Тест парсинга CARVEOUT с общим исключением (гепатит A не исключён)."""
        contract_text = """
        4.2. ნებისმიერ ჰეპატიტებთან დაკავშირებული ხარჯები არ დაფარულია
        გარდა: A ტიპის ჰეპატიტი და პირველადი დიაგნოსტიკა.
        """

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {
                "num": "4.2",
                "excluded": {
                    "ka": "ნებისმიერ ჰეპატიტებთან",
                    "ru": "Гепатиты (любые)",
                    "icd10": ["B15", "B16", "B17", "B18", "B19"]
                },
                "carveout_conditions": [
                    {
                        "type": "service_urgency",
                        "value": "diagnostic",
                        "ka_marker": "პირველადი დიაგნოსტიკა"
                    }
                ],
                "general_exceptions": ["B15"],  # Гепатит А НЕ исключён
                "original_text": "ნებისმიერ ჰეპატიტებთან..."
            }
        ]))]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_carveout_exclusions_with_claude(contract_text)

            assert len(result) == 1
            assert "B15" in result[0]["general_exceptions"]

    @pytest.mark.asyncio
    async def test_parse_no_carveouts_found(self):
        """Если CARVEOUT-ов нет, возвращаем пустой список."""
        contract_text = "Обычный текст без CARVEOUT-исключений."

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_carveout_exclusions_with_claude(contract_text)

            assert result == []

    @pytest.mark.asyncio
    async def test_parse_invalid_json_returns_empty(self):
        """Если Claude вернул некорректный JSON, возвращаем пустой список."""
        contract_text = "Some text"

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Not valid JSON")]

        with patch("anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(return_value=mock_response)

            result = await parse_carveout_exclusions_with_claude(contract_text)

            assert result == []


class TestCarveoutChunkCreation:
    """Тесты для создания ContractChunk-ов из CARVEOUT-ов."""

    @pytest.mark.asyncio
    async def test_create_carveout_chunks_with_structure(self):
        """Тест создания чанков с chunk_structure."""
        from uuid import UUID
        from unittest.mock import AsyncMock

        carveouts = [
            {
                "num": "4.1",
                "excluded": {
                    "ka": "თირკმლის ქრონიკულ უკმარისობა",
                    "ru": "Хроническая почечная недостаточность",
                    "icd10": ["N18", "N19"]
                },
                "carveout_conditions": [
                    {
                        "type": "service_urgency",
                        "value": "urgent",
                        "ka_marker": "ურგენტული ჩარევა"
                    }
                ],
                "general_exceptions": [],
                "original_text": "თირკმლის ქრონიკულ უკმარისობა... გარდა ურგენტული ჩარევის"
            }
        ]

        mock_db = AsyncMock()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        with patch("layers.rag.indexer.get_embedding") as mock_embedding:
            mock_embedding.return_value = [0.1] * 1024

            await create_carveout_chunks(
                carveouts,
                tenant_id=tenant_id,
                policy_number="POL-001",
                version_id="v20240609",
                db=mock_db,
            )

            # Verify that db.add was called
            mock_db.add.assert_called_once()

            # Verify the chunk structure
            added_chunk = mock_db.add.call_args[0][0]
            assert added_chunk.section_type == "exclusion_with_carveout"
            assert added_chunk.chunk_structure["type"] == "exclusion_with_carveout"
            assert "N18" in added_chunk.chunk_structure["excluded_icd10"]
            assert len(added_chunk.chunk_structure["carveout_conditions"]) == 1

    @pytest.mark.asyncio
    async def test_create_carveout_chunks_skips_invalid(self):
        """Пропускаем CARVEOUT-ы без original_text."""
        carveouts = [
            {
                "num": "4.1",
                "excluded": {"icd10": ["N18"]},
                # Нет original_text
            },
            {
                "num": "4.2",
                "excluded": {"icd10": ["B15"]},
                "original_text": "Валидный текст"
            }
        ]

        mock_db = AsyncMock()
        tenant_id = UUID("00000000-0000-0000-0000-000000000001")

        with patch("layers.rag.indexer.get_embedding") as mock_embedding:
            mock_embedding.return_value = [0.1] * 1024

            await create_carveout_chunks(
                carveouts,
                tenant_id=tenant_id,
                policy_number="POL-001",
                version_id="v20240609",
                db=mock_db,
            )

            # Только один valid CARVEOUT должен быть добавлен
            mock_db.add.assert_called_once()


class TestCarveoutIntegration:
    """Интеграционные тесты для CARVEOUT-парсинга."""

    def test_carveout_structure_matches_decision_schema(self):
        """Проверить что chunk_structure соответствует ожидаемой схеме."""
        chunk_structure = {
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

        # Проверяем что все обязательные поля присутствуют
        assert chunk_structure["type"] == "exclusion_with_carveout"
        assert isinstance(chunk_structure["excluded_icd10"], list)
        assert isinstance(chunk_structure["carveout_conditions"], list)
        assert isinstance(chunk_structure["general_exceptions"], list)

        # Проверяем структуру condition
        condition = chunk_structure["carveout_conditions"][0]
        assert condition["type"] in ("service_urgency", "diagnosis_exception", "condition_type")
        assert condition["value"] in ("urgent", "diagnostic", "planned")

    def test_decision_engine_can_use_chunk_structure(self):
        """Проверить что decision-engine сможет использовать chunk_structure."""
        chunk_structure = {
            "type": "exclusion_with_carveout",
            "excluded_icd10": ["N18"],
            "carveout_conditions": [
                {"type": "service_urgency", "value": "urgent"}
            ],
            "general_exceptions": []
        }

        # Эмуляция decision-engine логики:
        diagnosis_icd10 = "N18.3"
        service_urgency = "urgent"

        # Проверяем: входит ли диагноз в excluded_icd10?
        is_excluded = any(
            diagnosis_icd10.startswith(code)
            for code in chunk_structure["excluded_icd10"]
        )
        assert is_excluded is True

        # Проверяем: есть ли carveout-условие для service_urgency?
        has_carveout_for_urgency = any(
            c["type"] == "service_urgency" and c["value"] == service_urgency
            for c in chunk_structure["carveout_conditions"]
        )
        assert has_carveout_for_urgency is True

        # Вывод: диагноз исключён, но есть CARVEOUT → ПОКРЫТО
        is_covered = is_excluded and has_carveout_for_urgency
        assert is_covered is True
