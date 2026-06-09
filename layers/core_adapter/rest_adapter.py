"""
Слой 6 — Реализация Core System Adapter для Lite GROUP.

Два API:
1. LiteMed API (данные полисов):
     Auth:  POST {core_api_auth_url}/api/User/authenticate → {"token": "..."}
     Data:  POST {core_api_base_url}/api/Client/getpolicylist
            Body: {"personalnumber": "...", "STATE": "0", "schedule": "0"}
            Header: Authorization: Bearer <token>

2. Claims API (создание убытка):
     POST {core_api_claims_base_url}/LiteApi/LiteServiceJSON
     Body: {"METHODNAME": "ClaimParsing_UNI", "XML_DATA": {...}}
     Header: Authorization: Bearer <token>
     URL уточнить у владельца кор-системы (core_api_claims_base_url в .env).

Ответ getpolicylist: {"PolicyList": "..."} — строка (пустая или JSON-массив).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date

import httpx
import structlog

from core.config import get_settings
from core.exceptions import CoreAPIUnavailableError, PolicyNotFoundError
from core.schemas.core_api import (
    ContractData,
    ICD10Item,
    ProviderInfo,
    RiskInfo,
    RisksAndLimits,
    SubmitClaimResult,
)
from layers.core_adapter.interface import CoreSystemAdapter

log = structlog.get_logger()
settings = get_settings()

RETRY_BACKOFF = [1, 3, 10]
REDIS_TOKEN_KEY = "lite_group:jwt_token"
REDIS_ICD10_KEY = "lite_group:icd10_list"
REDIS_PROVIDERS_KEY = "lite_group:providers"
TOKEN_TTL = 3600        # 1 час
ICD10_CACHE_TTL = 86400 # 24 часа



class LiteGroupAdapter(CoreSystemAdapter):
    """
    Реализация для кор-системы Lite GROUP.

    Auth-токен кэшируется в Redis (TTL=1ч) с in-memory fallback.
    При 401 — однократное обновление токена и повтор запроса.
    """

    def __init__(self) -> None:
        base = settings.core_api_base_url.rstrip("/")
        self._data_base = base

        # Auth-сервер: отдельный (прод) или тот же (дев/тест)
        auth_base = settings.core_api_auth_url.rstrip("/") if settings.core_api_auth_url else base
        self._auth_url = f"{auth_base}/api/User/authenticate"

        # Сервер для ClaimParsing_UNI (может быть другим)
        claims_base = settings.core_api_claims_base_url.rstrip("/") if settings.core_api_claims_base_url else base
        self._claims_url = f"{claims_base}/LiteApi/LiteServiceJSON"

        self._timeout = settings.core_api_timeout
        self._max_retries = settings.core_api_retry

        # In-memory fallback для токена (если Redis недоступен)
        self._token_cache: str | None = None
        self._token_ts: float = 0.0
        self._redis = None  # lazy init

    # ── Управление токеном ─────────────────────────────────────────

    async def _get_redis(self):
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(
                    settings.redis_url, decode_responses=True, socket_timeout=2
                )
            except Exception:
                pass
        return self._redis

    async def _get_token(self) -> str:
        redis = await self._get_redis()
        if redis:
            try:
                cached = await redis.get(REDIS_TOKEN_KEY)
                if cached:
                    return cached
            except Exception:
                pass

        if self._token_cache and (time.time() - self._token_ts) < TOKEN_TTL:
            return self._token_cache

        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """POST /api/User/authenticate → Bearer-токен."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._auth_url,
                json={
                    "userName": settings.core_api_username,
                    "passWord": settings.core_api_password,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # Поле "token" (нижний регистр — подтверждено тестовым сервером)
            token = data.get("token") or data.get("TOKEN") or data.get("Token", "")

        if not token:
            raise CoreAPIUnavailableError(message="Empty token from /api/User/authenticate")

        redis = await self._get_redis()
        if redis:
            try:
                await redis.setex(REDIS_TOKEN_KEY, TOKEN_TTL, token)
            except Exception:
                pass

        self._token_cache = token
        self._token_ts = time.time()
        log.info("core_token_refreshed")
        return token

    # ── Общий REST-вызов с retry и 401-refresh ─────────────────────

    async def _call_rest(self, url: str, body: dict) -> dict:
        """
        POST {url} с Bearer-авторизацией.
        Retry: max_retries попыток, backoff [1, 3, 10] сек.
        При 401 → однократный refresh + повтор (не считается как attempt).
        """
        token = await self._get_token()
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    }
                    resp = await client.post(url, json=body, headers=headers)

                    if resp.status_code == 401:
                        log.info("core_token_expired_refreshing", url=url, attempt=attempt)
                        token = await self._refresh_token()
                        headers["Authorization"] = f"Bearer {token}"
                        resp = await client.post(url, json=body, headers=headers)
                        if resp.status_code == 401:
                            raise CoreAPIUnavailableError(
                                message=f"401 after token refresh: {url}"
                            )

                    if resp.status_code == 404:
                        raise PolicyNotFoundError(f"404 at {url}")

                    resp.raise_for_status()
                    return resp.json()

            except (PolicyNotFoundError, CoreAPIUnavailableError):
                raise

            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                log.warning("core_api_retry", url=url, attempt=attempt + 1, error=str(e))
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])

        raise CoreAPIUnavailableError(
            message=f"Failed after {self._max_retries} attempts ({url}): {last_error}"
        )

    # ── LiteMed API: getpolicylist ──────────────────────────────────

    async def get_policy_list(self, personal_number: str) -> list[dict]:
        """
        POST /api/Client/getpolicylist
        Body: {"personalnumber": "...", "STATE": "0", "schedule": "0"}
        Ответ: {"PolicyList": "" | "[{...}]" | [{...}]}
        """
        url = f"{self._data_base}/api/Client/getpolicylist"
        result = await self._call_rest(url, {
            "personalnumber": personal_number,
            "STATE": "0",
            "schedule": "1",
        })

        policy_list = result.get("PolicyList", [])

        # Кор-система может вернуть PolicyList как строку (в т.ч. пустую или JSON)
        if isinstance(policy_list, str):
            if not policy_list.strip():
                return []
            try:
                policy_list = json.loads(policy_list)
            except json.JSONDecodeError:
                log.warning("policy_list_parse_failed", raw=policy_list[:200])
                return []

        return policy_list if isinstance(policy_list, list) else []

    async def _find_policy(
        self,
        policy_number: str,
        personal_number: str | None,
    ) -> dict:
        """
        Найти полис по номеру медкарточки в списке полисов пользователя.
        Если personal_number не задан — возвращает пустой dict (без ошибки).
        Если список непустой но нужный полис не найден → PolicyNotFoundError.
        """
        if not personal_number:
            log.warning(
                "core_find_policy_no_personal_number",
                policy_number=policy_number,
            )
            return {}

        policies = await self.get_policy_list(personal_number)

        if not policies:
            # Нет активных полисов у пользователя
            raise PolicyNotFoundError(
                f"No active policies for personal_id={personal_number}"
            )

        # Перебираем возможные имена поля с номером полиса
        _NUM_KEYS = (
            "PolicyNumber", "policyNumber", "POLICY_NUMBER",
            "policy_number", "MedCard", "medcard",
        )
        for p in policies:
            pnum = ""
            for k in _NUM_KEYS:
                if k in p:
                    pnum = str(p[k])
                    break
            if pnum == policy_number:
                return p

        raise PolicyNotFoundError(
            f"Policy {policy_number} not found for personal_id={personal_number}"
        )

    # ── Метод 1: Генеральный договор ───────────────────────────────

    async def get_contract(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> ContractData:
        """
        Извлечь текст договора из данных полиса (getpolicylist).
        Если текст договора не доступен через API — возвращает пустое поле content
        (RAG-индексация не запустится, заявка уйдёт в manual_review).
        """
        policy = await self._find_policy(policy_number, personal_number)

        content = (
            policy.get("ContractText")
            or policy.get("contractText")
            or policy.get("Contract")
            or policy.get("contract")
            or ""
        )
        content_hash = (
            policy.get("ContentHash")
            or policy.get("contentHash")
            or policy.get("content_hash")
        )
        version = (
            policy.get("Version")
            or policy.get("version")
            or policy.get("PolicyVersion")
        )

        return ContractData(
            policy_number=policy_number,
            content=str(content),
            content_hash=str(content_hash) if content_hash else None,
            version=str(version) if version else None,
        )

    # ── Метод 2: Риски и лимиты ────────────────────────────────────

    async def get_risks_and_limits(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> RisksAndLimits:
        """Извлечь риски, лимиты и остатки из данных полиса."""
        policy = await self._find_policy(policy_number, personal_number)

        risks: list[RiskInfo] = []
        raw_risks = (
            policy.get("RiskList")
            or policy.get("riskList")
            or policy.get("Risks")
            or policy.get("risks")
            or []
        )
        if isinstance(raw_risks, str):
            try:
                raw_risks = json.loads(raw_risks)
            except json.JSONDecodeError:
                raw_risks = []

        for r in (raw_risks if isinstance(raw_risks, list) else []):
            try:
                risks.append(RiskInfo(
                    risk_id=int(
                        r.get("RiskID") or r.get("riskId") or r.get("risk_id") or 0
                    ),
                    name=str(
                        r.get("RiskName") or r.get("riskName") or r.get("name") or ""
                    ),
                    coverage_pct=float(
                        r.get("CoveragePct") or r.get("coveragePct")
                        or r.get("coverage_pct") or r.get("Percent") or 100
                    ),
                    total_limit=float(
                        r.get("TotalLimit") or r.get("totalLimit")
                        or r.get("total_limit") or r.get("Limit") or 0
                    ),
                    remaining_limit=float(
                        r.get("RemainingLimit") or r.get("remainingLimit")
                        or r.get("remaining_limit") or r.get("Remaining") or 0
                    ),
                    currency=str(r.get("Currency") or r.get("currency") or "GEL"),
                    services=r.get("Services") or r.get("services") or [],
                ))
            except Exception as exc:
                log.warning("risk_parse_error", error=str(exc), raw=str(r)[:200])

        annual_limit = float(
            policy.get("AnnualLimit") or policy.get("annualLimit")
            or policy.get("annual_limit") or policy.get("Limit") or 0
        )
        remaining = float(
            policy.get("Remaining") or policy.get("remaining")
            or policy.get("RemainingLimit") or 0
        )
        currency = str(policy.get("Currency") or policy.get("currency") or "GEL")

        return RisksAndLimits(
            policy_number=policy_number,
            risks=risks,
            annual_limit=annual_limit,
            remaining=remaining,
            currency=currency,
        )

    # ── Метод 3: Справочник ICD10 ──────────────────────────────────

    async def get_icd10_list(self) -> list[ICD10Item]:
        """
        LiteMed API не предоставляет справочник ICD10.
        DiagnosID берётся из локальной таблицы icd10_diagnoses (icd10_enricher).
        Возвращаем пустой список — decision layer использует локальный справочник.
        """
        log.debug("icd10_list_from_local_db_used")
        return []

    # ── Метод 4: Справочник провайдеров ────────────────────────────

    async def get_providers(self) -> list[ProviderInfo]:
        """
        LiteMed API не предоставляет endpoint для справочника провайдеров.
        Список провайдеров задаётся статически (PersID подтверждены владельцем кор-системы).
        """
        return [
            ProviderInfo(pers_id=2353, name="შპს ნიუ ჰოსპიტალს",                    inn="205210467"),
            ProviderInfo(pers_id=3469, name='შპს ჰეპატოლოგიური კლინიკა „ჰეპა"', inn="205093147"),
        ]

    # ── Метод 5: ClaimParsing_UNI ───────────────────────────────────

    async def submit_claim(
        self,
        policy_number: str,
        diagnosid: int,
        event_start_date: str,
        event_end_date: str,
        pers_id: int,
        config_kind: int,
        risks_list: list[dict],
        file_fields: list[dict],
        comment: str,
    ) -> SubmitClaimResult:
        """
        Создать убыток через ClaimParsing_UNI.

        URL: {core_api_claims_base_url}/LiteApi/LiteServiceJSON
             core_api_claims_base_url задаётся в .env.
             Уточнить у владельца кор-системы Lite GROUP.
        """
        if not settings.core_api_claims_base_url:
            log.warning(
                "claims_url_not_configured",
                msg="core_api_claims_base_url не задан в .env. "
                    "ClaimParsing_UNI вызывается на core_api_base_url.",
            )

        result = await self._call_rest(
            self._claims_url,
            {
                "METHODNAME": "ClaimParsing_UNI",
                "XML_DATA": {
                    "PolicyNumber":   policy_number,
                    "DiagnosID":      diagnosid,
                    "EventStartDate": event_start_date,
                    "EventEndDate":   event_end_date,
                    "PersID":         pers_id,
                    "ConfigKind":     config_kind,
                    "Comment":        comment,
                    "RisksList":      risks_list,
                    "file_fields":    file_fields,
                },
            },
        )

        data = result.get("responseData", [{}])
        if isinstance(data, list):
            data = data[0] if data else {}

        status_code = int(data.get("status", -1))
        return SubmitClaimResult(
            innum=str(data.get("Innum") or data.get("innum") or ""),
            status=status_code,
            status_text=str(data.get("StatusText") or data.get("status_text") or ""),
        )


# ── Mock-адаптер для dev-окружения ─────────────────────────────────

class MockCoreAdapter(CoreSystemAdapter):
    """
    Заглушка для dev/тестов.
    Активируется когда ENVIRONMENT=development и core_api_base_url=http://mock-core.
    """

    async def get_contract(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> ContractData:
        log.warning("mock_core_adapter_used", method="get_contract")
        return ContractData(
            policy_number=policy_number,
            content=(
                "ГЕНЕРАЛЬНЫЙ ДОГОВОР ДМС (ТЕСТОВЫЙ)\n"
                "Раздел 1. Страховые случаи.\n"
                "Покрываются: амбулаторное лечение, диагностика, госпитализация.\n"
                "Раздел 2. Исключения.\n"
                "Не покрываются: косметические процедуры, лечение за рубежом.\n"
                "Раздел 3. Лимиты.\n"
                "Годовой лимит: 5000 GEL. Франшиза: 0 GEL.\n"
            ),
            content_hash="mock-hash-v1",
            version="v1.0",
        )

    async def get_risks_and_limits(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> RisksAndLimits:
        log.warning("mock_core_adapter_used", method="get_risks_and_limits")
        return RisksAndLimits(
            policy_number=policy_number,
            risks=[
                RiskInfo(
                    risk_id=1,
                    name="Амбулаторное лечение",
                    coverage_pct=80.0,
                    total_limit=2000.0,
                    remaining_limit=1500.0,
                    currency="GEL",
                    services=[{"serviceid": "AMB-001", "name": "Приём врача", "config_kind": 1}],
                ),
                RiskInfo(
                    risk_id=2,
                    name="Диагностика",
                    coverage_pct=100.0,
                    total_limit=1000.0,
                    remaining_limit=1000.0,
                    currency="GEL",
                    services=[{"serviceid": "DIAG-001", "name": "Анализы/УЗИ", "config_kind": 2}],
                ),
            ],
            annual_limit=5000.0,
            remaining=4250.0,
            currency="GEL",
        )

    async def get_icd10_list(self) -> list[ICD10Item]:
        log.warning("mock_core_adapter_used", method="get_icd10_list")
        return [
            ICD10Item(diagnosid=101, code="J06.9", name="Острая инфекция верхних дыхательных путей"),
            ICD10Item(diagnosid=102, code="Z00.0", name="Общий медицинский осмотр"),
            ICD10Item(diagnosid=103, code="K29.7", name="Гастрит неуточнённый"),
            ICD10Item(diagnosid=104, code="M54.5", name="Боль в нижней части спины"),
            ICD10Item(diagnosid=105, code="J45.9", name="Астма неуточнённая"),
        ]

    async def get_providers(self) -> list[ProviderInfo]:
        log.warning("mock_core_adapter_used", method="get_providers")
        return [
            ProviderInfo(pers_id=2353, name="შპს ნიუ ჰოსპიტალს",                    inn="205210467"),
            ProviderInfo(pers_id=3469, name='შპს ჰეპატოლოგიური კლინიკა „ჰეპა"', inn="205093147"),
        ]

    async def submit_claim(
        self,
        policy_number: str,
        diagnosid: int,
        event_start_date: str,
        event_end_date: str,
        pers_id: int,
        config_kind: int,
        risks_list: list[dict],
        file_fields: list[dict],
        comment: str,
    ) -> SubmitClaimResult:
        log.warning(
            "mock_core_adapter_used",
            method="submit_claim",
            policy_number=policy_number,
            diagnosid=diagnosid,
            risks_count=len(risks_list),
            files_count=len(file_fields),
        )
        return SubmitClaimResult(
            innum=f"MOCK-{policy_number[:6]}-001",
            status=0,
            status_text="Request completed successfully (MOCK)",
        )
