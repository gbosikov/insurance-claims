"""
FastAPI Gateway — главная точка входа.

Роуты:
  POST   /v1/claims              — создать заявку
  GET    /v1/claims/{id}         — статус заявки
  GET    /v1/claims/{id}/audit   — аудит-лог
  POST   /v1/claims/{id}/appeal  — апелляция
  POST   /v1/contracts           — загрузить контракт
  GET    /v1/analytics/summary   — статистика
"""

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import get_settings
from core.database import check_db_connection
from services.api.routers import analytics, appeals, claims, contracts, webhooks

log = structlog.get_logger()
settings = get_settings()

app = FastAPI(
    title="Insurance Claims Processing System",
    version="1.0.0",
    description="Автоматизированная обработка страховых требований ДМС",
    docs_url="/docs" if settings.environment == "development" else None,
    redoc_url="/redoc" if settings.environment == "development" else None,
)

# ── CORS ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"] if settings.environment == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Роуты ─────────────────────────────────────────────────────────
app.include_router(claims.router,    prefix="/v1/claims",    tags=["claims"])
app.include_router(contracts.router, prefix="/v1/contracts", tags=["contracts"])
app.include_router(appeals.router,   prefix="/v1/appeals",   tags=["appeals"])
app.include_router(analytics.router, prefix="/v1/analytics", tags=["analytics"])
app.include_router(webhooks.router,  prefix="/internal/hooks", tags=["webhooks"])


# ── Healthcheck ───────────────────────────────────────────────────
@app.get("/health", tags=["system"])
async def health() -> dict:
    db_ok = await check_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "environment": settings.environment,
    }


@app.get("/", tags=["system"])
async def root() -> dict:
    return {"service": "Insurance Claims Processing System", "version": "1.0.0"}


# ── Глобальный обработчик ошибок ─────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
