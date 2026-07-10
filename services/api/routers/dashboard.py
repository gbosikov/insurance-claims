"""
Router: /v1/dashboard — веб-портал (история заявок + стоимость).

Аутентификация: JWT Bearer (Depends(get_current_portal_user)).
Эти маршруты НЕ требуют X-API-Key — добавлены в
PUBLIC_PATH_PREFIXES в core/auth.py.

GET /v1/dashboard/claims               — список заявок с агрегированными затратами
GET /v1/dashboard/claims/{id}/cost     — детальная разбивка затрат по шагам
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from core.models.claim import Claim
from core.portal_auth import UserInToken, get_current_portal_user

log = structlog.get_logger()
router = APIRouter()

# Постоянная стоимость инфраструктуры на заявку (хранение, БД, очереди)
_INFRA_COST_USD = 0.005

# Наценка для клиента: себестоимость × 4 (OCR + AI токены)
_CLIENT_MARKUP = 4.0

# Себестоимость OCR (Google Vision — не зависит от LLM-провайдера)
_OCR_COST_PER_PAGE = 0.0015          # $0.0015/страница → клиенту $0.006

# Тарифы AI-токенов берутся по МОДЕЛИ, которая обработала заявку
# (audit_log.model_version) через settings.cost_for_model() — каждая заявка
# считается по цене своей модели, независимо от активной модели в .env.


# ── Helpers ────────────────────────────────────────────────────────

def _ai_cost_by_groups(groups: list[tuple[str | None, float, float]]) -> float:
    """Стоимость AI для клиента (×4). Каждая группа (model, in_tok, out_tok)
    считается по тарифу СВОЕЙ модели — так смешанная история (разные модели)
    считается корректно."""
    settings = get_settings()
    raw = 0.0
    for model, in_tok, out_tok in groups:
        in_rate, out_rate = settings.cost_for_model(model)
        raw += in_tok * in_rate / 1_000_000 + out_tok * out_rate / 1_000_000
    return round(raw * _CLIENT_MARKUP, 6)


def _ocr_cost(raw_usd: float) -> float:
    """Стоимость OCR для клиента (наценка ×4 от себестоимости)."""
    return round(raw_usd * _CLIENT_MARKUP, 6)


async def _fetch_cost_batch(
    db: AsyncSession,
    claim_ids: list[UUID],
    tenant_id: UUID,
) -> dict[str, dict]:
    """
    Батч-запрос затрат для списка заявок.
    OCR агрегируется по заявке (модель-независимо), AI-токены — по (заявка, модель),
    чтобы каждую заявку посчитать по цене её собственной модели.
    Возвращает: {claim_id: {ocr_cost_usd, ocr_pages, ai_groups: [(model, in, out)]}}
    """
    if not claim_ids:
        return {}

    # Строим параметры для IN-clause (UUIDs из БД — безопасные)
    params: dict = {"tenant_id": str(tenant_id)}
    placeholders: list[str] = []
    for i, cid in enumerate(claim_ids):
        key = f"cid{i}"
        params[key] = str(cid)
        placeholders.append(f":{key}")

    in_clause = ", ".join(placeholders)

    # OCR — по заявке (Vision billится по страницам, от модели не зависит)
    ocr_rows = (await db.execute(
        text(f"""
            SELECT claim_id::text AS claim_id,
                   COALESCE(SUM((output_data->>'ocr_cost_usd')::numeric), 0) AS ocr_cost_usd,
                   COALESCE(SUM((output_data->>'pages_count')::numeric), 0)  AS ocr_pages
            FROM audit_log
            WHERE tenant_id::text = :tenant_id AND step = 'ocr'
              AND claim_id::text IN ({in_clause})
            GROUP BY claim_id
        """),
        params,
    )).mappings().all()

    # AI-токены — по (заявка, модель), чтобы считать каждую по её тарифу
    ai_rows = (await db.execute(
        text(f"""
            SELECT claim_id::text AS claim_id,
                   model_version,
                   COALESCE(SUM((output_data->>'input_tokens')::numeric), 0)  AS in_tok,
                   COALESCE(SUM((output_data->>'output_tokens')::numeric), 0) AS out_tok
            FROM audit_log
            WHERE tenant_id::text = :tenant_id AND step IN ('extraction', 'decision')
              AND claim_id::text IN ({in_clause})
            GROUP BY claim_id, model_version
        """),
        params,
    )).mappings().all()

    result: dict[str, dict] = {}
    for r in ocr_rows:
        result[r["claim_id"]] = {
            "ocr_cost_usd": float(r["ocr_cost_usd"]),
            "ocr_pages": int(r["ocr_pages"]),
            "ai_groups": [],
        }
    for r in ai_rows:
        entry = result.setdefault(
            r["claim_id"], {"ocr_cost_usd": 0.0, "ocr_pages": 0, "ai_groups": []}
        )
        entry["ai_groups"].append(
            (r["model_version"], float(r["in_tok"]), float(r["out_tok"]))
        )
    return result


# ── Endpoints ──────────────────────────────────────────────────────

@router.get("/claims")
async def list_claims(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    current_user: UserInToken = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Список заявок тенанта с агрегированными затратами (OCR + AI).
    Пагинация: page / per_page.
    Фильтр: ?status=AUTO_APPROVED
    """
    tenant_id = current_user.tenant_id

    # ── Подсчёт всего ─────────────────────────────────────
    count_q = select(func.count(Claim.id)).where(Claim.tenant_id == tenant_id)
    if status_filter:
        count_q = count_q.where(Claim.status == status_filter)
    total = (await db.execute(count_q)).scalar() or 0

    # ── Заявки страницы ───────────────────────────────────
    claims_q = (
        select(Claim)
        .where(Claim.tenant_id == tenant_id)
        .order_by(Claim.submission_date.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    if status_filter:
        claims_q = claims_q.where(Claim.status == status_filter)
    claims = (await db.execute(claims_q)).scalars().all()

    # ── Затраты (батч) ────────────────────────────────────
    cost_map = await _fetch_cost_batch(db, [c.id for c in claims], tenant_id)

    # ── Формируем ответ ───────────────────────────────────
    items = []
    for claim in claims:
        cost = cost_map.get(str(claim.id), {})
        ocr_raw   = float(cost.get("ocr_cost_usd") or 0)
        ai_groups = cost.get("ai_groups") or []
        inp = sum(g[1] for g in ai_groups)   # суммарные токены (для отображения)
        out = sum(g[2] for g in ai_groups)
        ocr = _ocr_cost(ocr_raw)
        ai  = _ai_cost_by_groups(ai_groups)  # стоимость — по модели каждой группы

        ocr_pages = int(cost.get("ocr_pages") or 0)
        items.append({
            "id": str(claim.id),
            "policy_number": claim.policy_number,
            "status": claim.status.value if hasattr(claim.status, "value") else str(claim.status),
            "submission_date": claim.submission_date.isoformat() if claim.submission_date else None,
            "event_date": claim.event_date.isoformat() if claim.event_date else None,
            "total_claimed": float(claim.total_claimed) if claim.total_claimed else None,
            "total_approved": float(claim.total_approved) if claim.total_approved else None,
            "final_payout": float(claim.final_payout) if claim.final_payout else None,
            "decision_type": claim.decision_type,
            "overall_confidence": float(claim.overall_confidence) if claim.overall_confidence else None,
            "cost": {
                "ocr_pages": ocr_pages,
                "ocr_cost_usd": ocr,
                "ai_input_tokens": int(inp),
                "ai_output_tokens": int(out),
                "ai_cost_usd": ai,
                "infra_cost_usd": _INFRA_COST_USD,
                "total_cost_usd": round(ocr + ai + _INFRA_COST_USD, 6),
            },
        })

    return {
        "claims": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.get("/claims/{claim_id}/cost")
async def get_claim_cost(
    claim_id: UUID,
    current_user: UserInToken = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Детальная разбивка затрат по конкретной заявке.
    Включает все шаги аудит-лога с токенами и стоимостью OCR.
    """
    tenant_id = current_user.tenant_id

    # Проверяем что заявка принадлежит тенанту
    claim = (await db.execute(
        select(Claim).where(
            Claim.id == claim_id,
            Claim.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()

    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    # Все аудит-записи заявки, отсортированные по времени
    audit_rows = (await db.execute(
        text("""
            SELECT
                step,
                timestamp,
                duration_ms,
                model_version,
                (output_data->>'input_tokens')::numeric  AS input_tokens,
                (output_data->>'output_tokens')::numeric AS output_tokens,
                (output_data->>'ocr_cost_usd')::numeric  AS ocr_cost_usd,
                (output_data->>'pages_count')::numeric    AS pages_count,
                COALESCE(
                    (confidence->>'overall')::numeric,
                    (confidence->>'extraction')::numeric,
                    (confidence->>'avg')::numeric
                )                                         AS confidence_overall
            FROM audit_log
            WHERE claim_id::text = :claim_id
              AND tenant_id::text = :tenant_id
            ORDER BY timestamp ASC
        """),
        {"claim_id": str(claim_id), "tenant_id": str(tenant_id)},
    )).mappings().all()

    steps = []
    total_ocr_pages = 0
    total_ocr_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    ai_groups_all: list[tuple[str | None, float, float]] = []

    for row in audit_rows:
        inp      = int(row["input_tokens"] or 0)
        out      = int(row["output_tokens"] or 0)
        ocr_raw  = float(row["ocr_cost_usd"] or 0)
        ocr      = _ocr_cost(ocr_raw)
        pages    = int(row["pages_count"] or 0)

        total_input_tokens  += inp
        total_output_tokens += out
        total_ocr_cost      += ocr
        total_ocr_pages     += pages

        # Стоимость шага по МОДЕЛИ этого шага (row.model_version)
        step_ai = _ai_cost_by_groups([(row["model_version"], inp, out)]) if (inp or out) else 0.0
        if inp or out:
            ai_groups_all.append((row["model_version"], inp, out))
        step_cost = step_ai + ocr

        steps.append({
            "step": row["step"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
            "duration_ms": row["duration_ms"],
            "model_version": row["model_version"],
            "input_tokens": inp or None,
            "output_tokens": out or None,
            "ai_cost_usd": round(step_ai, 6) if (inp or out) else None,
            "ocr_pages": pages or None,
            "ocr_cost_usd": round(ocr, 6) if ocr else None,
            "step_cost_usd": round(step_cost, 6) if step_cost else None,
            "confidence": float(row["confidence_overall"]) if row["confidence_overall"] else None,
        })

    ai_cost    = _ai_cost_by_groups(ai_groups_all)
    total_cost = round(total_ocr_cost + ai_cost + _INFRA_COST_USD, 6)

    return {
        "claim_id": str(claim.id),
        "policy_number": claim.policy_number,
        "status": claim.status.value if hasattr(claim.status, "value") else str(claim.status),
        "submission_date": claim.submission_date.isoformat() if claim.submission_date else None,
        "total_claimed": float(claim.total_claimed) if claim.total_claimed else None,
        "total_approved": float(claim.total_approved) if claim.total_approved else None,
        "final_payout": float(claim.final_payout) if claim.final_payout else None,
        "cost_summary": {
            "ocr_total_pages": total_ocr_pages,
            "ocr_cost_usd": round(total_ocr_cost, 6),
            "ai_input_tokens": total_input_tokens,
            "ai_output_tokens": total_output_tokens,
            "ai_cost_usd": round(ai_cost, 6),
            "infra_cost_usd": _INFRA_COST_USD,
            "total_cost_usd": total_cost,
        },
        "audit_steps": steps,
    }


@router.get("/stats")
async def get_stats(
    current_user: UserInToken = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Агрегированная статистика по всем заявкам тенанта:
    OCR страницы, input/output токены — с разбивкой стоимости по каждому типу.
    """
    tenant_id = current_user.tenant_id

    # База: количество заявок + OCR (модель-независимо)
    base = (await db.execute(
        text("""
            SELECT
                COUNT(DISTINCT al.claim_id)                                           AS total_claims,
                COALESCE(SUM(CASE WHEN al.step = 'ocr'
                    THEN (al.output_data->>'pages_count')::numeric END), 0)           AS total_ocr_pages,
                COALESCE(SUM(CASE WHEN al.step = 'ocr'
                    THEN (al.output_data->>'ocr_cost_usd')::numeric END), 0)          AS total_ocr_cost
            FROM audit_log al
            INNER JOIN claims c ON c.id = al.claim_id AND c.tenant_id = al.tenant_id
            WHERE al.tenant_id::text = :tenant_id
        """),
        {"tenant_id": str(tenant_id)},
    )).mappings().one()

    # AI-токены — по модели, чтобы каждую группу посчитать по своему тарифу
    ai_by_model = (await db.execute(
        text("""
            SELECT al.model_version,
                   COALESCE(SUM((al.output_data->>'input_tokens')::numeric), 0)  AS in_tok,
                   COALESCE(SUM((al.output_data->>'output_tokens')::numeric), 0) AS out_tok
            FROM audit_log al
            INNER JOIN claims c ON c.id = al.claim_id AND c.tenant_id = al.tenant_id
            WHERE al.tenant_id::text = :tenant_id
              AND al.step IN ('extraction', 'decision')
            GROUP BY al.model_version
        """),
        {"tenant_id": str(tenant_id)},
    )).mappings().all()

    total_claims  = int(base["total_claims"])
    ocr_pages     = int(base["total_ocr_pages"])
    ocr_raw       = float(base["total_ocr_cost"])

    settings = get_settings()
    input_tokens = sum(int(r["in_tok"]) for r in ai_by_model)
    output_tokens = sum(int(r["out_tok"]) for r in ai_by_model)
    input_cost = output_cost = 0.0
    for r in ai_by_model:
        in_rate, out_rate = settings.cost_for_model(r["model_version"])
        input_cost  += int(r["in_tok"])  * in_rate  / 1_000_000 * _CLIENT_MARKUP
        output_cost += int(r["out_tok"]) * out_rate / 1_000_000 * _CLIENT_MARKUP

    ocr_cost    = round(_ocr_cost(ocr_raw), 4)
    input_cost  = round(input_cost, 4)
    output_cost = round(output_cost, 4)
    infra_cost  = round(total_claims  * _INFRA_COST_USD, 4)
    total_cost  = round(ocr_cost + input_cost + output_cost + infra_cost, 4)

    return {
        "total_claims": total_claims,
        "ocr": {
            "pages": ocr_pages,
            "cost_usd": ocr_cost,
        },
        "ai_input": {
            "tokens": input_tokens,
            "cost_usd": input_cost,
        },
        "ai_output": {
            "tokens": output_tokens,
            "cost_usd": output_cost,
        },
        "infra": {
            "cost_usd": infra_cost,
        },
        "total_cost_usd": total_cost,
    }
