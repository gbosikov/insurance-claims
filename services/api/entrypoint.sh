#!/bin/bash
# Entrypoint для API-сервиса.
# Запускается перед uvicorn: загружает справочники если таблицы пустые.
set -e

# ── Справочник МКБ-10 ────────────────────────────────────────────────
ICD10_FILE="/app/db/data/ICD10.csv"

if [ -f "$ICD10_FILE" ]; then
    echo "[entrypoint] Проверяю справочник МКБ-10..."
    python -m db.loaders.load_icd10 --file "$ICD10_FILE" --skip-if-loaded
else
    echo "[entrypoint] Файл $ICD10_FILE не найден, пропускаю загрузку МКБ-10."
fi

# ── Справочник провайдеров ───────────────────────────────────────────
PROVIDERS_FILE="/app/db/data/providers.csv"

if [ -f "$PROVIDERS_FILE" ]; then
    echo "[entrypoint] Проверяю справочник провайдеров..."
    python -m db.loaders.load_providers --file "$PROVIDERS_FILE" --skip-if-loaded
else
    echo "[entrypoint] Файл $PROVIDERS_FILE не найден, пропускаю загрузку провайдеров."
fi

exec "$@"
