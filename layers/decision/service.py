"""
Слой 7 — Decision Engine.

Два уровня принятия решений:
  Уровень 1: Детерминированные проверки (без AI)
  Уровень 2: Claude API — интерпретация договора

После решения:
  - Маппинг ICD10-кода → DiagnosID (из справочника кор-системы)
  - Построение risks_list для ClaimParsing_UNI
  - summary идёт в поле Comment ClaimParsing_UNI

ПРАВИЛО: при сомнении — ручная проверка, не отказ.
ClaimParsing_UNI вызывается ВСЕГДА (в tasks.py), не здесь.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import UUID

import anthropic
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import AuditTimer, write_audit_entry
from core.config import get_settings
from core.schemas.claim import ExtractionResult
from core.schemas.contract import ContractChunkSchema
from core.schemas.core_api import ICD10Item, RisksAndLimits
from core.schemas.decision import ClaimDecision, DiagnosisDecisionSchema, LineItemDecisionSchema

log = structlog.get_logger()
settings = get_settings()

PROMPT_VERSION = "decision/v2.0.0"

# ── Decision Tool ─────────────────────────────────────────────────

DECISION_TOOL: dict[str, Any] = {
    "name": "make_claim_decision",
    "description": "Принять решение по страховой заявке на основе данных и текста договора",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnoses": {
                "type": "array",
                "description": "Решения по каждому диагнозу",
                "items": {
                    "type": "object",
                    "properties": {
                        "icd10_code":         {"type": "string"},
                        "is_covered":         {"type": "boolean"},
                        "approved_amount":    {"type": "number"},
                        "rejection_reason":   {"type": ["string", "null"]},
                        "contract_reference": {"type": "string", "description": "Например: Статья 4.2, пункт 3"},
                        "confidence":         {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["icd10_code", "is_covered", "approved_amount", "confidence"]
                }
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description":     {"type": "string"},
                        "claimed_amount":  {"type": "number"},
                        "approved_amount": {"type": "number"},
                        "linked_icd10":    {"type": ["string", "null"]},
                    },
                    "required": ["description", "claimed_amount", "approved_amount"]
                }
            },
            "total_approved":           {"type": "number"},
            "deductible_applied":       {"type": "number"},
            "final_payout":             {"type": "number"},
            "requires_manual_review":   {"type": "boolean"},
            "manual_review_reason":     {"type": ["string", "null"]},
            "overall_confidence":       {"type": "number", "minimum": 0, "maximum": 1},
            "summary":                  {
                "type": "string",
                "description": (
                    "Полный вердикт на русском: решение + обоснование + уровень уверенности + флаги. "
                    "Этот текст попадёт в поле Comment в кор-системе и будет читать оператор."
                )
            },
        },
        "required": [
            "diagnoses", "total_approved", "deductible_applied",
            "final_payout", "requires_manual_review", "overall_confidence", "summary"
        ]
    }
}

DECISION_SYSTEM_PROMPT = """Ты — эксперт по страховым выплатам ДМС. Принимай точные, обоснованные решения.

