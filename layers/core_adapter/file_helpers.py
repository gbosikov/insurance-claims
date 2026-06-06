"""Вспомогательные функции для подготовки файлов в ClaimParsing_UNI."""

from __future__ import annotations

import base64

from core.models.claim import ClaimDocument
from core.storage import StorageClient

# fkind — тип файла в кор-системе Lite GROUP
# TODO: уточнить реальные коды у владельца кор-системы
FKIND_MAP = {
    "form_100":    1,
    "id_document": 2,
    "receipt":     3,
}


async def documents_to_file_fields(
    documents: list[ClaimDocument],
    storage: StorageClient,
) -> list[dict]:
    """
    Конвертировать документы заявки в формат file_fields для ClaimParsing_UNI.
    file_data — base64-encoded содержимое файла.
    """
    file_fields = []
    for doc in documents:
        raw_bytes = await storage.download(doc.storage_path)
        file_fields.append({
            "file_data": base64.b64encode(raw_bytes).decode(),
            "file_name": doc.storage_path.split("/")[-1],
            "fkind":     FKIND_MAP.get(doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type), 1),
        })
    return file_fields
