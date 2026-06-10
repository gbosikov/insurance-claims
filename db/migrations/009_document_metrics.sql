-- Миграция 009: персистентность метрик качества и OCR-блоков (Фаза 1).
--
-- Раньше pipeline вычислял per-page метрики качества (DPI, blur, brightness, skew)
-- и per-block OCR confidence, но сохранял только агрегаты (quality_score, ocr_confidence).
-- Эти данные нужны для: пост-аудита, аналитики ошибок (claude_error_reason='ocr_quality'),
-- антифрода и подготовки данных для ML (Шаги 34-35).
--
-- На существующей БД применять вручную: make psql < db/migrations/009_document_metrics.sql

ALTER TABLE claim_documents
    -- per-page метрики качества из preprocessing:
    -- [{"page": 1, "resolution_dpi": 200.0, "blur_score": 150.2, "brightness": 128.0,
    --   "skew_angle": 1.5, "score": 0.95, "flags": []}]
    ADD COLUMN IF NOT EXISTS quality_metrics JSONB,
    -- OCR-блоки с confidence и bounding box:
    -- {"strategy": "vision_text_detection", "low_confidence_blocks": 2,
    --  "blocks": [{"text": "...", "confidence": 0.95, "bbox": [{"x":0,"y":0},...]}]}
    ADD COLUMN IF NOT EXISTS ocr_blocks JSONB;
