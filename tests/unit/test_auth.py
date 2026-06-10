"""Unit тесты: core/auth.py — API-ключи, скоупы, rate limiting."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.auth import (
    ALL_SCOPES,
    AuthError,
    authenticate_api_key,
    check_rate_limit,
    hash_api_key,
    is_public_path,
    required_scope,
)

TENANT_ID = uuid4()
KEY_ID = uuid4()


# ── hash_api_key ──────────────────────────────────────────────────


def test_hash_is_deterministic_sha256_hex():
    h1 = hash_api_key("icps_production_abc123")
    h2 = hash_api_key("icps_production_abc123")
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_hash_differs_for_different_keys():
    assert hash_api_key("key-a") != hash_api_key("key-b")


# ── is_public_path ────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/health", "/docs", "/redoc", "/openapi.json"])
def test_public_paths_are_public(path):
    assert is_public_path(path) is True


@pytest.mark.parametrize("path", [
    "/internal/hooks/contract-updated",
    "/internal/hooks/policy-status-changed",
])
def test_internal_hooks_are_public(path):
    """Webhooks защищены HMAC-подписью, не API-ключом."""
    assert is_public_path(path) is True


@pytest.mark.parametrize("path", [
    "/v1/claims", "/v1/reviews", "/v1/analytics/summary", "/v1/whatever",
])
def test_api_paths_are_not_public(path):
    assert is_public_path(path) is False


# ── required_scope ────────────────────────────────────────────────


@pytest.mark.parametrize("method,path,scope", [
    ("POST", "/v1/claims", "claims:write"),
    ("GET",  "/v1/claims/123", "claims:read"),
    ("GET",  "/v1/claims/123/audit", "claims:read"),
    ("POST", "/v1/appeals", "claims:write"),
    ("POST", "/v1/contracts", "contracts:write"),
    ("GET",  "/v1/contracts/DMC-001", "contracts:read"),
    ("POST", "/v1/contracts/DMC-001/reindex", "contracts:write"),
    ("GET",  "/v1/analytics/summary", "analytics:read"),
    ("GET",  "/v1/reviews", "reviews:read"),
    ("POST", "/v1/reviews/123/outcome", "reviews:write"),
])
def test_scope_mapping(method, path, scope):
    assert required_scope(method, path) == scope


def test_unknown_path_requires_admin():
    """Deny-by-default: неизвестный маршрут требует admin-скоупа."""
    assert required_scope("GET", "/v1/future-endpoint") == "admin"


def test_default_key_scopes_cannot_touch_reviews():
    """Дефолтный ключ медсистемы (claims:*) не имеет доступа к данным операторов."""
    default_scopes = ["claims:write", "claims:read"]
    assert required_scope("POST", "/v1/reviews/x/outcome") not in default_scopes
    assert required_scope("GET", "/v1/reviews") not in default_scopes


# ── authenticate_api_key ──────────────────────────────────────────


def make_db(api_key=None, tenant=None):
    """Mock db: execute().first() → (api_key, tenant) или None."""
    result = MagicMock()
    result.first.return_value = (api_key, tenant) if api_key is not None else None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def make_key_row(
    revoked_at=None,
    expires_at=None,
    scopes=None,
    rpm=60,
    tenant_status="active",
):
    api_key = MagicMock()
    api_key.id = KEY_ID
    api_key.tenant_id = TENANT_ID
    api_key.revoked_at = revoked_at
    api_key.expires_at = expires_at
    api_key.scopes = scopes if scopes is not None else ["claims:write", "claims:read"]
    api_key.rate_limit_rpm = rpm
    tenant = MagicMock()
    tenant.id = TENANT_ID
    tenant.status = tenant_status
    return api_key, tenant


@pytest.mark.asyncio
async def test_authenticate_valid_key():
    api_key, tenant = make_key_row()
    db = make_db(api_key, tenant)

    ctx = await authenticate_api_key(db, "icps_production_valid")

    assert ctx.tenant_id == TENANT_ID
    assert ctx.api_key_id == KEY_ID
    assert ctx.scopes == ["claims:write", "claims:read"]
    assert ctx.rate_limit_rpm == 60
    # last_used_at обновлён
    assert api_key.last_used_at is not None


@pytest.mark.asyncio
async def test_authenticate_unknown_key_401():
    db = make_db(None)
    with pytest.raises(AuthError) as exc:
        await authenticate_api_key(db, "icps_production_unknown")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_authenticate_revoked_key_401():
    api_key, tenant = make_key_row(revoked_at=datetime.now(timezone.utc))
    db = make_db(api_key, tenant)
    with pytest.raises(AuthError) as exc:
        await authenticate_api_key(db, "k")
    assert exc.value.status_code == 401
    assert "revoked" in exc.value.detail


@pytest.mark.asyncio
async def test_authenticate_expired_key_401():
    api_key, tenant = make_key_row(
        expires_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    db = make_db(api_key, tenant)
    with pytest.raises(AuthError) as exc:
        await authenticate_api_key(db, "k")
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


@pytest.mark.asyncio
async def test_authenticate_not_yet_expired_key_passes():
    api_key, tenant = make_key_row(
        expires_at=datetime.now(timezone.utc) + timedelta(days=30)
    )
    db = make_db(api_key, tenant)
    ctx = await authenticate_api_key(db, "k")
    assert ctx.tenant_id == TENANT_ID


@pytest.mark.asyncio
async def test_authenticate_suspended_tenant_403():
    api_key, tenant = make_key_row(tenant_status="suspended")
    db = make_db(api_key, tenant)
    with pytest.raises(AuthError) as exc:
        await authenticate_api_key(db, "k")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_scope_expands_to_all():
    api_key, tenant = make_key_row(scopes=["admin"])
    db = make_db(api_key, tenant)
    ctx = await authenticate_api_key(db, "k")
    for scope in ALL_SCOPES:
        assert scope in ctx.scopes


# ── check_rate_limit ──────────────────────────────────────────────


def make_redis(counts: list[int]):
    redis = AsyncMock()
    redis.incr = AsyncMock(side_effect=counts)
    redis.expire = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_rate_limit_allows_within_rpm():
    redis = make_redis([1])
    assert await check_rate_limit(redis, "hash", rpm=60) is True
    redis.expire.assert_awaited_once()  # TTL ставится на первом запросе окна


@pytest.mark.asyncio
async def test_rate_limit_denies_over_rpm():
    redis = make_redis([61])
    assert await check_rate_limit(redis, "hash", rpm=60) is False


@pytest.mark.asyncio
async def test_rate_limit_allows_when_redis_unavailable():
    """Redis недоступен → пропускаем (auth уже прошла через БД)."""
    assert await check_rate_limit(None, "hash", rpm=60) is True

    broken = AsyncMock()
    broken.incr = AsyncMock(side_effect=ConnectionError("down"))
    assert await check_rate_limit(broken, "hash", rpm=60) is True
