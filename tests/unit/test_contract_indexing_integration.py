"""Integration tests for contract indexing with CARVEOUT and POSITIVE LIST parsing."""

from datetime import date
from uuid import UUID
from unittest.mock import AsyncMock, MagicMock, patch
import json

import pytest

from layers.rag.indexer import index_contract, index_contract_from_text


class TestContractIndexingIntegration:
    """Integration tests for full contract indexing pipeline."""

    @pytest.mark.asyncio
    async def test_index_contract_from_text_calls_carveout_parsing(self):
        """index_contract_from_text должна вызвать parse_carveout_exclusions_with_claude."""
        contract_text = """
        4.1. თირკმლის ქრონიკულ უკმარისობა ნებისმიერ თანხით არ დაფარულია გარდა ურგენტული ჩარევის დროს.
        1.7.3. აკმეზი დაფარული პროცედურები: პოლიპექტომია, ადენოიდექტომია.
        """

        mock_db = AsyncMock()
        mock_storage = AsyncMock()

        # Mock existing_version check
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("layers.rag.indexer.parse_carveout_exclusions_with_claude") as mock_carveout, \
             patch("layers.rag.indexer.create_carveout_chunks") as mock_carveout_chunks, \
             patch("layers.rag.indexer.parse_positive_list_with_claude") as mock_positive_list, \
             patch("layers.rag.indexer.create_positive_list_records") as mock_positive_list_records, \
             patch("layers.rag.indexer.chunk_contract_with_claude") as mock_chunk, \
             patch("layers.rag.indexer.get_embedding") as mock_embedding:

            # Setup mocks
            mock_carveout.return_value = [
                {
                    "num": "4.1",
                    "excluded": {"ru": "Хроническая почечная недостаточность", "icd10": ["N18"]},
                    "carveout_conditions": [{"type": "service_urgency", "value": "urgent"}],
                    "general_exceptions": [],
                    "original_text": "Text"
                }
            ]

            mock_positive_list.return_value = [
                {
                    "procedure_name_ka": "პოლიპექტომია",
                    "procedure_name_ru": "Полипэктомия",
                    "section_reference": "1.7.3"
                }
            ]

            mock_positive_list_records.return_value = 1

            mock_chunk.return_value = [
                {
                    "section_type": "general",
                    "title": "General",
                    "content": "Some text",
                    "key_terms": ["term"]
                }
            ]

            mock_embedding.return_value = [0.1] * 1024

            result = await index_contract_from_text(
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                policy_number="POL-001",
                content=contract_text,
                content_hash=None,
                version_label="v1",
                valid_from=date(2024, 6, 9),
                db=mock_db,
                storage=mock_storage,
            )

            # Verify CARVEOUT parsing was called
            mock_carveout.assert_called_once()
            mock_carveout_chunks.assert_called_once()

            # Verify POSITIVE LIST parsing was called
            mock_positive_list.assert_called_once()
            mock_positive_list_records.assert_called_once()

            # Verify regular chunking was called
            mock_chunk.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_contract_from_text_pass_order(self):
        """CARVEOUT и POSITIVE LIST парсинг должны вызваться ДО обычного chunking."""
        contract_text = "Contract text"

        mock_db = AsyncMock()
        mock_storage = AsyncMock()

        # Mock existing_version check
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        call_order = []

        async def track_carveout(*args, **kwargs):
            call_order.append("carveout")
            return []

        async def track_positive_list(*args, **kwargs):
            call_order.append("positive_list")
            return []

        async def track_chunk(*args, **kwargs):
            call_order.append("chunk")
            return []

        with patch("layers.rag.indexer.parse_carveout_exclusions_with_claude", side_effect=track_carveout), \
             patch("layers.rag.indexer.create_carveout_chunks") as mock_carveout_chunks, \
             patch("layers.rag.indexer.parse_positive_list_with_claude", side_effect=track_positive_list), \
             patch("layers.rag.indexer.create_positive_list_records") as mock_positive_list_records, \
             patch("layers.rag.indexer.chunk_contract_with_claude", side_effect=track_chunk), \
             patch("layers.rag.indexer.get_embedding"):

            mock_carveout_chunks.return_value = None
            mock_positive_list_records.return_value = 0

            await index_contract_from_text(
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                policy_number="POL-001",
                content=contract_text,
                content_hash=None,
                version_label="v1",
                valid_from=date(2024, 6, 9),
                db=mock_db,
                storage=mock_storage,
            )

            # Verify order: CARVEOUT → POSITIVE LIST → CHUNK
            assert call_order == ["carveout", "positive_list", "chunk"]

    @pytest.mark.asyncio
    async def test_index_contract_from_pdf_calls_all_passes(self):
        """index_contract должна вызвать все три pass: CARVEOUT, POSITIVE LIST, chunk."""
        pdf_bytes = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj"

        mock_db = AsyncMock()
        mock_storage = AsyncMock()

        # Mock existing_version check
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("layers.rag.indexer.extract_text_from_pdf") as mock_extract, \
             patch("layers.rag.indexer.parse_carveout_exclusions_with_claude") as mock_carveout, \
             patch("layers.rag.indexer.create_carveout_chunks") as mock_carveout_chunks, \
             patch("layers.rag.indexer.parse_positive_list_with_claude") as mock_positive_list, \
             patch("layers.rag.indexer.create_positive_list_records") as mock_positive_list_records, \
             patch("layers.rag.indexer.chunk_contract_with_claude") as mock_chunk, \
             patch("layers.rag.indexer.get_embedding"):

            mock_extract.return_value = "Extracted text"
            mock_carveout.return_value = []
            mock_positive_list.return_value = []
            mock_positive_list_records.return_value = 0
            mock_chunk.return_value = []

            await index_contract(
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                policy_number="POL-001",
                pdf_bytes=pdf_bytes,
                valid_from=date(2024, 6, 9),
                storage=mock_storage,
                db=mock_db,
            )

            # Verify all passes were called
            mock_extract.assert_called_once()
            mock_carveout.assert_called_once()
            mock_positive_list.assert_called_once()
            mock_chunk.assert_called_once()


