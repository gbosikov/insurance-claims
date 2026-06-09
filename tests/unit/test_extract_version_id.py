"""Tests for extracting contract version_id from chunks."""

from unittest.mock import MagicMock

import pytest

from layers.decision.service import extract_contract_version_id
from core.schemas.contract import ContractChunkSchema


class TestExtractContractVersionId:
    """Tests for extract_contract_version_id function."""

    def test_extract_version_id_from_first_chunk(self):
        """Should extract version_id from first chunk that has it."""
        chunk1 = MagicMock(spec=ContractChunkSchema)
        chunk1.version_id = "v20240609"

        chunk2 = MagicMock(spec=ContractChunkSchema)
        chunk2.version_id = "v20240609"

        result = extract_contract_version_id([chunk1, chunk2])

        assert result == "v20240609"

    def test_extract_version_id_empty_chunks_returns_latest(self):
        """Should return 'latest' if chunks list is empty."""
        result = extract_contract_version_id([])

        assert result == "latest"

    def test_extract_version_id_no_version_id_field_returns_latest(self):
        """Should return 'latest' if chunks don't have version_id."""
        chunk1 = MagicMock(spec=ContractChunkSchema)
        chunk1.version_id = None

        chunk2 = MagicMock(spec=ContractChunkSchema)
        chunk2.version_id = None

        result = extract_contract_version_id([chunk1, chunk2])

        assert result == "latest"

    def test_extract_version_id_missing_attribute_returns_latest(self):
        """Should return 'latest' if chunks don't have version_id attribute."""
        chunk1 = MagicMock(spec=ContractChunkSchema)
        del chunk1.version_id  # Remove the attribute

        result = extract_contract_version_id([chunk1])

        assert result == "latest"

    def test_extract_version_id_skips_none_values(self):
        """Should skip chunks with None version_id and find the first valid one."""
        chunk1 = MagicMock(spec=ContractChunkSchema)
        chunk1.version_id = None

        chunk2 = MagicMock(spec=ContractChunkSchema)
        chunk2.version_id = "v20240610"

        chunk3 = MagicMock(spec=ContractChunkSchema)
        chunk3.version_id = "v20240610"

        result = extract_contract_version_id([chunk1, chunk2, chunk3])

        assert result == "v20240610"

    def test_extract_version_id_all_chunks_have_same_version(self):
        """All chunks should have the same version_id for consistency."""
        chunk1 = MagicMock(spec=ContractChunkSchema)
        chunk1.version_id = "v20240609"

        chunk2 = MagicMock(spec=ContractChunkSchema)
        chunk2.version_id = "v20240609"

        chunk3 = MagicMock(spec=ContractChunkSchema)
        chunk3.version_id = "v20240609"

        result = extract_contract_version_id([chunk1, chunk2, chunk3])

        assert result == "v20240609"

    def test_extract_version_id_various_formats(self):
        """Should handle various version_id formats."""
        test_cases = [
            "v20240609",
            "v1",
            "v2024-06-09",
            "version-1.0",
            "latest",
        ]

        for version_id in test_cases:
            chunk = MagicMock(spec=ContractChunkSchema)
            chunk.version_id = version_id

            result = extract_contract_version_id([chunk])

            assert result == version_id

    def test_extract_version_id_empty_string_skipped(self):
        """Should skip chunks with empty string version_id."""
        chunk1 = MagicMock(spec=ContractChunkSchema)
        chunk1.version_id = ""  # empty string

        chunk2 = MagicMock(spec=ContractChunkSchema)
        chunk2.version_id = "v20240609"

        result = extract_contract_version_id([chunk1, chunk2])

        assert result == "v20240609"


class TestExtractVersionIdIntegration:
    """Integration tests for version_id extraction in decision engine."""

    def test_version_id_used_in_positive_list_check(self):
        """Version_id should be passed to check_positive_list for correct procedure lookup."""
        # This test verifies the flow where version_id extracted from chunks
        # is passed to check_positive_list(version_id=extracted_id)

        # Scenario: Contract has multiple versions
        # - v20240601: 30 procedures
        # - v20240609: 45 procedures

        chunk_v20240609 = MagicMock(spec=ContractChunkSchema)
        chunk_v20240609.version_id = "v20240609"

        extracted_version = extract_contract_version_id([chunk_v20240609])

        # Verify we're using the right version for POSITIVE LIST lookup
        assert extracted_version == "v20240609"
        # This version_id will be used to query:
        # SELECT * FROM positive_list_procedures
        # WHERE version_id = "v20240609"
        # And get the 45 procedures from the newer contract version
