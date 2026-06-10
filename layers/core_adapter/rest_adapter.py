"""
Слой 6 — Реализация Core System Adapter для Lite GROUP.

Два API:
1. LiteMed API (данные полисов):
     Auth:  POST {core_api_auth_url}/api/User/authenticate → {"token": "..."}
     Data:  POST {core_api_base_url}/api/Client/getpolicylist
            Body: {"personalnumber": "...", "STATE": "0", "schedule": "1"}
            Header: Authorization: {core_api_auth_scheme} <token>

2. Claims API (создание убытка):
     POST {core_api_claims_base_url}/LiteApi/LiteServiceJSON
     Body: {"METHODNAME": "ClaimParsing_UNI", "XML_DATA": {...}}
     URL уточнить у владельца кор-системы (core_api_claims_base_url в .env).

Реальная структура ответа getpolicylist (верифицирована 2026-06-11):
  {"PolicyList": {"Policy": [{
      "Number": "MED 536638", "CardNumber": "UNI 700003/1", "OldNumber": "...",
      "StartDate": "01/01/2026", "EndDate": "01/01/2027", "StopDate": "",
      "ObjectList": {"Objects": {           ← dict ИЛИ list (XML→JSON артефакт!)
          "PersonalNumber": "...", "StartDate": "...", "ObjectData": "...",
          "InsuranceTypeList": {"InsuranceType": [{
              "TypeID": "23", "Amount": "27000.00", "AmountCurrency": "GEL",
              "RiskList": {"Risk": [{       ← dict ИЛИ list
                  "RiskId": "...", "RiskParentId": "0", "RiskName": "...",
                  "LimitAmount": "3000.00", "LimitCount": "",
                  "LinitPercent": "80",     ← опечатка в API (Linit)
                  "LimitAmountLeft": "2927.81", "LimitCountLeft": "0"}]}}]}}}}]}}

Особенности: все значения — строки; даты DD/MM/YYYY; одноэлементные списки
свёрнуты в dict; текст договора в ответе ОТСУТСТВУЕТ (ClauseList="").
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime

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


# ── Нормализация ответа кор-системы (XML→JSON артефакты) ──────────

def _ensure_list(value) -> list:
    """Одноэлементные списки приходят свёрнутыми в dict; пустые — как ""."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _parse_core_date(value) -> date | None:
    """Даты кор-системы: DD/MM/YYYY. Пусто/невалидно → None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except ValueError:
        return None


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_int_or_none(value) -> int | None:
    """"" → None; "0" → 0 (значимо: исчерпанный количественный лимит)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _norm_number(value) -> str:
    """Номер полиса/карточки для сравнения: без пробелов, верхний регистр."""
    return str(value or "").replace(" ", "").upper()



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

    @staticmethod
    def _auth_header_value(token: str) -> str:
        """Схема настраивается: "Bearer <token>" или сырой токен (см. config)."""
        scheme = settings.core_api_auth_scheme.strip()
        return f"{scheme} {token}" if scheme else token

    async def _call_rest(self, url: str, body: dict) -> dict:
        """
        POST {url} с авторизацией (схема — core_api_auth_scheme).
        Retry: max_retries попыток, backoff [1, 3, 10] сек.
        При 401 → однократный refresh + повтор (не считается как attempt).
        """
        token = await self._get_token()
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    headers = {
                        "Authorization": self._auth_header_value(token),
                        "Content-Type": "application/json",
                    }
                    resp = await client.post(url, json=body, headers=headers)

                    if resp.status_code == 401:
                        log.info("core_token_expired_refreshing", url=url, attempt=attempt)
                        token = await self._refresh_token()
                        headers["Authorization"] = self._auth_header_value(token)
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
        Body: {"personalnumber": "...", "STATE": "0", "schedule": "1"}
        Ответ (верифицирован): {"PolicyList": {"Policy": [...] | {...}}}.
        Защита: строковый вариант ("" | "[...]") тоже разбирается.
        """
        url = f"{self._data_base}/api/Client/getpolicylist"
        result = await self._call_rest(url, {
            "personalnumber": personal_number,
            "STATE": "0",
            "schedule": "1",
        })

        policy_list = result.get("PolicyList", [])

        # Защита: кор-система может вернуть PolicyList строкой (пустой или JSON)
        if isinstance(policy_list, str):
            if not policy_list.strip():
                return []
            try:
                policy_list = json.loads(policy_list)
            except json.JSONDecodeError:
                log.warning("policy_list_parse_failed", raw_length=len(policy_list))
                return []

        # Реальный формат: {"Policy": [...]} (одноэлементный может быть dict)
        if isinstance(policy_list, dict):
            return _ensure_list(policy_list.get("Policy"))

        return policy_list if isinstance(policy_list, list) else []

    async def _find_policy(
        self,
        policy_number: str,
        personal_number: str | None,
    ) -> dict:
        """
        Найти ДМС-полис в списке полисов пользователя.

        Рассматриваются ТОЛЬКО медицинские продукты
        (Policy.ProductName ∈ core_api_medical_product_names) —
        имущественные и прочие полисы клиента игнорируются.
        Номер сверяется (без пробелов, без регистра) с полями:
        CardNumber (номер медкарточки — основной идентификатор для ДМС),
        Number (номер полиса), OldNumber (старый номер договора).
        Расторгнутые полисы (StopDate непустой) пропускаются.
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

        allowed_products = {
            name.strip() for name in settings.core_api_medical_product_names
        }
        target = _norm_number(policy_number)

        for p in policies:
            if str(p.get("StopDate") or "").strip():
                continue  # полис расторгнут
            product = str(p.get("ProductName") or "").strip()
            if allowed_products and product not in allowed_products:
                continue  # не медицинский продукт (имущество, НС и т.п.)
            candidates = (p.get("CardNumber"), p.get("Number"), p.get("OldNumber"))
            if any(_norm_number(c) == target for c in candidates if c):
                return p

        raise PolicyNotFoundError(
            f"Medical policy {policy_number} not found for personal_id={personal_number}"
        )

    @staticmethod
    def _extract_insured_object(policy: dict, personal_number: str | None) -> dict:
        """
        Застрахованный объект полиса (ObjectList.Objects — dict или list).
        При нескольких застрахованных выбирается по личному номеру.
        """
        objects = _ensure_list((policy.get("ObjectList") or {}).get("Objects"))
        if personal_number:
            target = str(personal_number).strip()
            for obj in objects:
                if str(obj.get("PersonalNumber") or "").strip() == target:
                    return obj
        return objects[0] if objects else {}

    # ── Метод 1: Генеральный договор ───────────────────────────────

    async def get_contract(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> ContractData:
        """
        Текст договора из данных полиса.

        ВЕРИФИЦИРОВАНО (2026-06-11): getpolicylist НЕ возвращает текст договора —
        в реальном ответе есть только ClauseList (пустой в примере). Пробуем
        ClauseList на случай, если он заполняется; иначе content="" →
        RAG вернёт пустой список → decision поставит requires_manual_review.
        ОТКРЫТО: канал доставки текста договора — вопрос владельцу Lite GROUP
        (пока договоры загружаются вручную через POST /v1/contracts).
        """
        policy = await self._find_policy(policy_number, personal_number)

        clause_list = policy.get("ClauseList") or ""
        if isinstance(clause_list, (dict, list)):
            content = json.dumps(clause_list, ensure_ascii=False)
        else:
            content = str(clause_list)

        if not content.strip():
            log.info(
                "contract_text_not_in_policy_response",
                policy_number=policy_number,
            )

        return ContractData(
            policy_number=policy_number,
            content=content,
            content_hash=None,
            version=None,
        )

    # ── Метод 2: Риски и лимиты ────────────────────────────────────

    async def get_risks_and_limits(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> RisksAndLimits:
        """
        Риски, лимиты и остатки из данных полиса (структура верифицирована).

        Путь: Policy → ObjectList.Objects (по личному номеру) →
        InsuranceTypeList.InsuranceType (медицинские TypeID из настройки) →
        RiskList.Risk.

        annual_limit = страховая сумма медицинского типа (Amount).
        remaining: на уровне полиса остатка нет — берётся максимум
        LimitAmountLeft по рискам с собственным денежным лимитом
        (все суб-лимиты исчерпаны → 0 → manual_review «limit_exhausted»).
        """
        policy = await self._find_policy(policy_number, personal_number)
        insured = self._extract_insured_object(policy, personal_number)

        ins_types = _ensure_list(
            (insured.get("InsuranceTypeList") or {}).get("InsuranceType")
        )
        medical_types = [
            t for t in ins_types
            if _to_int_or_none(t.get("TypeID")) in settings.core_api_medical_type_ids
        ]
        selected_types = medical_types or ins_types
        if ins_types and not medical_types:
            log.warning(
                "medical_insurance_type_not_found",
                policy_number=policy_number,
                type_ids=[t.get("TypeID") for t in ins_types],
            )

        risks: list[RiskInfo] = []
        annual_limit = 0.0
        currency = "GEL"

        for ins_type in selected_types:
            type_currency = str(ins_type.get("AmountCurrency") or "GEL")
            type_amount = _to_float(ins_type.get("Amount"))
            if type_amount > annual_limit:
                annual_limit = type_amount
                currency = type_currency

            for r in _ensure_list((ins_type.get("RiskList") or {}).get("Risk")):
                try:
                    limit_amount = _to_float(r.get("LimitAmount"))
                    parent_id = _to_int_or_none(r.get("RiskParentId"))
                    risks.append(RiskInfo(
                        risk_id=_to_int_or_none(r.get("RiskId")) or 0,
                        name=str(r.get("RiskName") or "").strip(),
                        # LinitPercent — опечатка в самом API кор-системы
                        coverage_pct=_to_float(r.get("LinitPercent"), default=100.0),
                        total_limit=limit_amount,
                        remaining_limit=_to_float(r.get("LimitAmountLeft")),
                        currency=type_currency,
                        sublimit=limit_amount if limit_amount > 0 else None,
                        parent_risk_id=parent_id if parent_id else None,  # "0" = корневой
                        limit_count=_to_int_or_none(r.get("LimitCount")),
                        limit_count_left=_to_int_or_none(r.get("LimitCountLeft")),
                    ))
                except Exception as exc:
                    log.warning("risk_parse_error", error=str(exc), risk_id=r.get("RiskId"))

        limited_risks = [r for r in risks if (r.sublimit or 0) > 0]
        remaining = max(
            (r.remaining_limit for r in limited_risks),
            default=annual_limit,
        )

        return RisksAndLimits(
            policy_number=policy_number,
            risks=risks,
            annual_limit=annual_limit,
            remaining=remaining,
            currency=currency,
            policy_start_date=_parse_core_date(
                insured.get("StartDate") or policy.get("StartDate")
            ),
            policy_end_date=_parse_core_date(
                insured.get("EndDate") or policy.get("EndDate")
            ),
            object_data=str(insured.get("ObjectData") or "").strip() or None,
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
        Загрузить справочник провайдеров из локальной БД (таблица providers).
        Кэшируется в Redis на 24 часа для избежания частых запросов к БД.
        """
        redis = await self._get_redis()

        # Попытка получить из Redis кэша
        if redis:
            try:
                cached = await redis.get(REDIS_PROVIDERS_KEY)
                if cached:
                    import json
                    data = json.loads(cached)
                    return [ProviderInfo(**item) for item in data]
            except Exception as e:
                log.warning("redis_cache_miss", key=REDIS_PROVIDERS_KEY, error=str(e))

        # Загрузить из БД
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from core.models.provider import Provider

        try:
            async with AsyncSessionLocal() as db:
                stmt = select(Provider).where(Provider.is_active == True)
                result = await db.execute(stmt)
                rows = result.scalars().all()

            providers = [
                ProviderInfo(pers_id=p.customer_id, name=p.cstname, inn=p.taxpayer)
                for p in rows
            ]

            # Кэшировать в Redis на 24 часа
            if redis:
                try:
                    import json
                    cache_data = [
                        {"pers_id": p.pers_id, "name": p.name, "inn": p.inn}
                        for p in providers
                    ]
                    await redis.setex(
                        REDIS_PROVIDERS_KEY,
                        86400,  # 24 часа
                        json.dumps(cache_data),
                    )
                except Exception as e:
                    log.warning("redis_cache_set_failed", key=REDIS_PROVIDERS_KEY, error=str(e))

            return providers

        except Exception as e:
            log.error("providers_load_failed", error=str(e))
            return []

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
        # Mock тоже загружает из БД если есть данные, или возвращает примеры
        from sqlalchemy import select
        from core.database import AsyncSessionLocal
        from core.models.provider import Provider

        try:
            async with AsyncSessionLocal() as db:
                stmt = select(Provider).where(Provider.is_active == True)
                result = await db.execute(stmt)
                rows = result.scalars().all()

            if rows:
                return [
                    ProviderInfo(pers_id=p.customer_id, name=p.cstname, inn=p.taxpayer)
                    for p in rows
                ]
        except Exception:
            pass

        # Fallback на примеры если БД не настроена
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
