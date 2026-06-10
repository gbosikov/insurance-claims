.PHONY: up down logs migrate test lint shell psql env

env:
	cp .env.example .env
	@echo "Скопирован .env.example → .env. Заполни значения перед запуском."

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

migrate:
	docker compose exec api alembic upgrade head

migrate-status:
	docker compose exec api alembic current

migrate-new:
	@echo "docker compose exec api alembic revision -m \"описание\"  (или --autogenerate)"

test:
	docker compose exec api pytest tests/ -v --tb=short

lint:
	docker compose exec api ruff check . && docker compose exec api mypy .

shell:
	docker compose exec api python

psql:
	docker compose exec postgres psql -U claims_user -d claims

check-google-auth:
	docker compose exec api python -c "\
import google.auth; \
credentials, project = google.auth.default(); \
print('OK — project:', project); \
print('Credentials type:', type(credentials).__name__)"

worker-logs:
	docker compose logs -f worker beat

build:
	docker compose build

restart:
	docker compose restart api worker

status:
	docker compose ps
