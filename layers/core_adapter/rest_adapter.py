"""
Слой 6 — Реализация Core System Adapter для Lite GROUP.

Аутентификация: POST /api/User/authenticate → JWT-токен
Токен кэшируется в Redis (TTL=1ч), обновляется при 401.
Вызов методов: POST /LiteApi/LiteServiceJSON
  Body: {"METHODNAME": "...", "XML_DATA": {...}}
  Header: Authorization: <TOKEN>
"""

from __future__ import annotations

import asyncio
import base64
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
TOKEN_TTL = 3600       # 1 час
ICD10_CACHE_TTL = 86400  # 24 часа


class LiteGroupAdapter(CoreSystemAdapter):
    """
    Реализация для кор-системы Lite GROUP.

    Работа с токеном:
    - Первый запрос → получает токен через /api/User/authenticate
    - Кэшируется в Redis (TTL=1ч) или in-memory (если Redis недоступен)
    - При 401 → автоматически обновляет токен и повторяет запрос
    """

    def __init__(self) -> None:
        self._base_url = settings.core_api_base_url.rstrip("/")
        self._service_url = f"{self._base_url}/LiteApi/LiteServiceJSON"
        self._auth_url = f"{self._base_url}/api/User/authenticate"
        self._timeout = settings.core_api_timeout
        self._max_retries = settings.core_api_retry
        # Fallback in-memory token cache (если Redis недоступен)
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
        """Вернуть токен из кэша или запросить новый."""
        redis = await self._get_redis()

        if redis:
            try:
                cached = await redis.get(REDIS_TOKEN_KEY)
                if cached:
                    return cached
            except Exception:
                pass

        # In-memory fallback
        if self._token_cache and (time.time() - self._token_ts) < TOKEN_TTL:
            return self._token_cache

        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """Получить новый JWT-токен через /api/User/authenticate."""
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
            # Поле может быть "TOKEN" или "token" в зависимости от версии API
            token = data.get("TOKEN") or data.get("token") or data.get("Token", "")

        if not token:
            raise CoreAPIUnavailableError(message="Empty token from /api/User/authenticate")

        # Кэшируем в Redis
        redis = await self._get_redis()
        if redis:
            try:
                await redis.setex(REDIS_TOKEN_KEY, TOKEN_TTL, token)
            except Exception:
                pass

        # In-memory fallback
        self._token_cache = token
        self._token_ts = time.time()

        log.info("core_token_refreshed")
        return token

    # ── Универсальный вызов метода ──────────────────────────────────

    async def _call(self, method_name: str, xml_data: dict) -> dict:
        """
        POST /LiteApi/LiteServiceJSON
        Retry: max_retries попыток, backoff [1, 3, 10] сек.
        При 401 → refresh_token + один inline повтор (не считается как attempt).
        Два 401 подряд (после refresh) → CoreAPIUnavailableError.
        """
        token = await self._get_token()
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        self._service_url,
                        json={"METHODNAME": method_name, "XML_DATA": xml_data},
                        headers={"Authorization": token, "Content-Type": "application/json"},
                    )

                    if resp.status_code == 401:
                        log.info("core_token_expired_refreshing", attempt=attempt)
                        token = await self._refresh_token()
                        # Один inline повтор с новым токеном — не считается как attempt (#4)
                        resp = await client.post(
                            self._service_url,
                            json={"METHODNAME": method_name, "XML_DATA": xml_data},
                            headers={"Authorization": token, "Content-Type": "application/json"},
                        )
                        if resp.status_code == 401:
                            raise CoreAPIUnavailableError(
                                message=f"Method={method_name}: 401 after token refresh"
                            )

                    if resp.status_code == 404:
                        raise PolicyNotFoundError(f"Method={method_name}, policy not found")

                    resp.raise_for_status()
                    return resp.json()

            except (PolicyNotFoundError, CoreAPIUnavailableError):
                raise

            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                last_error = e
                log.warning(
                    "core_api_retry",
                    method=method_name,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])

        raise CoreAPIUnavailableError(
            message=f"Method={method_name} failed after {self._max_retries} attempts: {last_error}"
        )

    # ── Метод 1: Получить генеральный договор ──────────────────────

    async def get_contract(self, policy_number: str) -> ContractData:
        """
        Получить генеральный договор по номеру медкарточки.
        METHODNAME: TODO_CONTRACT_METHOD (уточнить у владельца)
        """
        result = await self._call(
            method_name="TODO_CONTRACT_METHOD",  # ← уточнить
            xml_data={"PolicyNumber": policy_number},
        )
        data = result.get("responseData", [{}])
        if isinstance(data, list):
            data = data[0] if data else {}

        return ContractData(
            policy_number=policy_number,
            content=data.get("ContractText") or data.get("content", ""),
            content_hash=data.get("ContentHash") or data.get("content_hash"),
            version=data.get("Version") or data.get("version"),
        )

    # ── Метод 2: Получить риски и лимиты ───────────────────────────

    async def get_risks_and_limits(self, policy_number: str) -> RisksAndLimits:
        """
        Получить список рисков, % покрытия, лимиты.
        METHODNAME: TODO_RISKS_METHOD (уточнить у владельца)
        """
        result = await self._call(
            method_name="TODO_RISKS_METHOD",  # ← уточнить
            xml_data={"PolicyNumber": policy_number},
        )
        data = result.get("responseData", [{}])
        if isinstance(data, list):
            data = data[0] if data else {}

        risks = []
        for r in data.get("Risks", data.get("risks", [])):
            risks.append(RiskInfo(
                risk_id=int(r.get("RiskID", 0)),
                name=r.get("RiskName", r.get("name", "")),
                coverage_pct=float(r.get("CoveragePct", r.get("coverage_pct", 100))),
                total_limit=float(r.get("TotalLimit", r.get("total_limit", 0))),
                remaining_limit=float(r.get("RemainingLimit", r.get("remaining_limit", 0))),
                currency=r.get("Currency", r.get("currency", "GEL")),
                services=r.get("Services", r.get("services", [])),
            ))

        return RisksAndLimits(
            policy_number=policy_number,
            risks=risks,
            annual_limit=float(data.get("AnnualLimit", data.get("annual_limit", 0))),
            remaining=float(data.get("Remaining", data.get("remaining", 0))),
            currency=data.get("Currency", "GEL"),
        )

    # ── Метод 3: Справочник диагнозов ICD10 ────────────────────────

    async def get_icd10_list(self) -> list[ICD10Item]:
        """
        Получить справочник диагнозов ICD10.
        Кэшируется в Redis на 24 часа — меняется редко.
        METHODNAME: TODO_ICD10_METHOD (уточнить у владельца)
        """
        redis = await self._get_redis()
        if redis:
            try:
                cached = await redis.get(REDIS_ICD10_KEY)
                if cached:
                    return [ICD10Item(**item) for item in json.loads(cached)]
            except Exception:
                pass

        result = await self._call(
            method_name="TODO_ICD10_METHOD",  # ← уточнить
            xml_data={},
        )
        raw_list = result.get("responseData", [])
        if not isinstance(raw_list, list):
            raw_list = []

        items = []
        for r in raw_list:
            try:
                items.append(ICD10Item(
                    diagnosid=int(r.get("DiagnosID", 0)),
                    code=r.get("ICD10Code", r.get("code", "")),
                    name=r.get("Description", r.get("name", "")),
                ))
            except Exception:
                continue

        if redis and items:
            try:
                await redis.setex(
                    REDIS_ICD10_KEY,
                    ICD10_CACHE_TTL,
                    json.dumps([i.model_dump() for i in items]),
                )
            except Exception:
                pass

        return items

    # ── Метод 4: Справочник провайдеров ────────────────────────────

    REDIS_PROVIDERS_KEY = "lite_group:providers"
    PROVIDERS_CACHE_TTL = 86400  # 24 часа

    async def get_providers(self) -> list[ProviderInfo]:
        """
        Получить справочник медицинских учреждений (провайдеров).
        Кэшируется в Redis на 24 часа.
        METHODNAME: TODO_PROVIDERS_METHOD (уточнить у владельца)
        Ожидаемый формат ответа: [{ PersID, Name, INN }]
        """
        redis = await self._get_redis()
        if redis:
            try:
                cached = await redis.get(self.REDIS_PROVIDERS_KEY)
                if cached:
                    return [ProviderInfo(**item) for item in json.loads(cached)]
            except Exception:
                pass

        result = await self._call(
            method_name="TODO_PROVIDERS_METHOD",  # ← уточнить
            xml_data={},
        )
        raw_list = result.get("responseData", [])
        if not isinstance(raw_list, list):
            raw_list = []

        items = []
        for r in raw_list:
            try:
                items.append(ProviderInfo(
                    pers_id=int(r.get("PersID", 0)),
                    name=r.get("Name", r.get("name", "")),
                    inn=str(r.get("INN", r.get("inn", ""))),
                ))
            except Exception:
                continue

        if redis and items:
            try:
                await redis.setex(
                    self.REDIS_PROVIDERS_KEY,
                    self.PROVIDERS_CACHE_TTL,
                    json.dumps([i.model_dump() for i in items]),
                )
            except Exception:
                pass

        return items

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
        """ClaimParsing_UNI — создать убыток в кор-системе."""
        result = await self._call(
            method_name="ClaimParsing_UNI",
            xml_data={
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
        )

        data = result.get("responseData", [{}])
        if isinstance(data, list):
            data = data[0] if data else {}

        status_code = int(data.get("status", -1))
        return SubmitClaimResult(
            innum=data.get("Innum", ""),
            status=status_code,
            status_text=data.get("StatusText", ""),
        )


# ── Mock-адаптер для dev-окружения ─────────────────────────────────

class MockCoreAdapter(CoreSystemAdapter):
    """
    Заглушка для dev/тестов.
    Активируется когда CORE_API_BASE_URL=http://mock-core.
    """

    async def get_contract(self, policy_number: str) -> ContractData:
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

    async def get_risks_and_limits(self, policy_number: str) -> RisksAndLimits:
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
            ProviderInfo(pers_id=1, name="Клиника Аврора",        inn="123456789"),
            ProviderInfo(pers_id=2, name="МЦ Мединтер",           inn="987654321"),
            ProviderInfo(pers_id=3, name="Диагностический центр", inn="111222333"),
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
