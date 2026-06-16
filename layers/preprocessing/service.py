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


def _quality_metrics_payload(reports: list[QualityReport]) -> list[dict]:
    """Per-page метрики для claim_documents.quality_metrics (миграция 009)."""
    return [
        {
            "page": i + 1,
            "resolution_dpi": round(r.resolution_dpi, 1),
            "blur_score": round(r.blur_score, 2),
            "brightness": round(r.brightness, 1),
            "skew_angle": round(r.skew_angle, 2),
            "score": r.score,
            "flags": r.flags,
        }
        for i, r in enumerate(reports)
    ]


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


def deskew_image(image: np.ndarray, angle: float, expand: bool = False) -> np.ndarray:
    """
    Выравнивает изображение по углу наклона.

    expand=True: увеличивает холст так чтобы всё содержимое поместилось после поворота.
    Используется для больших углов (>15°) — иначе текст обрезается по краям кадра.
    """
    import cv2
    h, w = image.shape[:2]
    center = (w / 2, h / 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)

    if expand and abs(angle) > 10:
        cos_a = abs(M[0, 0])
        sin_a = abs(M[0, 1])
        new_w = int(h * sin_a + w * cos_a)
        new_h = int(h * cos_a + w * sin_a)
        M[0, 2] += (new_w - w) / 2
        M[1, 2] += (new_h - h) / 2
        return cv2.warpAffine(
            image, M, (new_w, new_h),
            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
        )

    return cv2.warpAffine(
        image, M, (w, h),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
    )


def normalize_orientation(
    image: np.ndarray,
    raw_bytes: bytes | None = None,
) -> tuple[np.ndarray, str]:
    """
    Нормализует ориентацию изображения перед quality gate.

    Два метода применяются последовательно:
    1. EXIF: читает тег Orientation (274) из JPEG/PNG метаданных.
       Покрывает большинство случаев — Android и iOS всегда пишут EXIF.
    2. Морфологический анализ: определяет 90°/270° поворот по соотношению
       горизонтальных и вертикальных текстовых линий. Fallback когда EXIF
       отсутствует или вырезан конвертером.

    180° поворот морфологически не определяется (требует OCR) — пользователь
    получит ошибку quality gate и переснимет документ.

    Fail-safe: при любом сомнении изображение не изменяется.

    Returns:
        (corrected_image, label) где label:
        "none" | "exif_cw90" | "exif_ccw90" | "exif_180" |
        "morph_cw90" | "morph_ccw90"
    """
    import cv2

    # ── 1. EXIF (только для JPEG/PNG, raw_bytes передаётся из preprocess_document) ──
    if raw_bytes is not None:
        cv2_rot, label = _exif_rotation(raw_bytes)
        if cv2_rot is not None:
            corrected = cv2.rotate(image, cv2_rot)
            log.info("orientation_corrected", method="exif", label=label)
            return corrected, label

    # ── 2. Морфологический анализ ─────────────────────────────────────────────────
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    cv2_rot, label = _morph_rotation(gray)
    if cv2_rot is not None:
        corrected = cv2.rotate(image, cv2_rot)
        log.info("orientation_corrected", method="morphology", label=label)
        return corrected, label

    return image, "none"


def _exif_rotation(raw_bytes: bytes) -> tuple:
    """
    Возвращает (cv2_rotation_constant, label) или (None, "none").

    EXIF Orientation tag 274:
      3 = перевёрнут 180°
      6 = сохранён CCW, нужен CW для исправления   (телефон повёрнут CCW при съёмке)
      8 = сохранён CW,  нужен CCW для исправления  (телефон повёрнут CW при съёмке)
    """
    import cv2
    try:
        from PIL import Image as _PILImage
        pil = _PILImage.open(io.BytesIO(raw_bytes))
        exif = pil.getexif()
        orientation = exif.get(274)
        mapping = {
            3: (cv2.ROTATE_180,                 "exif_180"),
            6: (cv2.ROTATE_90_CLOCKWISE,         "exif_cw90"),
            8: (cv2.ROTATE_90_COUNTERCLOCKWISE,  "exif_ccw90"),
        }
        result = mapping.get(orientation)
        return result if result else (None, "none")
    except Exception:
        return None, "none"


