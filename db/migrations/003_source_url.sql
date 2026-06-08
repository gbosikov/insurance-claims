-- Миграция 003: поддержка URL-based приёма документов
--
-- Изменения:
--   1. claim_documents.storage_path — снимаем NOT NULL
--      (до скачивания файла реального пути нет)
--   2. claim_documents.source_url — URL откуда скачивать файл
--      (заполняется при intake; после скачивания хранится как audit trail)

ALTER TABLE claim_documents
    ALTER COLUMN storage_path DROP NOT NULL;

ALTER TABLE claim_documents
    ADD COLUMN IF NOT EXISTS source_url TEXT;

COMMENT ON COLUMN claim_documents.source_url IS
    'Pre-signed URL внешней системы. Заполняется при intake, файл скачивается в worker (шаг 0).';

COMMENT ON COLUMN claim_documents.storage_path IS
    'Путь в нашем storage (GCS/local). NULL до скачивания файла.';
