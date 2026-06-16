"""
Router: /internal/hooks — webhook endpoints for external systems.

Webhooks are called by core system (LiteGroup) to notify about:
1. Contract updates (contract_updated)
2. Policy status changes (policy_status_changed)

Webhooks are always on /internal/* path and do NOT require API authentication.
Instead, they are protected by HMAC-SHA256 signature verification:

1. CoreAPI (sender) computes: signature = HMAC-SHA256(webhook_body, secret_key)
2. CoreAPI sends: POST /internal/hooks/... with x-webhook-signature header
3. We (receiver) verify: HMAC-SHA256(received_body, secret_key) == received_signature
4. If match → process webhook
   If no match → reject with 401 Unauthorized

Signature format:
  x-webhook-signature: v1,sha256=<hex-digest>

Example:
  v1,sha256=abcd1234efgh5678ijkl9012mnop3456qrst7890uvwx

Security:
- Prevents webhook spoofing (no one else knows secret_key)
- Time-insensitive (no timestamp comparison needed)
- Constant-time comparison (prevents timing attacks)
- Compatible with all webhook frameworks (CoreAPI, Stripe, GitHub, etc.)
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
import structlog

from core.config import get_settings
from services.worker.celery_app import celery_app

log = structlog.get_logger()
settings = get_settings()

router = APIRouter()

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def verify_webhook_signature(
    body: bytes | str,
    signature_header: str | None,
    secret_key: str | None = None,
) -> bool:
    """
    Verify webhook signature using HMAC-SHA256.

    Args:
        body: Raw webhook body (bytes or string)
        signature_header: Value of x-webhook-signature header
                          Format: "v1,sha256=<hex-digest>"
        secret_key: Shared secret (if None, uses settings.webhook_secret_key)

    Returns:
        True if signature is valid, False otherwise

    Signature verification:
    1. Parse header: "v1,sha256=abc123" → version="v1", algorithm="sha256", signature="abc123"
    2. Compute expected: HMAC-SHA256(body, secret_key)
    3. Compare: expected == received (constant-time comparison)

    Example:
        >>> body = b'{"policy_number": "POL-001"}'
        >>> secret = "my-secret-key"
        >>> expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        >>> header = f"v1,sha256={expected_sig}"
        >>> verify_webhook_signature(body, header, secret)
        True
    """
    if not secret_key:
        secret_key = settings.webhook_secret_key

    # If secret is not configured, skip verification (dev mode)
    if not secret_key:
        log.warning("webhook_signature_verification_disabled")
        return True

    # Header must be provided
    if not signature_header:
        log.warning("webhook_signature_missing")
        return False

    # Parse header: "v1,sha256=abc123def456..."
    try:
        parts = signature_header.split(",")
        if len(parts) != 2:
            log.warning("webhook_signature_invalid_format", header=signature_header)
            return False

        version = parts[0]
        algo_sig = parts[1]

        if not algo_sig.startswith("sha256="):
            log.warning("webhook_signature_unsupported_algorithm", header=signature_header)
            return False

        received_signature = algo_sig[7:]  # Remove "sha256=" prefix

        # Only support v1 format
        if version != "v1":
            log.warning("webhook_signature_unsupported_version", version=version)
            return False

    except Exception as e:
        log.error("webhook_signature_parse_error", error=str(e), header=signature_header)
        return False

    # Convert body to bytes if string
    if isinstance(body, str):
        body = body.encode("utf-8")

    # Compute expected signature
    expected_signature = hmac.new(
        secret_key.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison (prevents timing attacks)
    is_valid = hmac.compare_digest(expected_signature, received_signature)

    if not is_valid:
        log.warning(
            "webhook_signature_mismatch",
            received=received_signature[:8] + "...",
            expected=expected_signature[:8] + "...",
        )

    return is_valid


class ContractUpdatedPayload(BaseModel):
    """Payload from CoreAPI when contract is updated."""

    policy_number: str
    version_id: str | None = None
    reason: str  # "contract_text_updated" | "contract_terms_changed" | "contract_active"
    timestamp: str | None = None  # ISO 8601
    details: dict | None = None  # Additional context from core system

    class Config:
        json_schema_extra = {
            "example": {
                "policy_number": "POL-001",
                "version_id": "v20240609",
                "reason": "contract_text_updated",
                "timestamp": "2024-06-09T12:34:56Z",
                "details": {"previous_version": "v20240601", "change_count": 5},
            }
        }


class PolicyStatusChangedPayload(BaseModel):
    """Payload from CoreAPI when policy status changes."""

    policy_number: str
    status: str  # "active" | "inactive" | "suspended" | "expired"
    timestamp: str | None = None
    details: dict | None = None

    class Config:
        json_schema_extra = {
            "example": {
                "policy_number": "POL-001",
                "status": "inactive",
                "timestamp": "2024-06-09T12:34:56Z",
            }
        }


@router.post("/contract-updated", status_code=202)
async def webhook_contract_updated(
    request: Request,
    x_webhook_signature: str | None = Header(None),
):
    """
    Webhook: Contract updated in core system.

    Called by CoreAPI when:
    - Contract text changes (e.g., exclusions, limits updated)
    - Contract becomes active
    - New version of contract is added

    Automatically triggers reindexing of CARVEOUT and POSITIVE LIST structures.

    Security:
    - Signature verification via HMAC-SHA256 (x-webhook-signature header)
    - Returns 401 if signature is invalid or missing
    - Skips verification if webhook_secret_key not configured (dev mode)

    Args:
        request: FastAPI Request object (for raw body)
        x_webhook_signature: Signature header value (format: "v1,sha256=...")

    Payload:
        {
            "policy_number": "POL-001",
            "version_id": "v20240609",
            "reason": "contract_text_updated",
            "timestamp": "2024-06-09T12:34:56Z",
            "details": {...}
        }

    Returns (202 Accepted):
        {
            "status": "queued",
            "webhook_id": "wh_...",
            "received_at": "...",
            "policy_number": "POL-001",
            "message": "Contract reindexing has been queued..."
        }

    Example request:
        ```
        POST /internal/hooks/contract-updated
        x-webhook-signature: v1,sha256=abc123def456...
        Content-Type: application/json

        {
            "policy_number": "POL-001",
            "version_id": "v20240609",
            "reason": "contract_text_updated",
            "timestamp": "2024-06-09T12:34:56Z"
        }
        ```

    Example response (202):
        ```
        {
            "status": "queued",
            "webhook_id": "wh_abc123...",
            "received_at": "2024-06-09T12:34:57Z",
            "policy_number": "POL-001",
            "message": "Contract reindexing has been queued..."
        }
        ```
    """
    from uuid import uuid4

    webhook_id = f"wh_{uuid4().hex[:12]}"
    received_at = datetime.utcnow().isoformat()

    # ── Verify webhook signature ───────────────────────────────────
    raw_body = await request.body()

    if not verify_webhook_signature(raw_body, x_webhook_signature):
        log.error(
            "webhook_signature_verification_failed",
            webhook_id=webhook_id,
            event_type="contract_updated",
            signature_present=x_webhook_signature is not None,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    log.info(
        "webhook_signature_verified",
        webhook_id=webhook_id,
        event_type="contract_updated",
    )

    # ── Parse and validate payload ─────────────────────────────────
    try:
        payload = ContractUpdatedPayload.model_validate_json(raw_body)
    except Exception as e:
        log.error(
            "webhook_payload_parse_error",
            webhook_id=webhook_id,
            event_type="contract_updated",
            error=str(e),
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payload: {str(e)}",
        )

    # Log webhook receipt
    log.info(
        "webhook_received",
        webhook_id=webhook_id,
        event_type="contract_updated",
        policy_number=payload.policy_number,
        version_id=payload.version_id,
        reason=payload.reason,
        timestamp=payload.timestamp,
    )

    # Queue reindex task
    celery_app.send_task(
        "reindex_contract_structures",
        kwargs={
            "tenant_id": DEFAULT_TENANT_ID,
            "policy_number": payload.policy_number,
            "version_id": payload.version_id or "latest",
            "pdf_storage_path": None,  # Will fetch from DB
        },
        queue="contracts",
    )

    log.info(
        "contract_reindex_queued",
        webhook_id=webhook_id,
        policy_number=payload.policy_number,
        reason=payload.reason,
    )

    return {
        "status": "queued",
        "webhook_id": webhook_id,
        "received_at": received_at,
        "policy_number": payload.policy_number,
        "message": "Contract reindexing has been queued. New structures will be available in 2-5 minutes.",
    }


@router.post("/policy-status-changed", status_code=202)
async def webhook_policy_status_changed(
    request: Request,
    x_webhook_signature: str | None = Header(None),
):
    """
    Webhook: Policy status changed in core system.

    Called by CoreAPI when:
    - Policy becomes active/inactive
    - Policy is suspended
    - Policy expires

    Currently logs the event. Future: could trigger other actions
    (e.g., reject pending claims if policy becomes inactive).

    Security:
    - Signature verification via HMAC-SHA256
    - Returns 401 if signature is invalid or missing

    Args:
        request: FastAPI Request object (for raw body)
        x_webhook_signature: Signature header value

    Payload:
        {
            "policy_number": "POL-001",
            "status": "inactive",
            "timestamp": "2024-06-09T12:34:56Z",
            "details": {...}
        }

    Returns (202 Accepted):
        {
            "status": "received",
            "webhook_id": "wh_...",
            "received_at": "...",
            "policy_number": "POL-001",
            "message": "Policy status change recorded"
        }

    Example request:
        ```
        POST /internal/hooks/policy-status-changed
        x-webhook-signature: v1,sha256=...
        Content-Type: application/json

        {
            "policy_number": "POL-001",
            "status": "inactive",
            "timestamp": "2024-06-09T12:34:56Z"
        }
        ```

    Example response (202):
        ```
        {
            "status": "received",
            "webhook_id": "wh_def456...",
            "received_at": "2024-06-09T12:34:57Z",
            "policy_number": "POL-001",
            "message": "Policy status change recorded"
        }
        ```
    """
    from uuid import uuid4

    webhook_id = f"wh_{uuid4().hex[:12]}"
    received_at = datetime.utcnow().isoformat()

    # ── Verify webhook signature ───────────────────────────────────
    raw_body = await request.body()

    if not verify_webhook_signature(raw_body, x_webhook_signature):
        log.error(
            "webhook_signature_verification_failed",
            webhook_id=webhook_id,
            event_type="policy_status_changed",
            signature_present=x_webhook_signature is not None,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    log.info(
        "webhook_signature_verified",
        webhook_id=webhook_id,
        event_type="policy_status_changed",
    )

    # ── Parse and validate payload ─────────────────────────────────
    try:
        payload = PolicyStatusChangedPayload.model_validate_json(raw_body)
    except Exception as e:
        log.error(
            "webhook_payload_parse_error",
            webhook_id=webhook_id,
            event_type="policy_status_changed",
            error=str(e),
        )
        raise HTTPException(
            status_code=400,
            detail=f"Invalid payload: {str(e)}",
        )

    log.info(
        "webhook_received",
        webhook_id=webhook_id,
        event_type="policy_status_changed",
        policy_number=payload.policy_number,
        status=payload.status,
        timestamp=payload.timestamp,
    )

    # TODO: Implement policy status change handling
    # - If status == "inactive": reject pending claims?
    # - If status == "suspended": pause all processing?
    # - If status == "expired": archive all claims?

    return {
        "status": "received",
        "webhook_id": webhook_id,
        "received_at": received_at,
        "policy_number": payload.policy_number,
        "message": "Policy status change recorded",
    }


@router.post("/test", status_code=200)
async def webhook_test():
    """
    Test webhook endpoint.

    Used by CoreAPI to verify webhook connectivity during setup.
    Always returns 200 OK (no signature verification required).

    Example request:
        ```
        POST /internal/hooks/test
        ```

    Example response (200):
        ```
        {
            "status": "ok",
            "message": "Webhook endpoint is reachable",
            "timestamp": "2024-06-09T12:34:57Z"
        }
        ```
    """
    log.info("webhook_test_received")
    return {
        "status": "ok",
        "message": "Webhook endpoint is reachable",
        "timestamp": datetime.utcnow().isoformat(),
    }
