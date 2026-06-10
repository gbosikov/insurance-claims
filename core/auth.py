"""
core/auth.py — аутентификация API-ключей и tenant resolution.

Каждый запрос к /v1/* должен нести заголовок X-API-Key (настройка api_key_header).
Ключ хэшируется SHA-256 и ищется в platform.api_keys → определяется tenant_id,
скоупы и rate limit. Сам ключ нигде не хранится и не логируется.

Скоупы (deny-by-default — неизвестный путь требует 'admin'):
  claims:read / claims:write       — заявки и апелляции (внешняя медсистема)
  contracts:read / contracts:write — загрузка и индексация контрактов
  analytics:read                   — статистика
  reviews:read / reviews:write     — ручная проверка (ТОЛЬКО операторы —
                                     эти данные питают петлю обучения)

Не-production окружение без заголовка → дефолтный тенант с предупреждением
(как пустой whitelist в downloader). Невалидный ключ → 401 в любом окружении.

/internal/hooks/* не проходят через эту аутентификацию — они защищены
HMAC-подписью кор-системы (webhook_secret_key).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from core.config import get_settings
from core.database import AsyncSessionLocal
from core.models.platform import ApiKey, Tenant

log = structlog.get_logger()
settings = get_settings()

# Единственный тенант до полного онбординга клиентов; также dev-fallback
DEFAULT_TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")

# Пути, не требующие API-ключа
PUBLIC_PATHS = {"/", "/health", "/docs", "/redoc", "/openapi.json"}
# /internal/hooks/* защищены HMAC-подписью (см. routers/webhooks.py)
PUBLIC_PATH_PREFIXES = ("/internal/hooks",)

# Полный набор скоупов — выдаётся dev-fallback'у и ключам с scope 'admin'
ALL_SCOPES = [
    "claims:read", "claims:write",
    "contracts:read", "contracts:write",
    "analytics:read",
    "reviews:read", "reviews:write",
]

_redis = None  # lazy init (паттерн как в rest_adapter)


@dataclass
class AuthContext:
    tenant_id: UUID
    api_key_id: UUID | None
    scopes: list[str] = field(default_factory=list)
    rate_limit_rpm: int = 0
    key_hash: str = ""


class AuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Базовые функции ────────────────────────────────────────────────

def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex ключа — формат platform.api_keys.key_hash."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES)


def required_scope(method: str, path: str) -> str:
    """
    Скоуп, необходимый для запроса. Deny-by-default:
    неизвестный путь требует 'admin' (которого нет в дефолтных ключах).
    """
    is_write = method in ("POST", "PUT", "PATCH", "DELETE")

    if path.startswith("/v1/claims") or path.startswith("/v1/appeals"):
        return "claims:write" if is_write else "claims:read"
    if path.startswith("/v1/contracts"):
        return "contracts:write" if is_write else "contracts:read"
    if path.startswith("/v1/analytics"):
        return "analytics:read"
    if path.startswith("/v1/reviews"):
        return "reviews:write" if is_write else "reviews:read"

    return "admin"


async def authenticate_api_key(db: AsyncSession, raw_key: str) -> AuthContext:
    """
    Проверить ключ против platform.api_keys.

    Поднимает AuthError: 401 (неизвестен/отозван/истёк), 403 (тенант неактивен).
    Обновляет last_used_at (commit — на вызывающем).
    """
    key_hash = hash_api_key(raw_key)

    result = await db.execute(
        select(ApiKey, Tenant)
        .join(Tenant, Tenant.id == ApiKey.tenant_id)
        .where(ApiKey.key_hash == key_hash)
    )
    row = result.first()
    if row is None:
        raise AuthError(401, "Invalid API key")

    api_key, tenant = row
    now = datetime.now(timezone.utc)

    if api_key.revoked_at is not None:
        raise AuthError(401, "API key has been revoked")
    if api_key.expires_at is not None and api_key.expires_at < now:
        raise AuthError(401, "API key has expired")
    if tenant.status != "active":
        raise AuthError(403, "Tenant is not active")

    scopes = list(api_key.scopes or [])
    if "admin" in scopes:
        scopes = list(set(scopes) | set(ALL_SCOPES))

    api_key.last_used_at = now

    return AuthContext(
        tenant_id=api_key.tenant_id,
        api_key_id=api_key.id,
        scopes=scopes,
        rate_limit_rpm=api_key.rate_limit_rpm or settings.api_rate_limit_default_rpm,
        key_hash=key_hash,
    )


async def check_rate_limit(redis_client, key_hash: str, rpm: int) -> bool:
    """
    Fixed-window лимит запросов в минуту (Redis INCR + EXPIRE).

    Redis недоступен → пропускаем (аутентификация уже прошла через БД;
    доступность важнее строгости лимита).
    """
    if redis_client is None or rpm <= 0:
        return True
    try:
        window = int(time.time() // 60)
        redis_key = f"ratelimit:{key_hash[:16]}:{window}"
        count = await redis_client.incr(redis_key)
        if count == 1:
            await redis_client.expire(redis_key, 90)
        return count <= rpm
    except Exception as e:
        log.warning("rate_limit_check_failed_allowing", error=str(e))
        return True


async def get_redis():
    global _redis
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(
                settings.redis_url, decode_responses=True, socket_timeout=2
            )
        except Exception:
            pass
    return _redis


# ── Middleware ─────────────────────────────────────────────────────

class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Аутентификация каждого запроса к /v1/*:
    1. Публичные пути и CORS preflight пропускаются
    2. X-API-Key → SHA-256 → platform.api_keys (revoked/expired/tenant.status)
    3. Проверка скоупа маршрута
    4. Rate limit по api_keys.rate_limit_rpm
    5. tenant_id/scopes → request.state — роутеры читают через get_tenant_id()
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if request.method == "OPTIONS" or is_public_path(path):
            return await call_next(request)

        raw_key = request.headers.get(settings.api_key_header)

        if not raw_key:
            # Не-production без ключа → дефолтный тенант (как пустой whitelist
            # в downloader). Production — строгий отказ.
            if settings.environment != "production":
                log.warning("api_auth_missing_key_dev_fallback", path=path)
                request.state.tenant_id = DEFAULT_TENANT_ID
                request.state.api_key_id = None
                request.state.scopes = list(ALL_SCOPES)
                return await call_next(request)
            return JSONResponse(status_code=401, content={"detail": "API key required"})

        async with AsyncSessionLocal() as db:
            try:
                ctx = await authenticate_api_key(db, raw_key)
                await db.commit()  # last_used_at
            except AuthError as e:
                log.warning("api_auth_failed", path=path, detail=e.detail)
                return JSONResponse(status_code=e.status_code, content={"detail": e.detail})

        scope = required_scope(request.method, path)
        if scope not in ctx.scopes:
            log.warning(
                "api_auth_scope_denied",
                path=path, required=scope, api_key_id=str(ctx.api_key_id),
            )
            return JSONResponse(
                status_code=403,
                content={"detail": f"API key lacks required scope: {scope}"},
            )

        allowed = await check_rate_limit(await get_redis(), ctx.key_hash, ctx.rate_limit_rpm)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )

        request.state.tenant_id = ctx.tenant_id
        request.state.api_key_id = ctx.api_key_id
        request.state.scopes = ctx.scopes
        return await call_next(request)


# ── FastAPI dependencies ───────────────────────────────────────────

def get_tenant_id(request: Request) -> UUID:
    """Тенант текущего запроса (выставлен middleware)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return tenant_id
