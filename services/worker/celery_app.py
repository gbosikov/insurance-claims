"""
Celery application — конфигурация очереди задач.
"""

from celery import Celery
from core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "insurance_claims",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["services.worker.tasks"],
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
    },
)