ВАЖНЫЕ ПРАВИЛА:
1. Если в тексте договора нет ЯВНОГО указания что случай покрывается — верни requires_manual_review=true
2. НЕ принимай решение об ОТКАЗЕ без явного пункта договора об исключении
3. При любом сомнении — ручная проверка, не отказ
4. Цитируй КОНКРЕТНЫЙ пункт договора для каждого решения (contract_reference)
5. Рассчитывай итоговую сумму с учётом процента покрытия и остатка лимита из risks_data
6. summary должен содержать: решение + обоснование + уверенность + флаги (читает оператор)
7. Отвечай СТРОГО в формате JSON-инструмента — никакого свободного текста"""


# ── Уровень 1: Детерминированные проверки ─────────────────────────

def check_remaining_limit(risks_limits: RisksAndLimits) -> None:
    """Общий остаток должен быть > 0."""
    if risks_limits.remaining <= 0:
        from core.exceptions import PolicyLimitExhaustedError
        raise PolicyLimitExhaustedError(
            risks_limits.policy_number,
            risks_limits.remaining,
            risks_limits.currency,
        )


def check_claim_filed_in_time(submission_date: date, event_date: date, max_days: int = 90) -> bool:
    """Заявка должна быть подана не позже N дней после события."""
    delta = (submission_date - event_date).days
    return 0 <= delta <= max_days


# ── Промпт Decision Engine ─────────────────────────────────────────

def build_decision_prompt(
    extraction: ExtractionResult,
    risks_limits: RisksAndLimits,
    chunks: list[ContractChunkSchema],
) -> str:
    """Собирает промпт: данные заявки + риски/лимиты + чанки договора."""

    claim_data = {
        "insured": {
            "full_name":    extraction.insured.full_name,
            "birth_date":   extraction.insured.birth_date,
            "personal_id":  extraction.insured.personal_id,
            "policy_number": extraction.insured.policy_number,
        },
        "event": {
            "date":        extraction.event.date,
            "institution": extraction.event.institution,
            "diagnoses":   [{"icd10_code": d.icd10_code, "description": d.description} for d in extraction.event.diagnoses],
            "line_items":  [{"description": li.description, "amount": li.amount} for li in extraction.event.line_items],
            "total_claimed": extraction.event.total_claimed,
        },
        "extraction_confidence": extraction.extraction_confidence,
        "flags": extraction.flags,
    }

    risks_data = {
        "annual_limit": risks_limits.annual_limit,
        "remaining":    risks_limits.remaining,
        "currency":     risks_limits.currency,
        "risks": [
            {
                "risk_id":       r.risk_id,
                "name":          r.name,
                "coverage_pct":  r.coverage_pct,
                "remaining":     r.remaining_limit,
            }
            for r in risks_limits.risks
        ],
    }

    chunks_text = "\n\n".join(
        f"[{chunk.section_type or 'general'}] {chunk.title or ''}\n{chunk.content}"
        for chunk in chunks
    )

    return f"""## Данные заявки
{json.dumps(claim_data, ensure_ascii=False, indent=2)}

## Риски и лимиты (актуальные данные из кор-системы)
{json.dumps(risks_data, ensure_ascii=False, indent=2)}

