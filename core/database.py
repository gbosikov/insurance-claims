"""
core/database.py — подключение к PostgreSQL через SQLAlchemy async.
"""

import os
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.config import get_settings

settings = get_settings()

# Celery worker вызывает asyncio.run() по одному разу на задачу.
# Стандартный пул хранит futures, привязанные к первому event loop;
# при следующем asyncio.run() новый loop их не узнаёт →
# RuntimeError "Future attached to a different loop".
# NullPool отключает пулинг: каждый asyncio.run() получает свежее
# соединение и закрывает его по выходу — никаких межлупных утечек.
_is_celery = bool(os.environ.get("CELERY_WORKER"))

if _is_celery:
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(
        settings.database_url,
        echo=settings.environment == "development",
        poolclass=NullPool,
    )
else:
    engine = create_async_engine(
        settings.database_url,
        echo=settings.environment == "development",
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Базовый класс для всех SQLAlchemy-моделей."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: предоставляет сессию БД."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Проверить доступность БД (используется в healthcheck)."""
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