class TestContractIndexingLogging:
    """Tests for logging in contract indexing."""

    @pytest.mark.asyncio
    async def test_index_contract_logs_carveout_and_positive_list(self):
        """Логирование должно включать информацию о CARVEOUT и POSITIVE LIST."""
        pdf_bytes = b"%PDF-1.4"

        mock_db = AsyncMock()
        mock_storage = AsyncMock()

        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with patch("layers.rag.indexer.extract_text_from_pdf") as mock_extract, \
             patch("layers.rag.indexer.parse_carveout_exclusions_with_claude") as mock_carveout, \
             patch("layers.rag.indexer.create_carveout_chunks"), \
             patch("layers.rag.indexer.parse_positive_list_with_claude") as mock_positive_list, \
             patch("layers.rag.indexer.create_positive_list_records") as mock_positive_list_records, \
             patch("layers.rag.indexer.chunk_contract_with_claude") as mock_chunk, \
             patch("layers.rag.indexer.get_embedding"), \
             patch("layers.rag.indexer.log") as mock_log:

            mock_extract.return_value = "Text"

            # Return non-empty CARVEOUT and POSITIVE LIST
            mock_carveout.return_value = [{"num": "4.1"}, {"num": "4.2"}]
            mock_positive_list.return_value = [{"procedure_name_ka": "პროც1"}, {"procedure_name_ka": "პროც2"}]
            mock_positive_list_records.return_value = 2
            mock_chunk.return_value = [{"content": "text"}]

            await index_contract(
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                policy_number="POL-001",
                pdf_bytes=pdf_bytes,
                valid_from=date(2024, 6, 9),
                storage=mock_storage,
                db=mock_db,
            )

            # Find the final contract_indexed log call
            contract_indexed_logs = [
                call for call in mock_log.info.call_args_list
                if "contract_indexed" in str(call)
            ]

            assert len(contract_indexed_logs) > 0
            # Check that carveout_chunks and positive_list_procedures are logged
            log_kwargs = contract_indexed_logs[-1][1]
            assert "carveout_chunks" in log_kwargs
            assert "positive_list_procedures" in log_kwargs
            assert log_kwargs["carveout_chunks"] == 2
            assert log_kwargs["positive_list_procedures"] == 2
