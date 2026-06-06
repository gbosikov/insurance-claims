"""
Слой 2 — Preprocessing Service.

Задача: quality gate + подготовка изображений для OCR.

ВАЖНО: quality gate стоит ДО вызова Vision API.
Плохие документы отсекаются здесь с конкретным сообщением для клиента.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from uuid import UUID

import numpy as np
import structlog
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.exceptions import DocumentQualityError
from core.models.claim import ClaimDocument, ClaimStatus
from core.storage import StorageClient

log = structlog.get_logger()
settings = get_settings()

# Сообщения для клиента (по причине отказа quality gate)
QUALITY_ERROR_MESSAGES: dict[str, str] = {
    "low_resolution": "Разрешение слишком низкое. Сфотографируйте документ с расстояния 20–30 см.",
    "blurry":         "Изображение размытое. Удерживайте камеру неподвижно при съёмке.",
    "dark":           "Изображение слишком тёмное. Обеспечьте хорошее освещение.",
    "bright":         "Изображение пересвечено. Избегайте прямого источника света на документ.",
    "cropped":        "Текст обрезан по краям. Убедитесь, что весь документ помещается в кадр.",
}


@dataclass
class QualityReport:
    passed: bool
    score: float                  # 0.0–1.0
    flags: list[str]              # причины отказа
    resolution_dpi: float = 0.0
    blur_score: float = 0.0
    brightness: float = 0.0
    skew_angle: float = 0.0


@dataclass
class PreprocessedDocument:
    doc_id: UUID
    preprocessed_path: str
    quality_report: QualityReport
    page_paths: list[str]         # для многостраничных PDF


def check_quality(image: np.ndarray, dpi: float = 0.0) -> QualityReport:
    """
    Проверяет качество изображения по всем критериям quality gate.
    Возвращает QualityReport с флагами и общим score.
    """
    import cv2

    flags: list[str] = []

    # 1. Разрешение (DPI)
    h, w = image.shape[:2]
    estimated_dpi = dpi or _estimate_dpi(w, h)
    if estimated_dpi < settings.quality_min_resolution_dpi:
        flags.append("low_resolution")

    # 2. Размытость (Laplacian variance — чем меньше, тем размытее)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if blur_score < settings.quality_max_blur_score:
        flags.append("blurry")

    # 3. Яркость (среднее по всем пикселям)
    brightness = float(np.mean(gray))
    if brightness < settings.quality_min_brightness:
        flags.append("dark")
    elif brightness > settings.quality_max_brightness:
        flags.append("bright")

    # 4. Угол наклона текста
    skew_angle = _detect_skew(gray)
    if abs(skew_angle) > settings.quality_max_skew_angle_deg:
        flags.append("cropped")  # экстремальный наклон = скорее всего документ обрезан

    # Итоговый score: 1.0 если нет флагов, уменьшается за каждый флаг
    score = max(0.0, 1.0 - len(flags) * 0.25)

    return QualityReport(
        passed=len(flags) == 0,
        score=score,
        flags=flags,
        resolution_dpi=estimated_dpi,
        blur_score=blur_score,
        brightness=brightness,
        skew_angle=skew_angle,
    )


def _estimate_dpi(width_px: int, height_px: int) -> float:
    """Оценка DPI по предположению, что документ A4 (210×297 мм)."""
    # A4 высота = 11.69 дюйма
    return height_px / 11.69


def _detect_skew(gray: np.ndarray) -> float:
    """Определяет угол наклона изображения через линии Хафа."""
    import cv2
    try:
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLines(edges, 1, np.pi / 180, 200)
        if lines is None:
            return 0.0
        angles = [line[0][1] * 180 / np.pi - 90 for line in lines]
        return float(np.median(angles))
    except Exception:
        return 0.0


def deskew_image(image: np.ndarray, angle: float) -> np.ndarray:
    """Выравнивает изображение по углу наклона."""
    import cv2
    h, w = image.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def enhance_image(image: np.ndarray) -> np.ndarray:
    """
    Улучшение качества изображения:
    - шумоподавление
    - коррекция контраста (CLAHE)
    """
    import cv2
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    # Шумоподавление
    denoised = cv2.fastNlMeansDenoising(gray, h=10)
    # Адаптивное выравнивание гистограммы
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(denoised)
    # Возвращаем в BGR если было цветным
    if len(image.shape) == 3:
        return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    return enhanced


def pdf_to_images(pdf_bytes: bytes) -> list[np.ndarray]:
    """Конвертирует PDF в список изображений (одно на страницу)."""
    import fitz  # pymupdf

    images: list[np.ndarray] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        mat = fitz.Matrix(2.0, 2.0)  # 2x масштаб = ~144 DPI из 72 DPI default
        pix = page.get_pixmap(matrix=mat)
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:  # RGBA → BGR
            import cv2
            img_array = cv2.cvtColor(img_array, cv2.COLOR_RGBA2BGR)
        images.append(img_array)
    return images


async def preprocess_document(
    doc: ClaimDocument,
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
) -> PreprocessedDocument:
    """
    Полный цикл предобработки одного документа.

    1. Скачать оригинал из storage
    2. Если PDF — конвертировать страницы в изображения
    3. Для каждого изображения: quality gate
    4. При провале — DocumentQualityError с причиной для клиента
    5. При успехе — deskew + enhance
    6. Сохранить обработанные изображения в storage
    7. Обновить ClaimDocument.preprocessed_path
    8. Запись в audit_log
    """
    import cv2

    with AuditTimer() as timer:
        raw_data = await storage.download(doc.storage_path)

        # Определяем формат и загружаем
        is_pdf = doc.storage_path.lower().endswith(".pdf") or (
            raw_data[:4] == b"%PDF"
        )

        if is_pdf:
            pages = pdf_to_images(raw_data)
        else:
            # JPEG/PNG
            img_array = np.frombuffer(raw_data, dtype=np.uint8)
            page = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            pages = [page]

        # Quality Gate для каждой страницы
        all_reports: list[QualityReport] = []
        for page_img in pages:
            report = check_quality(page_img)
            all_reports.append(report)

            if not report.passed:
                # Берём первый флаг как основную причину
                first_flag = report.flags[0]
                detail = QUALITY_ERROR_MESSAGES.get(first_flag, "Документ не прошёл проверку качества.")

                # Обновляем статус документа
                doc.quality_score = report.score
                doc.quality_flags = report.flags
                await db.flush()

                await write_audit_entry(
                    db,
                    claim_id=doc.claim_id,
                    tenant_id=tenant_id,
                    step="preprocessing",
                    input_data={"doc_id": str(doc.id), "doc_type": doc.doc_type.value},
                    output_data={"quality_report": {
                        "passed": False,
                        "flags": report.flags,
                        "score": report.score,
                        "blur_score": report.blur_score,
                        "brightness": report.brightness,
                    }},
                    duration_ms=timer.duration_ms,
                )

                raise DocumentQualityError(reason=first_flag, detail=detail)

        # Все страницы прошли — обрабатываем
        page_paths: list[str] = []
        for i, (page_img, report) in enumerate(zip(pages, all_reports)):
            # Deskew
            if abs(report.skew_angle) > 2.0:
                page_img = deskew_image(page_img, report.skew_angle)

            # Enhance
            page_img = enhance_image(page_img)

            # Сохраняем обработанную страницу
            success, encoded = cv2.imencode(".png", page_img)
            if not success:
                continue

            page_filename = f"preprocessed_page_{i}.png"
            page_path = storage.generate_path(
                tenant_id=str(tenant_id),
                claim_id=str(doc.claim_id),
                filename=f"pp_{doc.id}_{page_filename}",
            )
            await storage.upload(encoded.tobytes(), page_path, content_type="image/png")
            page_paths.append(page_path)

        # Обновляем документ
        best_report = min(all_reports, key=lambda r: r.score)
        doc.preprocessed_path = page_paths[0] if page_paths else doc.storage_path
        doc.quality_score = best_report.score
        doc.quality_flags = []
        await db.flush()

    await write_audit_entry(
        db,
        claim_id=doc.claim_id,
        tenant_id=tenant_id,
        step="preprocessing",
        input_data={"doc_id": str(doc.id), "doc_type": doc.doc_type.value, "pages": len(pages)},
        output_data={
            "quality_passed": True,
            "pages_processed": len(page_paths),
            "quality_scores": [r.score for r in all_reports],
        },
        duration_ms=timer.duration_ms,
    )

    return PreprocessedDocument(
        doc_id=doc.id,
        preprocessed_path=page_paths[0] if page_paths else doc.storage_path,
        quality_report=all_reports[0],
        page_paths=page_paths,
    )


async def preprocess_all_documents(
    documents: list[ClaimDocument],
    storage: StorageClient,
    db: AsyncSession,
    tenant_id: UUID,
) -> list[PreprocessedDocument]:
    """Последовательная предобработка всех документов заявки."""
    results: list[PreprocessedDocument] = []
    for doc in documents:
        result = await preprocess_document(doc, storage, db, tenant_id)
        results.append(result)
    return results