def _morph_rotation(gray: np.ndarray) -> tuple:
    """
    Определяет 90°/270° поворот по соотношению текстовых линий.

    Алгоритм:
    - Морфологические маски горизонтальных и вертикальных линий.
    - Если вертикальные доминируют (ratio > 1.5 и > 500 px) → документ повёрнут.
    - Направление: у медформ заголовок сверху → плотность выше у верхнего края.
      Сравниваем левую/правую половину вертикальной маски:
        правая > левой  → верх документа справа  → повёрнут CW  → fix: CCW
        левая  > правой → верх документа слева   → повёрнут CCW → fix: CW
    - Если разница < 20% — неопределённо, не трогаем.

    Возвращает (cv2_rotation_constant, label) или (None, "none").
    """
    import cv2
    h, w = gray.shape
    if h < 100 or w < 100:
        return None, "none"

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    kw = max(25, w // 8)
    kh = max(25, h // 8)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kh))

    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    h_score = cv2.countNonZero(h_lines)
    v_score = cv2.countNonZero(v_lines)

    if v_score < 500 or v_score <= h_score * 1.5:
        return None, "none"

    left_score  = cv2.countNonZero(v_lines[:, :w // 2])
    right_score = cv2.countNonZero(v_lines[:, w // 2:])
    total = left_score + right_score
    if total == 0 or abs(left_score - right_score) / total < 0.20:
        return None, "none"

    if right_score > left_score:
        return cv2.ROTATE_90_COUNTERCLOCKWISE, "morph_ccw90"
    else:
        return cv2.ROTATE_90_CLOCKWISE, "morph_cw90"


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

        # Нормализация ориентации + Quality Gate для каждой страницы
        orientation_corrections: list[dict] = []
        all_reports: list[QualityReport] = []
        corrected_pages: list[np.ndarray] = []

        for i, page_img in enumerate(pages):
            # Нормализация ориентации — строго до quality gate.
            # Для JPEG/PNG (i==0): пробуем EXIF из raw_data, затем морфологию.
            # Для PDF-страниц (is_pdf): только морфология (нет EXIF в numpy array).
            raw = raw_data if (not is_pdf and i == 0) else None
            page_img, orient_label = normalize_orientation(page_img, raw_bytes=raw)
            if orient_label != "none":
                orientation_corrections.append({"page": i + 1, "correction": orient_label})
            corrected_pages.append(page_img)  # добавляем; fallback-блок ниже обновит если нужно

            report = check_quality(page_img)

            # Если единственный флаг — cropped (экстремальный skew), пробуем
            # исправить поворотом с расширением холста и перепроверяем.
            # Это ловит случаи когда EXIF стриплен и морфология не сработала.
            if not report.passed and report.flags == ["cropped"] and abs(report.skew_angle) < 85:
                import cv2
                angle = report.skew_angle
                # Для углов близких к ±90° — скорее всего 90° поворот без EXIF:
                # пробуем оба варианта и берём тот где skew после коррекции меньше.
                if abs(abs(angle) - 90) < 30:
                    candidates = [
                        cv2.rotate(page_img, cv2.ROTATE_90_CLOCKWISE),
                        cv2.rotate(page_img, cv2.ROTATE_90_COUNTERCLOCKWISE),
                    ]
                    best_img, best_rep = page_img, report
                    for cand in candidates:
                        rep = check_quality(cand)
                        if rep.passed or (
                            len(rep.flags) < len(best_rep.flags)
                            or abs(rep.skew_angle) < abs(best_rep.skew_angle)
                        ):
                            best_img, best_rep = cand, rep
                    if best_rep.passed:
                        page_img, report = best_img, best_rep
                        orientation_corrections.append({
                            "page": i + 1, "correction": "fallback_90_rotation",
                        })
                        log.info("orientation_corrected", method="fallback_90", page=i + 1)
                else:
                    # Произвольный большой угол — пробуем deskew с расширением холста
                    deskewed = deskew_image(page_img, angle, expand=True)
                    rep_after = check_quality(deskewed)
                    if rep_after.passed:
                        page_img, report = deskewed, rep_after
                        orientation_corrections.append({
                            "page": i + 1,
                            "correction": f"deskew_{angle:.0f}deg_expanded",
                        })
                        log.info("orientation_corrected", method="deskew_expand",
                                 angle=angle, page=i + 1)

            corrected_pages[-1] = page_img  # обновляем уже добавленный элемент
            all_reports.append(report)

            if not report.passed:
                # Берём первый флаг как основную причину
                first_flag = report.flags[0]
                detail = QUALITY_ERROR_MESSAGES.get(first_flag, "Документ не прошёл проверку качества.")

                # Обновляем статус документа (метрики всех проверенных страниц включительно)
                doc.quality_score = report.score
                doc.quality_flags = report.flags
                doc.quality_metrics = _quality_metrics_payload(all_reports)
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
                        "resolution_dpi": round(report.resolution_dpi, 1),
                        "blur_score": report.blur_score,
                        "brightness": report.brightness,
                        "skew_angle": round(report.skew_angle, 2),
                        "orientation_corrections": orientation_corrections,
                    }},
                    duration_ms=timer.duration_ms,
                )

                raise DocumentQualityError(reason=first_flag, detail=detail)

        # Все страницы прошли — обрабатываем уже откорректированные изображения
        page_paths: list[str] = []
        for i, (page_img, report) in enumerate(zip(corrected_pages, all_reports)):
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
        # Сырые per-page метрики сохраняем даже при успехе —
        # около-пороговые значения нужны аналитике и петле обучения
        doc.quality_metrics = _quality_metrics_payload(all_reports)
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
            "quality_metrics": _quality_metrics_payload(all_reports),
            "orientation_corrections": orientation_corrections,
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
