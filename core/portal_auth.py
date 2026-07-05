"""
core/portal_auth.py — JWT-аутентификация для веб-портала.

Полностью независима от core/auth.py (API-ключи для machine-to-machine).
Использует python-jose (HS256) + passlib (bcrypt).

Маршруты портала (AUTH_PATH_PREFIXES в core/auth.py):
  /auth/*           — публичный эндпоинт (логин)
  /v1/dashboard/*   — защищено через Depends(get_current_portal_user)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import bcrypt as _bcrypt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from core.config import get_settings

log = structlog.get_logger()
settings = get_settings()

_ALGORITHM = "HS256"

bearer_scheme = HTTPBearer(auto_error=False)


# ── Pydantic schemas ───────────────────────────────────────────────

class UserInToken(BaseModel):
    user_id: UUID
    tenant_id: UUID
    email: str
    role: str
    full_name: str | None = None


# ── Password utilities ─────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def hash_password(plain: str) -> str:
    salt = _bcrypt.gensalt()
    return _bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


# ── JWT utilities ──────────────────────────────────────────────────

def create_access_token(
    user_id: UUID,
    tenant_id: UUID,
    email: str,
    role: str,
    full_name: str | None = None,
) -> str:
    """Создать JWT токен для пользователя портала."""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.portal_jwt_expire_hours)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "email": email,
        "role": role,
        "full_name": full_name,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


# ── FastAPI dependency ─────────────────────────────────────────────

async def get_current_portal_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UserInToken:
    """
    Dependency для роутов /v1/dashboard/*.
    Читает JWT из заголовка Authorization: Bearer <token>.
    Возвращает UserInToken или выбрасывает 401.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.secret_key,
            algorithms=[_ALGORITHM],
        )
        user_id = payload.get("sub")
        tenant_id = payload.get("tenant_id")
        email = payload.get("email")
        role = payload.get("role")

        if not user_id or not tenant_id:
            raise ValueError("missing claims")

        return UserInToken(
            user_id=UUID(user_id),
            tenant_id=UUID(tenant_id),
            email=email or "",
            role=role or "viewer",
            full_name=payload.get("full_name"),
        )

    except (JWTError, ValueError) as exc:
        log.warning("portal_auth_invalid_token", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
