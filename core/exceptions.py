"""
core/exceptions.py — иерархия исключений системы.

Правило: каждый слой бросает только исключения из этого файла.
Не используй встроенные ValueError/RuntimeError в бизнес-логике.
"""


class ClaimsBaseError(Exception):
    """Базовый класс всех ошибок системы."""


# ── Полис / Клиент ────────────────────────────────────────────────

class PolicyNotFoundError(ClaimsBaseError):
    """Полис не найден в кор-системе по указанному личному номеру."""


class PolicyInactiveError(ClaimsBaseError):
    """Полис найден, но неактивен на дату события."""
    def __init__(self, policy_number: str, event_date: str):
        self.policy_number = policy_number
        self.event_date = event_date
        super().__init__(f"Policy {policy_number} inactive on {event_date}")


class PolicyLimitExhaustedError(ClaimsBaseError):
    """Годовой лимит по полису исчерпан."""
    def __init__(self, policy_number: str, remaining: float, currency: str):
        self.policy_number = policy_number
        self.remaining = remaining
        self.currency = currency
        super().__init__(f"Policy {policy_number} limit exhausted: {remaining} {currency} remaining")


# ── Документы / Quality Gate ──────────────────────────────────────

class DocumentQualityError(ClaimsBaseError):
    """Документ не прошёл quality gate."""
    def __init__(self, reason: str, detail: str):
        self.reason = reason    # low_resolution | blurry | dark | bright | cropped
        self.detail = detail    # сообщение для клиента
        super().__init__(f"Quality gate failed: {reason} — {detail}")


class UnsupportedFileTypeError(ClaimsBaseError):
    """Формат файла не поддерживается."""
    def __init__(self, mime_type: str):
        self.mime_type = mime_type
        super().__init__(f"Unsupported file type: {mime_type}")


class FileTooLargeError(ClaimsBaseError):
    """Файл превышает максимально допустимый размер."""
    def __init__(self, size_mb: float, max_mb: float):
        self.size_mb = size_mb
        self.max_mb = max_mb
        super().__init__(f"File size {size_mb:.1f} MB exceeds limit {max_mb} MB")


# ── OCR ───────────────────────────────────────────────────────────

class OCRFailedError(ClaimsBaseError):
    """OCR завершился с ошибкой после всех retry."""
    def __init__(self, doc_id: str, reason: str = ""):
        self.doc_id = doc_id
        super().__init__(f"OCR failed for document {doc_id}: {reason}")


class OCRLowConfidenceError(ClaimsBaseError):
    """Средний confidence OCR ниже минимального порога."""
    def __init__(self, doc_id: str, confidence: float, threshold: float):
        self.doc_id = doc_id
        self.confidence = confidence
        self.threshold = threshold
        super().__init__(
            f"OCR confidence {confidence:.2f} < threshold {threshold:.2f} for doc {doc_id}"
        )


# ── Extraction ────────────────────────────────────────────────────

class ExtractionFailedError(ClaimsBaseError):
    """Claude не смог извлечь данные из OCR-текста."""


class CrossValidationError(ClaimsBaseError):
    """Кросс-валидация между документами выявила несоответствие."""
    def __init__(self, field: str, doc1: str, doc2: str, detail: str):
        self.field = field
        self.doc1 = doc1
        self.doc2 = doc2
        self.detail = detail
        super().__init__(f"Cross-validation failed: {field} mismatch between {doc1} and {doc2}: {detail}")


# ── Кор-система ───────────────────────────────────────────────────

class CoreAPIUnavailableError(ClaimsBaseError):
    """Кор-система недоступна после всех retry."""
    def __init__(self, attempts: int = 3, message: str = ""):
        self.attempts = attempts
        super().__init__(message or f"Core API unavailable after {attempts} attempts")


class CoreAPIAuthError(ClaimsBaseError):
    """Ошибка авторизации в кор-системе."""


# ── RAG / Контракты ───────────────────────────────────────────────

class ContractNotIndexedError(ClaimsBaseError):
    """Контракт не проиндексирован в RAG."""
    def __init__(self, policy_number: str):
        self.policy_number = policy_number
        super().__init__(f"Contract not indexed for policy {policy_number}")


class ContractReindexTimeoutError(ClaimsBaseError):
    """Переиндексация контракта превысила таймаут."""
    def __init__(self, policy_number: str, timeout_sec: int):
        self.policy_number = policy_number
        super().__init__(f"Contract reindex timeout ({timeout_sec}s) for policy {policy_number}")


# ── Аудит ─────────────────────────────────────────────────────────

class AuditLogError(ClaimsBaseError):
    """Не удалось записать аудит-лог — критическая ошибка."""
    # Система не должна продолжать работу без аудит-лога
