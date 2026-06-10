"""
Celery application — конфигурация очереди задач.
"""

from celery import Celery
from celery.schedules import crontab

from core.config import get_settings
from core.logging import configure_logging

configure_logging()  # JSON-логи + маскирование ПД (worker и beat)
settings = get_settings()

celery_app = Celery(
    "insurance_claims",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["services.worker.tasks", "services.worker.tasks_analytics"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,         # подтверждать задачу только после выполнения
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # одна задача за раз на воркер
    task_routes={
        "process_claim": {"queue": "claims"},
        "index_contract": {"queue": "contracts"},
        # Петля обучения (Шаги 29, 33) — низкоприоритетная очередь
        "calibrate_confidence": {"queue": "analytics"},
        "calibrate_confidence_all": {"queue": "analytics"},
        "update_amount_benchmarks": {"queue": "analytics"},
        "update_amount_benchmarks_all": {"queue": "analytics"},
    },
    # ── Celery Beat: периодические задачи петли обучения ──────────
    beat_schedule={
        "calibrate-confidence-daily": {
            "task": "calibrate_confidence_all",
            "schedule": crontab(hour=2, minute=30),
        },
        "update-amount-benchmarks-weekly": {
            "task": "update_amount_benchmarks_all",
            "schedule": crontab(day_of_week=0, hour=3, minute=0),  # воскресенье
        },
    },
)
