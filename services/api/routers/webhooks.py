"""
Router: /internal/hooks — webhook endpoints for external systems.

Webhooks are called by core system (LiteGroup) to notify about:
1. Contract updates (contract_updated)
2. Policy status changes (policy_status_changed)

Webhooks are always on /internal/* path and do NOT require API authentication.
They should be called from trusted internal networks only (add IP whitelist in production).
"""

from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import structlog

from services.worker.celery_app import celery_app

log = structlog.get_logger()

router = APIRouter()

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


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
async def webhook_contract_updated(payload: ContractUpdatedPayload):
    """
    Webhook: Contract updated in core system.

    Called by CoreAPI when:
    - Contract text changes (e.g., exclusions, limits updated)
    - Contract becomes active
    - New version of contract is added

    Automatically triggers reindexing of CARVEOUT and POSITIVE LIST structures.

    Args:
        policy_number: Policy identifier
        version_id: Specific contract version (optional, uses latest if not provided)
        reason: Why contract was updated
        timestamp: When update occurred (ISO 8601)
        details: Additional context

    Returns:
        {
            "status": "queued",
            "webhook_id": "wh_...",
            "message": "Contract reindexing has been queued..."
        }

    Example request:
        ```
        POST /internal/hooks/contract-updated
        {
            "policy_number": "POL-001",
            "version_id": "v20240609",
            "reason": "contract_text_updated",
            "timestamp": "2024-06-09T12:34:56Z"
        }
        ```

    Example response:
        ```
        {
            "status": "queued",
            "webhook_id": "wh_abc123...",
            "message": "Contract reindexing has been queued. New structures available in 2-5 minutes."
        }
        ```
    """
    from uuid import uuid4

    webhook_id = f"wh_{uuid4().hex[:12]}"
    received_at = datetime.utcnow().isoformat()

    # Log webhook receipt
    log.info(
        "webhook_received",
        webhook_id=webhook_id,
        event="contract_updated",
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
async def webhook_policy_status_changed(payload: PolicyStatusChangedPayload):
    """
    Webhook: Policy status changed in core system.

    Called by CoreAPI when:
    - Policy becomes active/inactive
    - Policy is suspended
    - Policy expires

    Currently logs the event. Future: could trigger other actions
    (e.g., reject pending claims if policy becomes inactive).

    Args:
        policy_number: Policy identifier
        status: New policy status
        timestamp: When status changed (ISO 8601)
        details: Additional context

    Returns:
        {
            "status": "received",
            "webhook_id": "wh_...",
            "message": "Policy status change recorded"
        }

    Example request:
        ```
        POST /internal/hooks/policy-status-changed
        {
            "policy_number": "POL-001",
            "status": "inactive",
            "timestamp": "2024-06-09T12:34:56Z"
        }
        ```

    Example response:
        ```
        {
            "status": "received",
            "webhook_id": "wh_def456...",
            "message": "Policy status change recorded"
        }
        ```
    """
    from uuid import uuid4

    webhook_id = f"wh_{uuid4().hex[:12]}"
    received_at = datetime.utcnow().isoformat()

    log.info(
        "webhook_received",
        webhook_id=webhook_id,
        event="policy_status_changed",
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
    Always returns 200 OK.

    Example request:
        ```
        POST /internal/hooks/test
        ```

    Example response:
        ```
        {
            "status": "ok",
            "message": "Webhook endpoint is reachable"
        }
        ```
    """
    log.info("webhook_test_received")
    return {
        "status": "ok",
        "message": "Webhook endpoint is reachable",
        "timestamp": datetime.utcnow().isoformat(),
    }
