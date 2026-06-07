-- ═══════════════════════════════════════════════════════════════════
-- 002_doc_type_training.sql
-- Добавляет поля для отслеживания источника типа документа
-- и сбора обучающих данных для будущего ML-классификатора.
-- ═══════════════════════════════════════════════════════════════════

ALTER TABLE claim_documents
    ADD COLUMN IF NOT EXISTS doc_type_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS doc_type_source    VARCHAR(30) NOT NULL DEFAULT 'filename_hint';

-- filename_hint  — определён по имени файла (Layer 1,ненадёжный)
-- ocr_rules      — переопределён по OCR-тексту (Layer 4, надёжный)
-- operator       — подтверждён оператором вручную (100% достоверно)

COMMENT ON COLUMN claim_documents.doc_type_confirmed IS
    'TRUE если тип документа верифицирован (auto_approved или оператором)';

COMMENT ON COLUMN claim_documents.doc_type_source IS
    'Источник определения типа: filename_hint | ocr_rules | operator';

-- Индекс для быстрого экспорта обучающей выборки
CREATE INDEX IF NOT EXISTS idx_claim_documents_training
    ON claim_documents (doc_type_confirmed, doc_type_source)
    WHERE doc_type_confirmed = TRUE;
