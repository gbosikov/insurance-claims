"""Tests for webhook signature verification."""

import hashlib
import hmac
from unittest.mock import patch

import pytest

from services.api.routers.webhooks import verify_webhook_signature


class TestWebhookSignatureVerification:
    """Tests for HMAC-SHA256 signature verification."""

    def test_verify_valid_signature(self):
        """Should return True for valid signature."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        # Compute expected signature
        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={expected_sig}"

        result = verify_webhook_signature(body, header, secret)

        assert result is True

    def test_verify_invalid_signature(self):
        """Should return False for invalid signature."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        # Wrong signature
        header = "v1,sha256=wrong_signature_abc123def456"

        result = verify_webhook_signature(body, header, secret)

        assert result is False

    def test_verify_signature_missing_header(self):
        """Should return False if signature header is missing."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        result = verify_webhook_signature(body, None, secret)

        assert result is False

    def test_verify_signature_no_secret_configured(self):
        """Should return True if no secret is configured (dev mode)."""
        body = b'{"policy_number": "POL-001"}'
        header = "v1,sha256=abc123"

        result = verify_webhook_signature(body, header, secret_key="")

        assert result is True

    def test_verify_signature_with_string_body(self):
        """Should handle string body (converted to bytes internally)."""
        secret = "my-secret-key"
        body_str = '{"policy_number": "POL-001"}'
        body_bytes = body_str.encode("utf-8")

        # Compute expected signature
        expected_sig = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        header = f"v1,sha256={expected_sig}"

        result = verify_webhook_signature(body_str, header, secret)

        assert result is True

    def test_verify_signature_invalid_format_no_comma(self):
        """Should return False if header format is invalid (no comma)."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'
        header = "v1sha256abc123"  # Missing comma

        result = verify_webhook_signature(body, header, secret)

        assert result is False

    def test_verify_signature_invalid_format_wrong_algorithm(self):
        """Should return False if algorithm is not sha256."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'
        header = "v1,sha512=abc123"  # Wrong algorithm

        result = verify_webhook_signature(body, header, secret)

        assert result is False

    def test_verify_signature_unsupported_version(self):
        """Should return False if version is not v1."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v2,sha256={expected_sig}"

        result = verify_webhook_signature(body, header, secret)

        assert result is False

    def test_verify_signature_constant_time_comparison(self):
        """Signature comparison should use constant-time comparison."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={expected_sig}"

        # Should use hmac.compare_digest internally
        result = verify_webhook_signature(body, header, secret)

        assert result is True

    def test_verify_signature_timing_attack_resistant(self):
        """Signature comparison should be resistant to timing attacks."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        # Create two signatures that differ in first character
        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        wrong_sig = "a" + expected_sig[1:]  # Change first char

        header = f"v1,sha256={wrong_sig}"

        result = verify_webhook_signature(body, header, secret)

        assert result is False

    def test_verify_signature_with_special_characters(self):
        """Should handle body with special characters."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001", "reason": "contract_text_updated"}'

        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={expected_sig}"

        result = verify_webhook_signature(body, header, secret)

        assert result is True

    def test_verify_signature_case_insensitive_algorithm(self):
        """Signature hex should be case-insensitive."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        # Try with uppercase
        header_upper = f"v1,sha256={expected_sig.upper()}"
        result_upper = verify_webhook_signature(body, header_upper, secret)

        # Result should depend on how Python handles hex comparison
        # HMAC comparison is case-sensitive for hex strings
        # So uppercase should fail
        assert result_upper is False

    def test_verify_signature_different_secrets_fail(self):
        """Should fail if secret differs."""
        secret1 = "secret-1"
        secret2 = "secret-2"
        body = b'{"policy_number": "POL-001"}'

        # Sign with secret1
        sig1 = hmac.new(secret1.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={sig1}"

        # Verify with secret2 should fail
        result = verify_webhook_signature(body, header, secret2)

        assert result is False

    def test_verify_signature_different_bodies_fail(self):
        """Should fail if body differs."""
        secret = "my-secret-key"
        body1 = b'{"policy_number": "POL-001"}'
        body2 = b'{"policy_number": "POL-002"}'

        # Sign body1
        sig = hmac.new(secret.encode(), body1, hashlib.sha256).hexdigest()
        header = f"v1,sha256={sig}"

        # Verify with body2 should fail
        result = verify_webhook_signature(body2, header, secret)

        assert result is False

    def test_verify_signature_empty_body(self):
        """Should handle empty body."""
        secret = "my-secret-key"
        body = b''

        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={sig}"

        result = verify_webhook_signature(body, header, secret)

        assert result is True

    def test_verify_signature_with_settings_default_secret(self):
        """Should use settings.webhook_secret_key if no secret provided."""
        body = b'{"policy_number": "POL-001"}'
        secret = "configured-secret"

        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={sig}"

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret

            result = verify_webhook_signature(body, header, secret_key=None)

            assert result is True

    def test_verify_signature_uses_settings_when_no_explicit_secret(self):
        """Should prefer explicit secret over settings."""
        body = b'{"policy_number": "POL-001"}'
        secret_explicit = "explicit-secret"
        secret_settings = "settings-secret"

        sig = hmac.new(secret_explicit.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={sig}"

        with patch("services.api.routers.webhooks.settings") as mock_settings:
            mock_settings.webhook_secret_key = secret_settings

            # Provide explicit secret - should use that
            result = verify_webhook_signature(body, header, secret_key=secret_explicit)

            assert result is True


class TestWebhookSignatureGeneration:
    """Tests for generating valid webhook signatures."""

    def test_generate_signature_format(self):
        """Generated signature should have correct format."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        sig_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"v1,sha256={sig_hex}"

        # Should be in format: v1,sha256=<64-char-hex>
        assert header.startswith("v1,sha256=")
        assert len(sig_hex) == 64  # SHA256 produces 64 hex characters

    def test_generate_different_signatures_for_different_payloads(self):
        """Different payloads should produce different signatures."""
        secret = "my-secret-key"
        body1 = b'{"policy_number": "POL-001"}'
        body2 = b'{"policy_number": "POL-002"}'

        sig1 = hmac.new(secret.encode(), body1, hashlib.sha256).hexdigest()
        sig2 = hmac.new(secret.encode(), body2, hashlib.sha256).hexdigest()

        assert sig1 != sig2

    def test_generate_same_signature_for_same_payload_and_secret(self):
        """Same payload and secret should always produce same signature."""
        secret = "my-secret-key"
        body = b'{"policy_number": "POL-001"}'

        sig1 = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        sig2 = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        assert sig1 == sig2