## Релевантные пункты договора
{chunks_text if chunks_text else "Релевантные пункты не найдены — верни requires_manual_review=true"}"""


# ── Маппинг на справочники кор-системы ───────────────────────────

def find_diagnosid(icd10_code: str, icd10_list: list[ICD10Item]) -> int | None:
    """Найти DiagnosID по коду ICD10 (точное совпадение, без учёта регистра)."""
    code_upper = icd10_code.upper().strip()
    for item in icd10_list:
        if item.code.upper().strip() == code_upper:
            return item.diagnosid
    # Fallback: совпадение по префиксу (J06 совпадёт с J06.9)
    prefix = code_upper.split(".")[0]
    for item in icd10_list:
        if item.code.upper().startswith(prefix):
            return item.diagnosid
    return None


def build_risks_list(
    decision_line_items: list[LineItemDecisionSchema],
    risks_limits: RisksAndLimits,
    event_date: str,
) -> tuple[list[dict], int | None]:
    """
    Построить risks_list для ClaimParsing_UNI из одобренных позиций.
    Возвращает: (risks_list, config_kind).
    """
    risks_list = []
    config_kind: int | None = None

    # Берём первый риск с ненулевым остатком как основной
    primary_risk = next(
        (r for r in risks_limits.risks if r.remaining_limit > 0),
        risks_limits.risks[0] if risks_limits.risks else None,
    )

    if primary_risk:
        # config_kind из первого сервиса риска
        if primary_risk.services:
            config_kind = primary_risk.services[0].get("config_kind") or primary_risk.services[0].get("ConfigKind")

        for li in decision_line_items:
            if li.approved_amount <= 0:
                continue
            # serviceid из справочника услуг риска или fallback
            serviceid = ""
            serv_name = li.description
            if primary_risk.services:
                svc = primary_risk.services[0]
                serviceid = svc.get("serviceid", svc.get("ServiceID", ""))
                if not serv_name:
                    serv_name = svc.get("name", svc.get("ServName", ""))

            risks_list.append({
                "RiskID":      primary_risk.risk_id,
                "FinalAmount": li.approved_amount,
                "ServDate":    event_date,
                "serviceid":   serviceid,
                "ServName":    serv_name,
            })

    return risks_list, config_kind


# ── Антифрод ──────────────────────────────────────────────────────

async def check_fraud(
    db: AsyncSession,
    tenant_id: UUID,
    personal_id: str,
    event_date: date,
    institution: str | None,
    total_amount: float,
) -> list[str]:
    """Детерминированные антифрод-проверки."""
    from sqlalchemy import func, select
    from core.models.claim import Claim, ClaimStatus

    fraud_flags: list[str] = []

    # 1. Дубль: тот же personal_id + event_date
    dup = await db.execute(
        select(Claim).where(
            Claim.tenant_id == tenant_id,
            Claim.personal_id_number == personal_id,
            Claim.event_date == event_date,
            Claim.status.notin_([ClaimStatus.REJECTED, ClaimStatus.RECEIVED]),
        )
    )
    if dup.scalars().first():
        fraud_flags.append("duplicate_claim")

    # 2. Частота: > MAX за N дней
    from datetime import timedelta
    window_start = event_date - timedelta(days=settings.fraud_frequency_window_days)
    cnt = await db.execute(
        select(func.count(Claim.id)).where(
            Claim.tenant_id == tenant_id,
            Claim.personal_id_number == personal_id,
            Claim.event_date >= window_start,
            Claim.event_date <= event_date,
        )
    )
    if (cnt.scalar() or 0) > settings.fraud_frequency_max_claims:
        fraud_flags.append("frequency_anomaly")

    # 3. Аномальная сумма (TODO: после накопления статистики)

    return fraud_flags


# ── Основная функция ──────────────────────────────────────────────

async def make_decision(
    *,
    claim_id: UUID,
    tenant_id: UUID,
    extraction: ExtractionResult,
    risks_limits: RisksAndLimits,
    icd10_list: list[ICD10Item],
    contract_chunks: list[ContractChunkSchema],
    submission_date: date,
    db: AsyncSession,
) -> ClaimDecision:
    """
    Принимает решение по заявке.
    Уровень 1 → Уровень 2 (Claude) → Антифрод → Маппинг на кор-систему → Аудит
    """
    with AuditTimer() as timer:

        # ── Уровень 1: Детерминированные проверки ─────────────────
        try:
            event_date = date.fromisoformat(extraction.event.date)
            check_remaining_limit(risks_limits)

            if not check_claim_filed_in_time(submission_date, event_date):
                summary = "Заявка подана позже допустимого срока (90 дней) с даты страхового события."
                return ClaimDecision(
                    claim_id=claim_id,
                    diagnoses=[],
                    total_approved=0.0,
                    deductible_applied=0.0,
                    final_payout=0.0,
                    status="rejected",
                    requires_manual_review=False,
                    fraud_flags=[],
                    overall_confidence=1.0,
                    summary=summary,
                    prompt_version=PROMPT_VERSION,
                    model_version=settings.claude_model,
                )
        except Exception as e:
            summary = str(e)
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="rejected",
                requires_manual_review=False,
                fraud_flags=[],
                overall_confidence=1.0,
                summary=summary,
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        # ── Антифрод (параллельно с Уровнем 2) ───────────────────
        import asyncio
        personal_id = extraction.insured.personal_id
        fraud_task = check_fraud(
            db, tenant_id, personal_id,
            event_date, extraction.event.institution,
            extraction.event.total_claimed,
        )

        # ── Уровень 2: Claude API ─────────────────────────────────
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        user_prompt = build_decision_prompt(extraction, risks_limits, contract_chunks)

        try:
            response = client.messages.create(
                model=settings.claude_model,
                max_tokens=settings.claude_decision_max_tokens,
                temperature=settings.claude_decision_temperature,
                system=DECISION_SYSTEM_PROMPT,
                tools=[DECISION_TOOL],
                tool_choice={"type": "tool", "name": "make_claim_decision"},
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.APIError as e:
            log.error("decision_claude_error", claim_id=str(claim_id), error=str(e))
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason=f"Claude API error: {e}",
                fraud_flags=[],
                overall_confidence=0.0,
                summary=f"Ошибка AI-анализа: {e}. Требуется ручная проверка оператором.",
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        tool_block = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_block is None:
            return ClaimDecision(
                claim_id=claim_id,
                diagnoses=[],
                total_approved=0.0,
                deductible_applied=0.0,
                final_payout=0.0,
                status="manual_review",
                requires_manual_review=True,
                manual_review_reason="Claude did not return tool_use block",
                fraud_flags=[],
                overall_confidence=0.0,
                summary="AI не вернул структурированный ответ. Требуется ручная проверка.",
                prompt_version=PROMPT_VERSION,
                model_version=settings.claude_model,
            )

        raw: dict[str, Any] = tool_block.input
        fraud_flags = await fraud_task

        diagnoses = [DiagnosisDecisionSchema(**d) for d in raw.get("diagnoses", [])]
        line_items = [LineItemDecisionSchema(**li) for li in raw.get("line_items", [])]

        all_covered = all(d.is_covered for d in diagnoses) if diagnoses else False
        any_covered = any(d.is_covered for d in diagnoses) if diagnoses else False

        if raw.get("requires_manual_review") or fraud_flags:
            status = "manual_review"
        elif all_covered:
            status = "approved"
        elif any_covered:
            status = "partial"
        else:
            status = "rejected"

        # ── Маппинг на справочники кор-системы ───────────────────
        # DiagnosID: берём из первого диагноза с покрытием
        diagnosid: int | None = None
        for diag in diagnoses:
            found = find_diagnosid(diag.icd10_code, icd10_list)
            if found is not None:
                diagnosid = found
                break
        # Если ни один не нашли — берём первый диагноз
        if diagnosid is None and extraction.event.diagnoses:
            diagnosid = find_diagnosid(extraction.event.diagnoses[0].icd10_code, icd10_list)

        # risks_list и config_kind из одобренных позиций
        risks_list, config_kind = build_risks_list(line_items, risks_limits, extraction.event.date)

        # PersID: TODO — нужен справочник провайдеров из кор-системы
        # Пока ставим 0; уточнить у владельца название метода lookup
        pers_id = 0

        decision = ClaimDecision(
            claim_id=claim_id,
            diagnoses=diagnoses,
            line_items=line_items,
            total_approved=raw.get("total_approved", 0.0),
            deductible_applied=raw.get("deductible_applied", 0.0),
            final_payout=raw.get("final_payout", 0.0),
            status=status,
            requires_manual_review=raw.get("requires_manual_review", False) or bool(fraud_flags),
            manual_review_reason=raw.get("manual_review_reason"),
            fraud_flags=fraud_flags,
            overall_confidence=raw.get("overall_confidence", 0.0),
            summary=raw.get("summary", ""),
            rag_chunks_used=[str(chunk.id) for chunk in contract_chunks],
            prompt_version=PROMPT_VERSION,
            model_version=settings.claude_model,
            # Поля для ClaimParsing_UNI
            diagnosid=diagnosid,
            pers_id=pers_id,
            config_kind=config_kind,
            risks_list=risks_list,
        )

    await write_audit_entry(
        db,
        claim_id=claim_id,
        tenant_id=tenant_id,
        step="decision",
        input_data={
            "diagnoses_count": len(extraction.event.diagnoses),
            "total_claimed":   extraction.event.total_claimed,
            "rag_chunks_count": len(contract_chunks),
            "risks_count":     len(risks_limits.risks),
        },
        output_data={
            "status":          decision.status,
            "final_payout":    decision.final_payout,
            "requires_manual_review": decision.requires_manual_review,
            "fraud_flags":     decision.fraud_flags,
            "diagnosid":       decision.diagnosid,
            "pers_id":         decision.pers_id,
            "config_kind":     decision.config_kind,
            "summary":         decision.summary[:300],
        },
        confidence={"overall": decision.overall_confidence},
        rag_chunks=[str(c.id) for c in contract_chunks],
        prompt_version=PROMPT_VERSION,
        model_version=settings.claude_model,
        duration_ms=timer.duration_ms,
    )

    log.info(
        "decision_made",
        claim_id=str(claim_id),
        status=decision.status,
        final_payout=decision.final_payout,
        diagnosid=decision.diagnosid,
        confidence=decision.overall_confidence,
        fraud_flags=decision.fraud_flags,
    )

    return decision
