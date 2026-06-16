"""
Обогащение диагнозов иерархическим контекстом из локального справочника МКБ-10.

Для каждого ICD10-кода (например J06.9) возвращает цепочку родителей:
  J06.9 → J06 → J00-J06 (блок) → J (глава)

Это позволяет Claude понять, что "J06.9" принадлежит категории
"Болезни органов дыхания / Острые респираторные инфекции",
даже если в договоре написано именно это, а не конкретный код.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models.icd10 import ICD10Diagnosis

_ICD10_STD_PART_RE = re.compile(r'^([A-Z]\d{2,3}(?:\.[0-9]{1,2})?)')


def _normalize_icd10_code(code: str) -> str:
    """E55D → E55, I10-I15 → I10, E78.2A → E78.2"""
    code = code.strip().upper()
    code = code.split("-")[0]
    m = _ICD10_STD_PART_RE.match(code)
    return m.group(1) if m else code

log = structlog.get_logger()


@dataclass
class EnrichedDiagnosis:
    code: str                          # J06.9
    name_r: str | None                 # Острая инфекция верхних дыхательных путей
    name_g: str | None                 # грузинское название
    name_e: str | None                 # английское название
    ancestors: list[AncestorNode] = field(default_factory=list)
    # ancestors упорядочены от ближайшего к корню: [J06, ..., глава J]

    @property
    def category_chain_ru(self) -> str:
        """Цепочка категорий на русском для промпта Claude."""
        parts = [self.name_r or self.code]
        for a in self.ancestors:
            if a.name_r:
                parts.append(a.name_r)
        return " → ".join(parts)

    @property
    def search_terms(self) -> list[str]:
        """Все названия (RU + EN + GE) + код для RAG-запроса."""
        terms = [self.code]
        for name in (self.name_r, self.name_e, self.name_g):
            if name:
                terms.append(name)
        for a in self.ancestors:
            for name in (a.name_r, a.name_e, a.name_g):
                if name:
                    terms.append(name)
        return terms


@dataclass
class AncestorNode:
    id: int
    extcod: str | None
    name_r: str | None
    name_g: str | None
    name_e: str | None


async def enrich_diagnosis(
    icd10_code: str,
    db: AsyncSession,
) -> EnrichedDiagnosis:
    """
    Найти запись в локальном справочнике и получить всех предков через CTE.
    Если код не найден — вернуть минимальный объект только с кодом.
    """
    # Рекурсивный обход вверх по дереву через PID
    cte_sql = text("""
        WITH RECURSIVE ancestors AS (
            -- Стартовый узел: ищем по коду МКБ-10
            SELECT id, pid, extcod, name_r, name_g, name_e, 0 AS depth
            FROM icd10_diagnoses
            WHERE extcod = :code AND is_available = TRUE

            UNION ALL

            -- Рекурсивный шаг: поднимаемся к родителю
            SELECT p.id, p.pid, p.extcod, p.name_r, p.name_g, p.name_e, a.depth + 1
            FROM icd10_diagnoses p
            JOIN ancestors a ON a.pid = p.id
            WHERE a.depth < 10   -- защита от зацикливания
        )
        SELECT id, pid, extcod, name_r, name_g, name_e, depth
        FROM ancestors
        ORDER BY depth ASC
    """)

    result = await db.execute(cte_sql, {"code": icd10_code})
    rows = result.fetchall()

    if not rows:
        # Попробовать нормализованный код: E55D → E55, I10-I15 → I10
        normalized = _normalize_icd10_code(icd10_code)
        if normalized != icd10_code.upper().strip():
            result2 = await db.execute(cte_sql, {"code": normalized})
            rows = result2.fetchall()
            if rows:
                log.info("icd10_code_normalized_for_enrichment", original=icd10_code, normalized=normalized)
            else:
                # Prefix-поиск: E55 → первый E55.x
                # ORDER BY + LIMIT нельзя в anchor RECURSIVE CTE — оборачиваем в подзапрос
                prefix_sql = text("""
                    WITH RECURSIVE ancestors AS (
                        SELECT id, pid, extcod, name_r, name_g, name_e, 0 AS depth
                        FROM (
                            SELECT id, pid, extcod, name_r, name_g, name_e
                            FROM icd10_diagnoses
                            WHERE extcod LIKE :prefix AND is_available = TRUE
                            ORDER BY extcod
                            LIMIT 1
                        ) AS base
                        UNION ALL
                        SELECT p.id, p.pid, p.extcod, p.name_r, p.name_g, p.name_e, a.depth + 1
                        FROM icd10_diagnoses p
                        JOIN ancestors a ON a.pid = p.id
                        WHERE a.depth < 10
                    )
                    SELECT id, pid, extcod, name_r, name_g, name_e, depth FROM ancestors ORDER BY depth ASC
                """)
                result3 = await db.execute(prefix_sql, {"prefix": normalized[:3] + "%"})
                rows = result3.fetchall()
                if rows:
                    log.info("icd10_code_prefix_match", original=icd10_code, prefix=normalized[:3])
        if not rows:
            log.warning("icd10_code_not_found_in_local_db", code=icd10_code)
            return EnrichedDiagnosis(code=icd10_code, name_r=None, name_g=None, name_e=None)

    # Первая строка — сам диагноз (depth=0), остальные — предки
    base = rows[0]
    ancestors = [
        AncestorNode(
            id=r.id,
            extcod=r.extcod,
            name_r=r.name_r,
            name_g=r.name_g,
            name_e=r.name_e,
        )
        for r in rows[1:]
    ]

    return EnrichedDiagnosis(
        code=icd10_code,
        name_r=base.name_r,
        name_g=base.name_g,
        name_e=base.name_e,
        ancestors=ancestors,
    )


async def enrich_all(
    icd10_codes: list[str],
    db: AsyncSession,
) -> dict[str, EnrichedDiagnosis]:
    """Обогатить список кодов. Возвращает словарь code → EnrichedDiagnosis."""
    results: dict[str, EnrichedDiagnosis] = {}
    for code in icd10_codes:
        results[code] = await enrich_diagnosis(code, db)
    return results


async def find_diagnosid(
    icd10_code: str,
    db: AsyncSession,
) -> tuple[int | None, float]:
    """
    Найти внутренний ID диагноза в локальном справочнике.

    Возвращает (id, confidence):
      - Точное совпадение → confidence=1.0
      - Один prefix-match (J06.x) → confidence=0.8
      - Несколько prefix-matches → (None, 0.0) — неоднозначно, лучше manual_review
      - Не найден → (None, 0.0)
    """
    # Точное совпадение
    result = await db.execute(
        select(ICD10Diagnosis.id)
        .where(ICD10Diagnosis.extcod == icd10_code, ICD10Diagnosis.is_available.is_(True))
    )
    exact = result.scalar_one_or_none()
    if exact is not None:
        return exact, 1.0

    # Prefix-match: J06 найдёт J06.0, J06.1, J06.9, etc.
    prefix = icd10_code[:3] if len(icd10_code) >= 3 else icd10_code
    result = await db.execute(
        select(ICD10Diagnosis.id, ICD10Diagnosis.extcod)
        .where(
            ICD10Diagnosis.extcod.like(f"{prefix}%"),
            ICD10Diagnosis.is_available.is_(True),
        )
    )
    matches = result.fetchall()

    if len(matches) == 1:
        log.info("icd10_prefix_match", code=icd10_code, matched=matches[0].extcod)
        return matches[0].id, 0.8

    if len(matches) > 1:
        log.warning(
            "icd10_ambiguous_prefix",
            code=icd10_code,
            matches=[m.extcod for m in matches[:5]],
        )
        return None, 0.0

    log.warning("icd10_not_found", code=icd10_code)
    return None, 0.0


async def search_by_name(
    query: str,
    db: AsyncSession,
    lang: str = "r",
    limit: int = 10,
) -> list[ICD10Diagnosis]:
    """
    Полнотекстовый поиск диагнозов по названию (для нечёткого поиска).
    lang: 'r' = русский (russian), 'g' = грузинский (simple), 'e' = английский (english)
    """
    ts_config = {"r": "russian", "g": "simple", "e": "english"}.get(lang, "russian")
    name_col = {"r": "name_r", "g": "name_g", "e": "name_e"}.get(lang, "name_r")

    fts_sql = text(f"""
        SELECT id, pid, extcod, name_r, name_g, name_e
        FROM icd10_diagnoses
        WHERE is_available = TRUE
          AND extcod IS NOT NULL
          AND to_tsvector('{ts_config}', COALESCE({name_col}, ''))
              @@ plainto_tsquery('{ts_config}', :query)
        ORDER BY ts_rank(
            to_tsvector('{ts_config}', COALESCE({name_col}, '')),
            plainto_tsquery('{ts_config}', :query)
        ) DESC
        LIMIT :limit
    """)

    result = await db.execute(fts_sql, {"query": query, "limit": limit})
    rows = result.fetchall()

    return [
        ICD10Diagnosis(
            id=r.id, pid=r.pid, extcod=r.extcod,
            name_r=r.name_r, name_g=r.name_g, name_e=r.name_e,
            is_available=True,
        )
        for r in rows
    ]
