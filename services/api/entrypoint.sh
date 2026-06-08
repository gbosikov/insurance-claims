#!/bin/bash
# Entrypoint для API-сервиса.
# Запускается перед uvicorn: загружает справочник МКБ-10 если таблица пустая.
set -e

ICD10_FILE="/app/db/data/ICD10.csv"

if [ -f "$ICD10_FILE" ]; then
    echo "[entrypoint] Проверяю справочник МКБ-10..."
    python -m db.loaders.load_icd10 --file "$ICD10_FILE" --skip-if-loaded
else
    echo "[entrypoint] Файл $ICD10_FILE не найден, пропускаю загрузку МКБ-10."
fi

exec "$@"
