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

# Полный трейс заявки: make claim-trace CLAIM=<claim_id>
claim-trace:
	@docker compose exec postgres psql -U claims_user -d claims -x -c "\
SELECT \
  to_char(al.timestamp AT TIME ZONE 'Asia/Tbilisi', 'HH24:MI:SS') AS time, \
  al.step, \
  al.duration_ms AS ms, \
  al.input_data, \
  al.output_data \
FROM audit_log al \
WHERE al.claim_id = '$(CLAIM)' \
ORDER BY al.timestamp;"

# Claude запросы/ответы для заявки: make claim-claude CLAIM=<claim_id>
claim-claude:
	@docker compose exec postgres psql -U claims_user -d claims -x -c "\
SELECT \
  al.step, \
  al.duration_ms AS ms, \
  al.input_data->>'model'              AS model, \
  al.input_data->>'use_thinking'       AS thinking, \
  al.input_data->>'user_message_chars' AS msg_chars, \
  al.input_data->>'user_prompt_chars'  AS prompt_chars, \
  al.output_data->>'input_tokens'      AS in_tok, \
  al.output_data->>'output_tokens'     AS out_tok, \
  al.output_data->>'claude_raw_response' AS raw_response \
FROM audit_log al \
WHERE al.claim_id = '$(CLAIM)' \
  AND al.step IN ('extraction','decision','decision_second_pass') \
ORDER BY al.timestamp;"

# Последние 5 заявок: make claims-recent
claims-recent:
	@docker compose exec postgres psql -U claims_user -d claims -c "\
SELECT \
  id, \
  policy_number, \
  status, \
  overall_confidence, \
  to_char(created_at AT TIME ZONE 'Asia/Tbilisi', 'DD.MM HH24:MI') AS created, \
  left(routing_reason, 60) AS routing_reason \
FROM claims \
ORDER BY created_at DESC \
LIMIT 5;"
