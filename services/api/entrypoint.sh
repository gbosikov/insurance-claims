#!/bin/bash
# Entrypoint для API-сервиса.
# Запускается перед uvicorn: применяет миграции, загружает справочники.
set -e

# ── Миграции схемы (Alembic) ─────────────────────────────────────────
# На свежей БД ревизия 0001 применит схему 001-007; на БД, созданной
# docker-entrypoint-initdb.d, она пропустится (см. alembic/versions/0001).
echo "[entrypoint] Применяю миграции (alembic upgrade head)..."
alembic upgrade head

# ── Справочник МКБ-10 ────────────────────────────────────────────────
ICD10_FILE="/app/db/data/ICD10.csv"

if [ -f "$ICD10_FILE" ]; then
    echo "[entrypoint] Проверяю справочник МКБ-10..."
    python -m db.loaders.load_icd10 --file "$ICD10_FILE" --skip-if-loaded
else
    echo "[entrypoint] Файл $ICD10_FILE не найден, пропускаю загрузку МКБ-10."
fi

# ── Справочник провайдеров ───────────────────────────────────────────
# Принимаем оба имени: providers.csv (стандарт) и Cliniks.csv (исходник Lite GROUP)
PROVIDERS_FILE=""
for _candidate in "/app/db/data/providers.csv" "/app/db/data/Cliniks.csv"; do
    if [ -f "$_candidate" ]; then
        PROVIDERS_FILE="$_candidate"
        break
    fi
done

if [ -n "$PROVIDERS_FILE" ]; then
    echo "[entrypoint] Проверяю справочник провайдеров ($PROVIDERS_FILE)..."
    python -m db.loaders.load_providers --file "$PROVIDERS_FILE" --skip-if-loaded
else
    echo "[entrypoint] Файл providers.csv / Cliniks.csv не найден, пропускаю загрузку провайдеров."
fi

exec "$@"
