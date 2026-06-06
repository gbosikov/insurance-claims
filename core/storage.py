"""
core/storage.py — абстракция над хранилищем документов.

Поддерживает: local (dev), GCS, S3.
Все файлы хранятся зашифрованными (AES-256 на стороне провайдера).
"""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import structlog

from core.config import get_settings

log = structlog.get_logger()
settings = get_settings()


class StorageClient(ABC):
    """Абстрактный интерфейс хранилища."""

    @abstractmethod
    async def upload(self, data: bytes, destination: str, content_type: str = "application/octet-stream") -> str:
        """Загрузить файл. Возвращает storage_path."""

    @abstractmethod
    async def download(self, storage_path: str) -> bytes:
        """Скачать файл по storage_path."""

    @abstractmethod
    async def delete(self, storage_path: str) -> None:
        """Удалить файл."""

    def generate_path(self, tenant_id: str, claim_id: str, filename: str) -> str:
        """Генерирует унифицированный путь: tenants/{tenant_id}/claims/{claim_id}/{uuid}_{filename}"""
        unique = uuid.uuid4().hex[:8]
        safe_name = Path(filename).name  # убираем directory traversal
        return f"tenants/{tenant_id}/claims/{claim_id}/{unique}_{safe_name}"


class LocalStorageClient(StorageClient):
    """Локальное хранилище — только для dev/тестов."""

    def __init__(self, base_dir: str = "./storage"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def upload(self, data: bytes, destination: str, content_type: str = "application/octet-stream") -> str:
        path = self.base_dir / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.debug("local_storage_upload", path=str(path), size=len(data))
        return destination

    async def download(self, storage_path: str) -> bytes:
        path = self.base_dir / storage_path
        if not path.exists():
            raise FileNotFoundError(f"File not found in local storage: {storage_path}")
        return path.read_bytes()

    async def delete(self, storage_path: str) -> None:
        path = self.base_dir / storage_path
        if path.exists():
            path.unlink()


class GCSStorageClient(StorageClient):
    """Google Cloud Storage клиент (используется ADC)."""

    def __init__(self, bucket_name: str):
        from google.cloud import storage as gcs
        self._client = gcs.Client()
        self._bucket = self._client.bucket(bucket_name)

    async def upload(self, data: bytes, destination: str, content_type: str = "application/octet-stream") -> str:
        blob = self._bucket.blob(destination)
        # AES-256 шифрование на стороне GCS (Server-Side Encryption)
        blob.upload_from_string(data, content_type=content_type)
        log.info("gcs_upload", destination=destination, size=len(data))
        return destination

    async def download(self, storage_path: str) -> bytes:
        blob = self._bucket.blob(storage_path)
        return blob.download_as_bytes()

    async def delete(self, storage_path: str) -> None:
        blob = self._bucket.blob(storage_path)
        blob.delete()


def get_storage_client() -> StorageClient:
    """Фабрика: возвращает нужный клиент по конфигурации."""
    provider = settings.storage_provider.lower()

    if provider == "local":
        return LocalStorageClient(base_dir="./storage")
    elif provider == "gcs":
        return GCSStorageClient(bucket_name=settings.storage_bucket)
    elif provider == "s3":
        raise NotImplementedError("S3 storage not implemented yet")
    else:
        raise ValueError(f"Unknown storage provider: {provider}")
