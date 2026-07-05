"""
Router: /auth — аутентификация веб-портала (JWT).

Маршруты публичные (не требуют X-API-Key) — добавлены в
PUBLIC_PATH_PREFIXES в core/auth.py.

POST /auth/login  — email + password → JWT token
GET  /auth/me     — текущий пользователь (по JWT)
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.models.platform import Tenant
from core.models.user import PortalUser
from core.portal_auth import (
    UserInToken,
    create_access_token,
    get_current_portal_user,
    verify_password,
)

log = structlog.get_logger()
router = APIRouter()


# ── Schemas ────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


# ── Endpoints ─────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """Войти в портал. Возвращает JWT Bearer-токен."""
    # Нормализуем email
    email = body.email.strip().lower()

    user = (await db.execute(
        select(PortalUser).where(
            PortalUser.email == email,
            PortalUser.is_active.is_(True),
        )
    )).scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        log.warning("portal_login_failed", email=email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    # Обновляем last_login_at
    user.last_login_at = datetime.now(timezone.utc)

    token = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        role=user.role,
        full_name=user.full_name,
    )

    log.info("portal_login_success", user_id=str(user.id), role=user.role)

    return TokenResponse(
        access_token=token,
        user={
            "id": str(user.id),
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "tenant_id": str(user.tenant_id),
        },
    )


@router.get("/me")
async def get_me(
    current_user: UserInToken = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Данные текущего авторизованного пользователя."""
    user = (await db.execute(
        select(PortalUser, Tenant)
        .join(Tenant, Tenant.id == PortalUser.tenant_id)
        .where(PortalUser.id == current_user.user_id)
    )).first()

    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    portal_user, tenant = user
    return {
        "id": str(portal_user.id),
        "email": portal_user.email,
        "full_name": portal_user.full_name,
        "role": portal_user.role,
        "tenant_id": str(portal_user.tenant_id),
        "tenant_name": tenant.name,
        "last_login_at": portal_user.last_login_at.isoformat() if portal_user.last_login_at else None,
    }
