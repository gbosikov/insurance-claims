"""Tests for webhook endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

import pytest

from services.api.routers.webhooks import (
    ContractUpdatedPayload,
    PolicyStatusChangedPayload,
    webhook_contract_updated,
    webhook_policy_status_changed,
    webhook_test,
)


class TestContractUpdatedWebhook:
    """Tests for POST /internal/hooks/contract-updated webhook."""

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_queues_reindex_task(self):
        """Webhook should queue reindex task when contract is updated."""
        payload = ContractUpdatedPayload(
            policy_number="POL-001",
            version_id="v20240609",
            reason="contract_text_updated",
            timestamp="2024-06-09T12:34:56Z",
        )

        with patch("services.api.routers.webhooks.celery_app") as mock_celery:
            result = await webhook_contract_updated(payload)

            # Verify task was queued
            mock_celery.send_task.assert_called_once()
            call_kwargs = mock_celery.send_task.call_args[1]
            assert call_kwargs["task_name"] == "reindex_contract_structures"
            assert call_kwargs["queue"] == "contracts"

            # Verify payload passed correctly
            task_kwargs = call_kwargs["kwargs"]
            assert task_kwargs["policy_number"] == "POL-001"
            assert task_kwargs["version_id"] == "v20240609"

            # Verify response
            assert result["status"] == "queued"
            assert result["policy_number"] == "POL-001"
            assert "webhook_id" in result
            assert result["webhook_id"].startswith("wh_")

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_without_version_id(self):
        """Webhook should use 'latest' if version_id not provided."""
        payload = ContractUpdatedPayload(
            policy_number="POL-002",
            reason="contract_active",
            timestamp="2024-06-09T12:34:56Z",
        )

        with patch("services.api.routers.webhooks.celery_app") as mock_celery:
            result = await webhook_contract_updated(payload)

            # Verify task was queued with 'latest'
            call_kwargs = mock_celery.send_task.call_args[1]
            task_kwargs = call_kwargs["kwargs"]
            assert task_kwargs["version_id"] == "latest"

            assert result["status"] == "queued"

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_logs_event(self):
        """Webhook should log receipt and task queueing."""
        payload = ContractUpdatedPayload(
            policy_number="POL-001",
            version_id="v20240609",
            reason="contract_text_updated",
        )

        with patch("services.api.routers.webhooks.celery_app"), \
             patch("services.api.routers.webhooks.log") as mock_log:

            result = await webhook_contract_updated(payload)

            # Verify logging
            webhook_id = result["webhook_id"]

            # Should log webhook receipt
            webhook_received_calls = [
                c for c in mock_log.info.call_args_list
                if "webhook_received" in str(c)
            ]
            assert len(webhook_received_calls) > 0

            # Should log task queuing
            task_queued_calls = [
                c for c in mock_log.info.call_args_list
                if "contract_reindex_queued" in str(c)
            ]
            assert len(task_queued_calls) > 0

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_response_format(self):
        """Webhook response should have correct format and fields."""
        payload = ContractUpdatedPayload(
            policy_number="POL-001",
            version_id="v20240609",
            reason="contract_text_updated",
        )

        with patch("services.api.routers.webhooks.celery_app"):
            result = await webhook_contract_updated(payload)

            # Verify response structure
            assert isinstance(result, dict)
            assert result["status"] == "queued"
            assert "webhook_id" in result
            assert "received_at" in result
            assert "policy_number" in result
            assert "message" in result

            # Verify values
            assert result["policy_number"] == "POL-001"
            assert "queued" in result["message"].lower()


class TestPolicyStatusChangedWebhook:
    """Tests for POST /internal/hooks/policy-status-changed webhook."""

    @pytest.mark.asyncio
    async def test_webhook_policy_status_changed_receives_and_logs(self):
        """Webhook should receive policy status change and log it."""
        payload = PolicyStatusChangedPayload(
            policy_number="POL-001",
            status="inactive",
            timestamp="2024-06-09T12:34:56Z",
        )

        with patch("services.api.routers.webhooks.log") as mock_log:
            result = await webhook_policy_status_changed(payload)

            # Verify logging
            webhook_log_calls = [
                c for c in mock_log.info.call_args_list
                if "webhook_received" in str(c)
            ]
            assert len(webhook_log_calls) > 0

            # Verify response
            assert result["status"] == "received"
            assert result["policy_number"] == "POL-001"
            assert "webhook_id" in result
            assert result["webhook_id"].startswith("wh_")

    @pytest.mark.asyncio
    async def test_webhook_policy_status_changed_various_statuses(self):
        """Webhook should handle different policy status values."""
        statuses = ["active", "inactive", "suspended", "expired"]

        for status in statuses:
            payload = PolicyStatusChangedPayload(
                policy_number="POL-001",
                status=status,
            )

            with patch("services.api.routers.webhooks.log"):
                result = await webhook_policy_status_changed(payload)

                assert result["status"] == "received"
                assert result["policy_number"] == "POL-001"

    @pytest.mark.asyncio
    async def test_webhook_policy_status_changed_response_format(self):
        """Webhook response should have correct format."""
        payload = PolicyStatusChangedPayload(
            policy_number="POL-001",
            status="inactive",
        )

        with patch("services.api.routers.webhooks.log"):
            result = await webhook_policy_status_changed(payload)

            assert isinstance(result, dict)
            assert result["status"] == "received"
            assert "webhook_id" in result
            assert "received_at" in result
            assert "policy_number" in result
            assert "message" in result


class TestTestWebhook:
    """Tests for POST /internal/hooks/test webhook."""

    @pytest.mark.asyncio
    async def test_webhook_test_returns_ok(self):
        """Test webhook should always return 200 OK."""
        with patch("services.api.routers.webhooks.log"):
            result = await webhook_test()

            assert isinstance(result, dict)
            assert result["status"] == "ok"
            assert "message" in result
            assert "reachable" in result["message"].lower()
            assert "timestamp" in result

    @pytest.mark.asyncio
    async def test_webhook_test_logs_receipt(self):
        """Test webhook should log its receipt."""
        with patch("services.api.routers.webhooks.log") as mock_log:
            result = await webhook_test()

            # Verify logging
            test_log_calls = [
                c for c in mock_log.info.call_args_list
                if "webhook_test_received" in str(c)
            ]
            assert len(test_log_calls) > 0


class TestWebhookPayloads:
    """Tests for webhook payload validation."""

    def test_contract_updated_payload_valid(self):
        """Valid ContractUpdatedPayload should parse correctly."""
        payload = ContractUpdatedPayload(
            policy_number="POL-001",
            version_id="v20240609",
            reason="contract_text_updated",
            timestamp="2024-06-09T12:34:56Z",
            details={"previous_version": "v20240601"},
        )

        assert payload.policy_number == "POL-001"
        assert payload.version_id == "v20240609"
        assert payload.reason == "contract_text_updated"

    def test_contract_updated_payload_optional_fields(self):
        """ContractUpdatedPayload should allow optional fields."""
        payload = ContractUpdatedPayload(
            policy_number="POL-001",
            reason="contract_active",
        )

        assert payload.policy_number == "POL-001"
        assert payload.version_id is None
        assert payload.timestamp is None
        assert payload.details is None

    def test_policy_status_changed_payload_valid(self):
        """Valid PolicyStatusChangedPayload should parse correctly."""
        payload = PolicyStatusChangedPayload(
            policy_number="POL-001",
            status="inactive",
            timestamp="2024-06-09T12:34:56Z",
        )

        assert payload.policy_number == "POL-001"
        assert payload.status == "inactive"

    def test_policy_status_changed_payload_optional_fields(self):
        """PolicyStatusChangedPayload should allow optional fields."""
        payload = PolicyStatusChangedPayload(
            policy_number="POL-001",
            status="active",
        )

        assert payload.policy_number == "POL-001"
        assert payload.timestamp is None
        assert payload.details is None


class TestWebhookIntegration:
    """Integration tests for webhook flow."""

    @pytest.mark.asyncio
    async def test_contract_updated_webhook_end_to_end(self):
        """Full flow: webhook receipt → reindex task queuing."""
        payload = ContractUpdatedPayload(
            policy_number="POL-001",
            version_id="v20240609",
            reason="contract_text_updated",
            timestamp="2024-06-09T12:34:56Z",
            details={"change_count": 5},
        )

        with patch("services.api.routers.webhooks.celery_app") as mock_celery, \
             patch("services.api.routers.webhooks.log") as mock_log:

            result = await webhook_contract_updated(payload)

            # Verify response
            assert result["status"] == "queued"
            assert result["policy_number"] == "POL-001"
            webhook_id = result["webhook_id"]

            # Verify Celery task queued
            mock_celery.send_task.assert_called_once()

            # Verify logging happened
            assert mock_log.info.call_count >= 2  # at least 2 log calls

    @pytest.mark.asyncio
    async def test_webhook_error_handling_missing_policy_number(self):
        """Webhook should reject payloads with missing required fields."""
        # Note: Pydantic will validate this at the model level
        # This test verifies the FastAPI endpoint receives validated data
        with pytest.raises(Exception):  # Pydantic ValidationError
            ContractUpdatedPayload(
                reason="contract_text_updated",
                # Missing policy_number
            )
