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
from layers.core_adapter.risk_matcher import match_risks as _build_fallback_risks
from layers.decision.service import make_decision
from layers.extraction.service import extract_claim_data
from layers.intake.downloader import download_all_documents
from layers.ocr.service import ocr_all_documents
from layers.preprocessing.service import preprocess_all_documents
from layers.rag.searcher import build_rag_query, get_contract_chunks_with_freshness_check
from layers.routing.service import route_claim
from core.audit import write_audit_entry
from services.worker.celery_app import celery_app

log = structlog.get_logger()
settings = get_settings()


def _build_structured_comment(decision, extraction) -> str:
    """
    Строит структурированный Comment для поля ClaimParsing_UNI:
    Уверенность: 85% | Диагнозы: E55.9 Дефицит... ✓, F45.9 ... ✗ |
    Сумма: 128 GEL (80% от 160) | Услуги: ... | <AI-вердикт>.
    """
    parts: list[str] = []

    # 1. Уверенность
    pct = round(decision.overall_confidence * 100)
    parts.append(f"Уверенность: {pct}%")

    # 2. Диагнозы с признаком покрытия
    dec_map = {d.icd10_code: d for d in decision.diagnoses}
    diag_labels = []
    for d in extraction.event.diagnoses:
        dec = dec_map.get(d.icd10_code)
        mark = "✓" if (dec and dec.is_covered) else "✗"
        name = (d.description or d.icd10_code)[:40]
        diag_labels.append(f"{d.icd10_code} {name} {mark}")
    if diag_labels:
        parts.append("Диагнозы: " + ", ".join(diag_labels))

    # 3. Сумма выплаты
    total = extraction.event.total_claimed or 0.0
    payout = decision.final_payout or 0.0
    if total > 0:
        cov_pct = round(payout / total * 100) if payout else 0
        parts.append(f"Сумма: {payout:.0f} GEL ({cov_pct}% от {total:.0f})")

    # 4. Услуги — только из чека, иначе все позиции
    receipt_svc = [
        li.description for li in extraction.event.line_items
        if li.doc_source and li.doc_source.startswith("receipt") and li.description
    ]
    services = receipt_svc or [li.description for li in extraction.event.line_items if li.description]
    if services:
        parts.append("Услуги: " + ", ".join(services))

    # 5. Краткое AI-описание (первые 200 символов из summary)
    if decision.summary:
        brief = decision.summary[:200].rstrip()
        if len(decision.summary) > 200:
            brief += "..."
        parts.append(brief)

    comment = " | ".join(parts)

    # Флаги в конце если есть
    flags: list[str] = []
    if decision.fraud_flags:
        flags.extend(decision.fraud_flags)
    if decision.requires_manual_review and decision.manual_review_reason:
        flags.append(decision.manual_review_reason)
    if flags:
        comment += " [" + "; ".join(flags) + "]"

    # MEDNOTE в PHEPOBJRISK кор-системы ограничена по длине
    max_len = settings.core_api_comment_max_length
    if len(comment) > max_len:
        comment = comment[: max_len - 1] + "…"

    return comment


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

            # personal_id_number доступен после extraction (шаг 4)
            personal_number = extraction.insured.personal_id

            try:
                contract_data, risks_limits, icd10_list, providers = await asyncio.gather(
                    _tracked(core_adapter.get_contract(policy_number, personal_number),         "get_contract"),
                    _tracked(core_adapter.get_risks_and_limits(policy_number, personal_number), "get_risks_and_limits"),
                    _tracked(core_adapter.get_icd10_list(),                                     "get_icd10_list"),
                    _tracked(core_adapter.get_providers(),                                      "get_providers"),
                )
            except PolicyNotFoundError:
                claim.status = ClaimStatus.REJECTED
                claim.routing_reason = "Полис не найден в кор-системе"
                await db.commit()
                return {"status": "rejected", "reason": "policy_not_found"}
            except CoreAPIUnavailableError:
                await db.commit()
                raise self.retry(countdown=300)

            # ── Верификация личного номера из документов vs getpolicylist ──
            # Сравниваем personal_id из OCR с PersonalNumber из matched Object полиса.
            # Несовпадение → предупреждение в extraction.flags, не отказ.
            policy_personal_number = risks_limits.insured_personal_number
            personal_id_verified = False
            personal_id_mismatch = False
            if personal_number and policy_personal_number:
                personal_id_verified = personal_number.strip() == policy_personal_number.strip()
                personal_id_mismatch = not personal_id_verified
            if personal_id_mismatch:
                log.warning(
                    "personal_id_mismatch_vs_policy",
                    claim_id=str(claim_uuid),
                    from_docs=personal_number,
                    from_policy=policy_personal_number,
                )
                extraction.flags.append("personal_id_mismatch")
                extraction.extraction_confidence = max(
                    0.0, extraction.extraction_confidence - 0.20
                )

            # Аудит: что получили из кор-системы
            await write_audit_entry(
                db,
                claim_id=claim_uuid,
                tenant_id=tenant_uuid,
                step="core_fetch",
                input_data={
                    "policy_number": policy_number,
                    "personal_number_from_docs": personal_number,
                    "personal_number_from_policy": policy_personal_number,
                    "personal_id_verified": personal_id_verified,
                },
                output_data={
                    "contract_has_content": bool(contract_data.content),
                    "annual_limit": risks_limits.annual_limit,
                    "remaining": risks_limits.remaining,
                    "currency": risks_limits.currency,
                    "icd10_list_count": len(icd10_list),
                    "providers_count": len(providers),
                    "risks": [
                        {
                            "risk_id": r.risk_id,
                            "name": r.name,
                            "total_limit": r.total_limit,
                            "remaining_limit": r.remaining_limit,
                            "coverage_pct": r.coverage_pct,
                            "sublimit": r.sublimit,
                        }
                        for r in risks_limits.risks
                    ],
                },
            )

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

            # Извлекаем OCR-текст формы 100 для risk_matcher
            # (определение категории: ამბულატ / სტაციონ → подбор риска)
            from core.models.claim import DocType as DocTypeModel
            form_100_ocr_text = next(
                (r.full_text for r in ocr_results if r.doc_type == DocTypeModel.FORM_100),
                "",
            )

            decision = await make_decision(
                claim_id=claim_uuid,
                tenant_id=tenant_uuid,
                policy_number=policy_number,
                extraction=extraction,
                risks_limits=risks_limits,
                icd10_list=icd10_list,
                providers=providers,
                contract_chunks=contract_chunks,
                submission_date=submission_date,
                db=db,
                form_100_ocr_text=form_100_ocr_text,
                ocr_texts=[r.full_text for r in ocr_results if r.full_text],
            )

            # ── Шаг 8: ClaimParsing_UNI — ВСЕГДА ─────────────────
            # Независимо от уровня уверенности и флагов.
            # Comment содержит полный AI-вердикт для оператора кор-системы.
            claim.status = ClaimStatus.DECISION_PENDING
            await db.flush()

            try:
                file_fields = await documents_to_file_fields(documents, storage)

                _comment = _build_structured_comment(decision, extraction)
                # Fallback для полей ClaimParsing_UNI когда decision вернул early-exit
                # (waiting_period_violation, policy_inactive и др. — без Claude).
                _config_kind = decision.config_kind or 2  # 2 = акт возмещения (дефолт)
                _risks_list = decision.risks_list
                if not _risks_list and risks_limits and risks_limits.risks:
                    # Предпочитаем строки из чеков — форма 100 содержит анамнез/медикаменты
                    # с большими суммами которые не должны попасть в кор-систему.
                    # Если чеков нет — используем все позиции (match_risks сам fallback-ит на total_claimed).
                    _fallback_receipt_items = [
                        li for li in extraction.event.line_items
                        if li.doc_source and li.doc_source.startswith("receipt")
                    ]
                    _fallback_items = _fallback_receipt_items if _fallback_receipt_items else extraction.event.line_items
                    _risks_list, _ = _build_fallback_risks(
                        line_items=_fallback_items,
                        risks=risks_limits.risks,
                        event_date=extraction.event.date or "",
                        form_100_text=form_100_ocr_text or "",
                        config_kind=_config_kind,
                        total_claimed=extraction.event.total_claimed or 0.0,
                    )
                    log.info(
                        "risks_list_fallback_built",
                        claim_id=claim_id,
                        risks_count=len(_risks_list),
                        reason=decision.manual_review_reason or "early_exit",
                    )
                core_result = await core_adapter.submit_claim(
                    policy_number=policy_number,
                    diagnosid=decision.diagnosid or settings.core_api_diagnosid_fallback,
                    event_start_date=extraction.event.date,
                    event_end_date=extraction.event.date,
                    pers_id=decision.pers_id or settings.core_api_pers_id_fallback,
                    config_kind=_config_kind,
                    risks_list=_risks_list,
                    file_fields=file_fields,
                    comment=_comment,
                )

                log.info(
                    "claim_submitted_to_core",
                    claim_id=claim_id,
                    innum=core_result.innum,
                    status=core_result.status,
                    status_text=core_result.status_text,
                )

                # Аудит: что отправили в ClaimParsing_UNI и что вернулось
                await write_audit_entry(
                    db,
                    claim_id=claim_uuid,
                    tenant_id=tenant_uuid,
                    step="core_submit",
                    input_data={
                        "PolicyNumber":   policy_number,
                        "DiagnosID":      decision.diagnosid or settings.core_api_diagnosid_fallback,
                        "EventStartDate": extraction.event.date,
                        "EventEndDate":   extraction.event.date,
                        "PersID":         decision.pers_id or settings.core_api_pers_id_fallback,
                        "ConfigKind":     _config_kind,
                        "Comment":        _comment[:500],
                        "RisksList":      _risks_list,
                        "files_count":    len(file_fields),
                    },
                    output_data={
                        "innum":       core_result.innum,
                        "status":      core_result.status,
                        "status_text": core_result.status_text,
                    },
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


@celery_app.task(name="reindex_contract_structures", queue="contracts")
def reindex_contract_structures_task(
    tenant_id: str,
    policy_number: str,
    version_id: str,
    pdf_storage_path: str | None = None,
) -> dict:
    """
    Переиндексировать CARVEOUT и POSITIVE LIST для существующей версии контракта.

    Args:
        tenant_id: Tenant ID
        policy_number: Policy number
        version_id: Contract version ("latest" or specific version like "v20240609")
        pdf_storage_path: Optional path to contract file in storage
                          If None, will look up from ContractVersion in database
    """

    async def _run():
        from layers.rag.indexer import reindex_contract_structures
        from core.storage import get_storage_client
        from sqlalchemy import select
        from core.models.contract import ContractVersion

        async with AsyncSessionLocal() as db:
            storage = get_storage_client()

            # Если pdf_storage_path не передан, получить из БД
            if not pdf_storage_path:
                query = select(ContractVersion).where(
                    ContractVersion.tenant_id == UUID(tenant_id),
                    ContractVersion.policy_number == policy_number,
                )

                if version_id != "latest":
                    query = query.where(ContractVersion.version_id == version_id)

                result = await db.execute(
                    query.order_by(ContractVersion.created_at.desc()).limit(1)
                )
                contract_version = result.scalar_one_or_none()

                if not contract_version:
                    raise ValueError(
                        f"ContractVersion not found for {policy_number} "
                        f"(version_id={version_id})"
                    )

                pdf_storage_path = contract_version.pdf_path
                version_id = contract_version.version_id

                log.info(
                    "reindex_contract_version_resolved",
                    policy_number=policy_number,
                    version_id=version_id,
                    pdf_path=pdf_storage_path,
                )

            # Скачать и извлечь текст из контракта
            contract_text = None
            if pdf_storage_path.lower().endswith(".pdf"):
                try:
                    pdf_bytes = await storage.download(pdf_storage_path)
                    from layers.rag.indexer import extract_text_from_pdf
                    contract_text = extract_text_from_pdf(pdf_bytes)
                except Exception as e:
                    log.error(
                        "reindex_pdf_extract_error",
                        policy_number=policy_number,
                        version_id=version_id,
                        pdf_path=pdf_storage_path,
                        error=str(e),
                    )
                    raise
            elif pdf_storage_path.lower().endswith(".txt"):
                # Текстовый файл (из index_contract_from_text)
                contract_text = (await storage.download(pdf_storage_path)).decode("utf-8")

            if not contract_text:
                raise ValueError(
                    f"Could not extract contract text from {pdf_storage_path}"
                )

            log.info(
                "reindex_contract_text_extracted",
                policy_number=policy_number,
                version_id=version_id,
                text_length=len(contract_text),
            )

            # Выполнить переиндексирование
            result = await reindex_contract_structures(
                tenant_id=UUID(tenant_id),
                policy_number=policy_number,
                version_id=version_id,
                contract_text=contract_text,
                db=db,
                storage=storage,
            )

            log.info(
                "reindex_contract_complete",
                policy_number=policy_number,
                version_id=version_id,
                carveout_chunks=result["carveout_chunks_new"],
                positive_list=result["positive_list_new"],
            )

            return {
                "policy_number": policy_number,
                "version_id": version_id,
                **result,
            }

    return run_async(_run())
