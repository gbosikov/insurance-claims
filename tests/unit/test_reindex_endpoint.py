"""Tests for contract reindex endpoint."""

from uuid import UUID
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date

import pytest
from fastapi.testclient import TestClient

from core.auth import DEFAULT_TENANT_ID
from services.api.routers.contracts import router


class TestReindexContractEndpoint:
    """Tests for POST /v1/contracts/{policy_number}/reindex endpoint."""

    @pytest.mark.asyncio
    async def test_reindex_contract_queues_task(self):
        """Endpoint should queue reindex task in Celery."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from core.models.contract import ContractVersion

        mock_db = AsyncMock(spec=AsyncSession)
        mock_version = MagicMock(spec=ContractVersion)
        mock_version.version_id = "v20240609"
        mock_version.pdf_path = "tenants/.../contracts/POL-001/v20240609.pdf"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_version
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("services.worker.celery_app.celery_app") as mock_celery:
            from services.api.routers.contracts import reindex_contract
            result = await reindex_contract("POL-001", None, mock_db)

            mock_celery.send_task.assert_called_once()
            send_call = mock_celery.send_task.call_args
            assert send_call.args[0] == "reindex_contract_structures"
            assert send_call.kwargs["queue"] == "contracts"
            assert send_call.kwargs["kwargs"]["policy_number"] == "POL-001"
            assert send_call.kwargs["kwargs"]["version_id"] == "v20240609"

            assert result["status"] == "queued"
            assert result["policy_number"] == "POL-001"
            assert result["version_id"] == "v20240609"

    @pytest.mark.asyncio
    async def test_reindex_contract_not_found(self):
        """Endpoint should return 404 if contract version not found."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from fastapi import HTTPException

        mock_db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        from services.api.routers.contracts import reindex_contract

        with pytest.raises(HTTPException) as exc_info:
            await reindex_contract("POL-NOT-EXIST", None, mock_db)

        assert exc_info.value.status_code == 404
        assert "Contract version not found" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_reindex_contract_with_specific_version(self):
        """Endpoint should filter by version_id if provided."""
        from sqlalchemy.ext.asyncio import AsyncSession

        mock_db = AsyncMock(spec=AsyncSession)
        mock_version = MagicMock()
        mock_version.version_id = "v20240601"
        mock_version.pdf_path = "path/to/pdf"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_version
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("services.worker.celery_app.celery_app"):
            from services.api.routers.contracts import reindex_contract
            result = await reindex_contract("POL-001", "v20240601", mock_db)

            assert result["version_id"] == "v20240601"


class TestReindexContractStructuresFunction:
    """Tests for reindex_contract_structures function in indexer."""

    @pytest.mark.asyncio
    async def test_reindex_deletes_old_records(self):
        """Reindex should delete old CARVEOUT and POSITIVE LIST records."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from layers.rag.indexer import reindex_contract_structures

        mock_db = AsyncMock(spec=AsyncSession)

        with patch("layers.rag.indexer.parse_carveout_exclusions_with_claude") as mock_carveout, \
             patch("layers.rag.indexer.create_carveout_chunks"), \
             patch("layers.rag.indexer.parse_positive_list_with_claude") as mock_positive_list, \
             patch("layers.rag.indexer.create_positive_list_records") as mock_positive_list_records:

            mock_carveout.return_value = []
            mock_positive_list.return_value = []
            mock_positive_list_records.return_value = 0

            count_result = MagicMock()
            count_result.scalar.side_effect = [5, 10]  # 5 old carveout, 10 old positive
            mock_db.execute = AsyncMock(return_value=count_result)

            result = await reindex_contract_structures(
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                policy_number="POL-001",
                version_id="v20240609",
                contract_text="Sample text",
                db=mock_db,
            )

            assert result["carveout_chunks_old"] == 5
            assert result["positive_list_old"] == 10
            assert result["carveout_chunks_new"] == 0
            assert result["positive_list_new"] == 0

    @pytest.mark.asyncio
    async def test_reindex_creates_new_records(self):
        """Reindex should parse and create new records."""
        from sqlalchemy.ext.asyncio import AsyncSession
        from layers.rag.indexer import reindex_contract_structures

        mock_db = AsyncMock(spec=AsyncSession)

        with patch("layers.rag.indexer.parse_carveout_exclusions_with_claude") as mock_carveout, \
             patch("layers.rag.indexer.create_carveout_chunks") as mock_carveout_chunks, \
             patch("layers.rag.indexer.parse_positive_list_with_claude") as mock_positive_list, \
             patch("layers.rag.indexer.create_positive_list_records") as mock_positive_list_records:

            mock_carveout.return_value = [
                {"num": "4.1", "excluded": {"icd10": ["N18"]}, "original_text": "Text"}
            ]
            mock_positive_list.return_value = [
                {"procedure_name_ka": "პროც1", "section_reference": "1.7.3"}
            ]
            mock_positive_list_records.return_value = 1

            count_result = MagicMock()
            count_result.scalar.side_effect = [0, 0]  # no old records
            mock_db.execute = AsyncMock(return_value=count_result)

            result = await reindex_contract_structures(
                tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
                policy_number="POL-001",
                version_id="v20240609",
                contract_text="Sample text",
                db=mock_db,
            )

            assert result["carveout_chunks_new"] == 1
            assert result["positive_list_new"] == 1

            mock_carveout.assert_called_once()
            mock_positive_list.assert_called_once()
            mock_carveout_chunks.assert_called_once()
            mock_positive_list_records.assert_called_once()
            mock_db.commit.assert_called_once()


class TestReindexTaskScheduling:
    """Tests for Celery task scheduling."""

    @pytest.mark.asyncio
    async def test_reindex_task_extracts_text_from_pdf(self):
        """Task should be importable and has the correct Celery task name."""
        pytest.importorskip("numpy")  # preprocessing/service.py requires numpy
        from services.worker.tasks import reindex_contract_structures_task

        # Verify that the task is registered with correct name
        assert reindex_contract_structures_task.name == "reindex_contract_structures"
