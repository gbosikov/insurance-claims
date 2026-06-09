"""Tests for webhook endpoints with signature verification."""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

import pytest
from fastapi import HTTPException

from services.api.routers.webhooks import (
    ContractUpdatedPayload,
    PolicyStatusChangedPayload,
    webhook_contract_updated,
    webhook_policy_status_changed,
    webhook_test,
    verify_webhook_signature,
)


class TestContractUpdatedWebhook:
    """Tests for POST /internal/hooks/contract-updated webhook."""

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_with_valid_signature(self):
        """Webhook should queue reindex task when signature is valid."""
        payload_dict = {
            "policy_number": "POL-001",
            "version_id": "v20240609",
            "reason": "contract_text_updated",
            "timestamp": "2024-06-09T12:34:56Z",
        }
        payload_bytes = json.dumps(payload_dict).encode("utf-8")
        secret = "test-secret-key"

        # Generate valid signature
        sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        signature_header = f"v1,sha256={sig}"

        # Mock request
        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.celery_app") as mock_celery, \
             patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            result = await webhook_contract_updated(mock_request, signature_header)

            # Verify task was queued
            mock_celery.send_task.assert_called_once()

            # Verify response
            assert result["status"] == "queued"
            assert result["policy_number"] == "POL-001"
            assert "webhook_id" in result
            assert result["webhook_id"].startswith("wh_")

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_invalid_signature_returns_401(self):
        """Webhook should return 401 if signature is invalid."""
        payload_bytes = b'{"policy_number": "POL-001"}'
        secret = "test-secret-key"

        # Invalid signature
        signature_header = "v1,sha256=invalid_signature_abc123"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            with pytest.raises(HTTPException) as exc_info:
                await webhook_contract_updated(mock_request, signature_header)

            assert exc_info.value.status_code == 401
            assert "signature" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_missing_signature_returns_401(self):
        """Webhook should return 401 if signature header is missing."""
        payload_bytes = b'{"policy_number": "POL-001"}'
        secret = "test-secret-key"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            with pytest.raises(HTTPException) as exc_info:
                await webhook_contract_updated(mock_request, None)

            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_invalid_payload_returns_400(self):
        """Webhook should return 400 for invalid JSON payload."""
        payload_bytes = b'{"invalid json'  # Malformed JSON
        secret = "test-secret-key"

        sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        signature_header = f"v1,sha256={sig}"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            with pytest.raises(HTTPException) as exc_info:
                await webhook_contract_updated(mock_request, signature_header)

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_webhook_contract_updated_dev_mode_no_signature(self):
        """In dev mode (no secret configured), signature check should be skipped."""
        payload_dict = {
            "policy_number": "POL-001",
            "reason": "contract_text_updated",
        }
        payload_bytes = json.dumps(payload_dict).encode("utf-8")

        # Fake signature (won't be checked in dev mode)
        signature_header = "v1,sha256=fake_signature"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.celery_app") as mock_celery, \
             patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = ""  # Dev mode: no secret

            result = await webhook_contract_updated(mock_request, signature_header)

            # Should succeed despite invalid signature (dev mode)
            assert result["status"] == "queued"
            mock_celery.send_task.assert_called_once()


class TestPolicyStatusChangedWebhook:
    """Tests for POST /internal/hooks/policy-status-changed webhook."""

    @pytest.mark.asyncio
    async def test_webhook_policy_status_changed_with_valid_signature(self):
        """Webhook should receive and log status change with valid signature."""
        payload_dict = {
            "policy_number": "POL-001",
            "status": "inactive",
            "timestamp": "2024-06-09T12:34:56Z",
        }
        payload_bytes = json.dumps(payload_dict).encode("utf-8")
        secret = "test-secret-key"

        sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        signature_header = f"v1,sha256={sig}"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            result = await webhook_policy_status_changed(mock_request, signature_header)

            assert result["status"] == "received"
            assert result["policy_number"] == "POL-001"
            assert "webhook_id" in result
            assert result["webhook_id"].startswith("wh_")

    @pytest.mark.asyncio
    async def test_webhook_policy_status_changed_invalid_signature_returns_401(self):
        """Webhook should return 401 if signature is invalid."""
        payload_bytes = b'{"policy_number": "POL-001", "status": "inactive"}'
        secret = "test-secret-key"

        signature_header = "v1,sha256=invalid_signature"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            with pytest.raises(HTTPException) as exc_info:
                await webhook_policy_status_changed(mock_request, signature_header)

            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_webhook_policy_status_changed_various_statuses(self):
        """Webhook should handle different policy statuses."""
        statuses = ["active", "inactive", "suspended", "expired"]
        secret = "test-secret-key"

        for status in statuses:
            payload_dict = {
                "policy_number": "POL-001",
                "status": status,
            }
            payload_bytes = json.dumps(payload_dict).encode("utf-8")

            sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
            signature_header = f"v1,sha256={sig}"

            mock_request = AsyncMock()
            mock_request.body = AsyncMock(return_value=payload_bytes)

            with patch("services.api.routers.webhooks.settings") as mock_settings:
                mock_settings.webhook_secret_key = secret

                result = await webhook_policy_status_changed(mock_request, signature_header)

                assert result["status"] == "received"
                assert result["policy_number"] == "POL-001"


class TestTestWebhook:
    """Tests for POST /internal/hooks/test webhook."""

    @pytest.mark.asyncio
    async def test_webhook_test_returns_ok(self):
        """Test webhook should always return 200 OK (no signature check)."""
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
    """Integration tests for webhook flow with signature verification."""

    @pytest.mark.asyncio
    async def test_contract_updated_webhook_full_flow(self):
        """Full flow: signature verify → parse → queue → response."""
        payload_dict = {
            "policy_number": "POL-001",
            "version_id": "v20240609",
            "reason": "contract_text_updated",
        }
        payload_bytes = json.dumps(payload_dict).encode("utf-8")
        secret = "test-secret-key"

        sig = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
        signature_header = f"v1,sha256={sig}"

        mock_request = AsyncMock()
        mock_request.body = AsyncMock(return_value=payload_bytes)

        with patch("services.api.routers.webhooks.celery_app") as mock_celery, \
             patch("services.api.routers.webhooks.settings") as mock_settings, \
             patch("services.api.routers.webhooks.log") as mock_log:

            mock_settings.webhook_secret_key = secret

            result = await webhook_contract_updated(mock_request, signature_header)

            # Verify response
            assert result["status"] == "queued"
            assert result["policy_number"] == "POL-001"
            webhook_id = result["webhook_id"]

            # Verify Celery task queued
            mock_celery.send_task.assert_called_once()

            # Verify logging
            assert mock_log.info.call_count >= 2  # signature verified + task queued
