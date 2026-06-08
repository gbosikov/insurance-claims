"""
Celery Tasks — оркестрация pipeline обработки заявки.

Главная задача: process_claim
Порядок шагов:
  0. Download (скачать файлы по source_url → наш storage)
  1. Preprocessing (quality gate)
  2. OCR (параллельно для всех документов)
  3. Extraction (Claude API)
  4. Параллельно: get_contract + get_risks_and_limits + get_icd10_list
  5. RAG search (с авто-индексацией если контракт новый)
  6. Decision (Claude API + маппинг DiagnosID/risks_list)
  7. Submit (ClaimParsing_UNI — ВСЕГДА, Comment = AI-вердикт)
  8. Routing (внутренний статус, очередь, уведомления)
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from uuid import UUID

import structlog
from celery import Task
from sqlalchemy import select

from core.config import get_settings
from core.database import AsyncSessionLocal
from core.exceptions import (
    ContractNotIndexedError,
    CoreAPIUnavailableError,
    DocumentQualityError,
    FileTooLargeError,
    OCRFailedError,
    PolicyNotFoundError,
    UnsupportedFileTypeError,
)
from core.models.claim import Claim, ClaimDocument, ClaimStatus
from core.storage import get_storage_client
from layers.core_adapter.factory import get_core_adapter
from layers.core_adapter.file_helpers import documents_to_file_fields
from layers.decision.service import make_decision
from layers.extraction.service import extract_claim_data
from layers.intake.downloader import download_all_documents
from layers.ocr.service import ocr_all_documents
from layers.preprocessing.service import preprocess_all_documents
from layers.rag.searcher import build_rag_query, get_contract_chunks_with_freshness_check
from layers.routing.service import route_claim
from services.worker.celery_app import celery_app

log = structlog.get_logger()
settings = get_settings()


def run_async(coro):
    """Запускает async-корутину в синхронном контексте Celery."""
    return asyncio.run(coro)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    name="process_claim",
    queue="claims",
    soft_time_limit=540,
    time_limit=600,
)
def process_claim(self: Task, claim_id: str, tenant_id: str) -> dict:
    """
    Главная задача: последовательно запускает все слои.
    ClaimParsing_UNI вызывается ВСЕГДА (см. правило #10 CLAUDE.md).
    """
    log.info("process_claim_started", claim_id=claim_id)

    async def _run():
        async with AsyncSessionLocal() as db:
            storage = get_storage_client()
            core_adapter = get_core_adapter()

            claim_uuid = UUID(claim_id)
            tenant_uuid = UUID(tenant_id)

            result = await db.execute(
                select(Claim).where(Claim.id == claim_uuid, Claim.tenant_id == tenant_uuid)
            )
            claim = result.scalar_one_or_none()
            if claim is None:
                log.error("claim_not_found", claim_id=claim_id)
                return {"error": "claim_not_found"}

            docs_result = await db.execute(
                select(ClaimDocument).where(ClaimDocument.claim_id == claim_uuid)
            )
            documents = list(docs_result.scalars().all())

            submission_date = claim.submission_date.date() if claim.submission_date else date.today()
            policy_number = claim.policy_number  # установлен при intake

            if not policy_number:
                log.error("claim_missing_policy_number", claim_id=claim_id)
                claim.status = ClaimStatus.MANUAL_REVIEW
                claim.routing_reason = "Отсутствует номер медкарточки"
                await db.commit()
                return {"status": "manual_review", "reason": "missing_policy_number"}

            # ── Слой 0: Download (скачать файлы по source_url) ────
            from sqlalchemy import text as sa_text
            allowed_hosts_result = await db.execute(
                sa_text(
                    "SELECT value FROM platform.tenant_configs "
                    "WHERE tenant_id = :tid AND key = 'allowed_download_hosts'"
                ),
                {"tid": str(tenant_uuid)},
            )
            row = allowed_hosts_result.fetchone()
            allowed_hosts: list[str] = []
            if row:
                import json as _json
                allowed_hosts = _json.loads(row[0])

            try:
                await download_all_documents(
                    documents=documents,
                    allowed_hosts=allowed_hosts,
                    storage=storage,
                    db=db,
                    tenant_id=tenant_uuid,
                    claim_id=claim_uuid,
                )
            except (DocumentQualityError, UnsupportedFileTypeError, FileTooLargeError) as e:
                reason = getattr(e, "reason", type(e).__name__)
                detail = getattr(e, "detail", str(e))
                claim.status = ClaimStatus.DOCS_REQUESTED
                claim.routing_reason = detail
                await db.commit()
                return {"status": "docs_requested", "reason": reason}

            # ── Слой 2: Preprocessing ──────────────────────────────
            claim.status = ClaimStatus.PREPROCESSING
            await db.flush()

            try:
                await preprocess_all_documents(documents, storage, db, tenant_uuid)
            except DocumentQualityError as e:
                claim.status = ClaimStatus.DOCS_REQUESTED
                claim.routing_reason = e.detail
                await db.commit()
                return {"status": "docs_requested", "reason": e.reason}

            # ── Слой 3: OCR (параллельно) ─────────────────────────
            claim.status = ClaimStatus.OCR_PROCESSING
            await db.flush()

            try:
                ocr_results = await ocr_all_documents(documents, storage, db, tenant_uuid)
            except OCRFailedError as e:
                claim.status = ClaimStatus.MANUAL_REVIEW
                claim.routing_reason = f"OCR failed: {e}"
                await db.commit()
                return {"status": "manual_review", "reason": "ocr_failed"}

            # ── Слой 4: Extraction ────────────────────────────────
            claim.status = ClaimStatus.EXTRACTING
            await db.flush()

            extraction = await extract_claim_data(
                ocr_results, claim_uuid, tenant_uuid, submission_date, db
            )
            claim.personal_id_number = extraction.insured.personal_id

            # ── Слой 6: Три параллельных запроса к кор-системе ────
            claim.status = ClaimStatus.IDENTITY_CHECK
            await db.flush()

            async def _tracked(coro, method: str):
                """Запускает корутину, логируя имя метода при любом исключении."""
                try:
                    return await coro
                except Exception as exc:
                    log.error(
                        "core_api_method_failed",
                        claim_id=claim_id,
                        method=method,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    raise

            try:
                contract_data, risks_limits, icd10_list, providers = await asyncio.gather(
                    _tracked(core_adapter.get_contract(policy_number),          "get_contract"),
                    _tracked(core_adapter.get_risks_and_limits(policy_number),  "get_risks_and_limits"),
                    _tracked(core_adapter.get_icd10_list(),                     "get_icd10_list"),
                    _tracked(core_adapter.get_providers(),                      "get_providers"),
                )
            except PolicyNotFoundError:
                claim.status = ClaimStatus.REJECTED
                claim.routing_reason = "Полис не найден в кор-системе"
                await db.commit()
                return {"status": "rejected", "reason": "policy_not_found"}
            except CoreAPIUnavailableError:
                await db.commit()
                raise self.retry(countdown=300)

            # ── Слой 5: RAG search (с авто-индексацией) ───────────
            claim.status = ClaimStatus.RAG_SEARCH
            await db.flush()

            rag_query = build_rag_query(extraction)
            event_date = date.fromisoformat(extraction.event.date)

            try:
                contract_chunks = await get_contract_chunks_with_freshness_check(
                    db=db,
                    tenant_id=tenant_uuid,
                    policy_number=policy_number,
                    event_date=event_date,
                    query=rag_query,
                    contract_data=contract_data,  # уже загружен выше
                )
            except ContractNotIndexedError:
                log.warning("contract_not_indexed_continuing", claim_id=claim_id)
                contract_chunks = []  # Claude вернёт requires_manual_review=True

            # ── Слой 7: Decision ──────────────────────────────────
            claim.status = ClaimStatus.DECISION_PENDING
            await db.flush()

            decision = await make_decision(
                claim_id=claim_uuid,
                tenant_id=tenant_uuid,
                extraction=extraction,
                risks_limits=risks_limits,
                icd10_list=icd10_list,
                providers=providers,
                contract_chunks=contract_chunks,
                submission_date=submission_date,
                db=db,
            )

            # ── Шаг 8: ClaimParsing_UNI — ВСЕГДА ─────────────────
            # Независимо от уровня уверенности и флагов.
            # Comment содержит полный AI-вердикт для оператора кор-системы.
            claim.status = ClaimStatus.DECISION_PENDING
            await db.flush()

            try:
                file_fields = await documents_to_file_fields(documents, storage)

                core_result = await core_adapter.submit_claim(
                    policy_number=policy_number,
                    diagnosid=decision.diagnosid or 0,
                    event_start_date=extraction.event.date,
                    event_end_date=extraction.event.date,
                    pers_id=decision.pers_id or 0,
                    config_kind=decision.config_kind or 0,
                    risks_list=decision.risks_list,
                    file_fields=file_fields,
                    comment=decision.summary,
                )

                log.info(
                    "claim_submitted_to_core",
                    claim_id=claim_id,
                    innum=core_result.innum,
                    status=core_result.status,
                    status_text=core_result.status_text,
                )

                # Коды ошибок ClaimParsing_UNI (0 = успех):
                # 1=нет номера карточки, 2=нет диагноза, 3=нет партнёра,
                # 4=нет вида направления, 5=полис не существует
                if core_result.status != 0:
                    log.warning(
                        "core_submit_non_zero_status",
                        claim_id=claim_id,
                        status=core_result.status,
                        status_text=core_result.status_text,
                    )
                    # Заявка создана в кор-системе с ошибкой — ручная проверка
                    claim.status = ClaimStatus.MANUAL_REVIEW
                    claim.routing_reason = (
                        f"Кор-система: {core_result.status_text} (код {core_result.status})"
                    )
                    claim.overall_confidence = decision.overall_confidence
                    claim.total_claimed = extraction.event.total_claimed
                    claim.event_date = event_date
                    claim.processed_at = datetime.utcnow()
                    await db.commit()
                    return {
                        "status": "manual_review",
                        "reason": f"core_error_{core_result.status}",
                        "status_text": core_result.status_text,
                    }

            except CoreAPIUnavailableError:
                await db.commit()
                raise self.retry(countdown=300)

            except Exception as e:
                log.error("core_submit_failed", claim_id=claim_id, error=str(e))
                # Ошибка submit — уходим в manual_review, но не теряем решение
                claim.status = ClaimStatus.MANUAL_REVIEW
                claim.routing_reason = f"Ошибка отправки в кор-систему: {str(e)[:200]}"
                claim.overall_confidence = decision.overall_confidence
                claim.total_claimed = extraction.event.total_claimed
                claim.total_approved = decision.total_approved
                claim.final_payout = decision.final_payout
                claim.event_date = event_date
                claim.processed_at = datetime.utcnow()
                await db.commit()
                return {"status": "manual_review", "reason": "core_submit_error"}

            # ── Шаг 9: Routing (после submit) ────────────────────
            routing_result = await route_claim(claim=claim, decision=decision, db=db)

            # Сохраняем Innum из кор-системы
            claim.client_reference = claim.client_reference or core_result.innum

            claim.total_claimed = extraction.event.total_claimed
            claim.total_approved = decision.total_approved
            claim.final_payout = decision.final_payout
            claim.event_date = event_date
            claim.overall_confidence = decision.overall_confidence
            claim.processed_at = datetime.utcnow()

            await db.commit()

            log.info(
                "process_claim_completed",
                claim_id=claim_id,
                route=routing_result.route,
                innum=core_result.innum,
                final_payout=decision.final_payout,
                confidence=decision.overall_confidence,
            )

            return {
                "status":      routing_result.route,
                "innum":       core_result.innum,
                "final_payout": decision.final_payout,
                "confidence":  decision.overall_confidence,
            }

    try:
        return run_async(_run())
    except Exception as e:
        log.error("process_claim_unexpected_error", claim_id=claim_id, error=str(e))

        async def _emergency_manual():
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Claim).where(Claim.id == UUID(claim_id)))
                claim = result.scalar_one_or_none()
                if claim:
                    claim.status = ClaimStatus.MANUAL_REVIEW
                    claim.routing_reason = f"system_error: {str(e)[:200]}"
                    await db.commit()

        run_async(_emergency_manual())
        raise


@celery_app.task(name="index_contract", queue="contracts")
def index_contract_task(
    tenant_id: str,
    policy_number: str,
    pdf_storage_path: str,
    valid_from: str,
) -> dict:
    """Фоновая индексация контракта (запускается при загрузке нового PDF)."""

    async def _run():
        from layers.rag.indexer import index_contract
        from core.storage import get_storage_client

        async with AsyncSessionLocal() as db:
            storage = get_storage_client()
            pdf_bytes = await storage.download(pdf_storage_path)

            version = await index_contract(
                tenant_id=UUID(tenant_id),
                policy_number=policy_number,
                pdf_bytes=pdf_bytes,
                valid_from=date.fromisoformat(valid_from),
                storage=storage,
                db=db,
            )
            return {"version_id": version.version_id, "policy_number": policy_number}

    return run_async(_run())
