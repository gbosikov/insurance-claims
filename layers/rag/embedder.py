"""
layers/5_rag/embedder.py — локальная модель эмбеддингов.

Модель: intfloat/multilingual-e5-large
Поддерживает: русский + грузинский + английский без дополнительной настройки.
Размерность: 1024 (соответствует vector(1024) в PostgreSQL).

Модель загружается ОДИН РАЗ при старте worker-сервиса.
Не создавай новый экземпляр на каждый запрос.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()

_model = None


def _load_model():
    """Ленивая загрузка модели при первом обращении (~1.1 GB, ~30 сек)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        from core.config import get_settings
        settings = get_settings()
        log.info("loading_embedding_model", model=settings.embedding_model)
        _model = SentenceTransformer(settings.embedding_model)
        log.info("embedding_model_loaded")
    return _model


def get_embedding(text: str, is_query: bool = False) -> list[float]:
    """
    Получить эмбеддинг текста.

    multilingual-e5-large требует префикс:
    - "query: "   для поисковых запросов
    - "passage: " для индексируемых текстов

    Args:
        text: текст для векторизации
        is_query: True если это поисковый запрос, False если индексируемый текст
    """
    model = _load_model()
    prefix = "query: " if is_query else "passage: "
    embedding = model.encode(prefix + text, normalize_embeddings=True)
    return embedding.tolist()


def get_embeddings_batch(texts: list[str], is_query: bool = False) -> list[list[float]]:
    """Пакетная векторизация — эффективнее одиночных вызовов."""
    model = _load_model()
    prefix = "query: " if is_query else "passage: "
    prefixed = [prefix + t for t in texts]
    embeddings = model.encode(prefixed, normalize_embeddings=True, batch_size=32)
    return [e.tolist() for e in embeddings]
