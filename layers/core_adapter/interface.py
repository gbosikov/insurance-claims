"""
Слой 6 — Core System Adapter: абстрактный интерфейс.

Кор-система Lite GROUP состоит из двух API:
1. LiteMed API  — данные полисов: POST /api/Client/getpolicylist (Bearer-токен)
2. Claims API   — создание убытка: POST /LiteApi/LiteServiceJSON (ClaimParsing_UNI)

Аутентификация для обоих: POST /api/User/authenticate → Bearer-токен.
"""

from abc import ABC, abstractmethod

from core.schemas.core_api import ContractData, ICD10Item, ProviderInfo, RisksAndLimits, SubmitClaimResult


class CoreSystemAdapter(ABC):
    """Абстрактный интерфейс кор-системы Lite GROUP."""

    @abstractmethod
    async def get_contract(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> ContractData:
        """
        Получить генеральный договор.
        personal_number (личный номер застрахованного) требуется для LiteGroup API:
        используется в POST /api/Client/getpolicylist для нахождения нужного полиса.
        """

    @abstractmethod
    async def get_risks_and_limits(
        self,
        policy_number: str,
        personal_number: str | None = None,
    ) -> RisksAndLimits:
        """
        Получить список рисков, % покрытия, лимиты и остатки.
        personal_number используется так же как в get_contract().
        """

    @abstractmethod
    async def get_icd10_list(self) -> list[ICD10Item]:
        """
        Получить справочник диагнозов ICD10 с DiagnosID для ClaimParsing_UNI.
        Результат кэшируется в Redis на 24 часа.
        Примечание: LiteMed API не предоставляет этот справочник.
        Если кор-система его не отдаёт — DiagnosID берётся из локальной таблицы icd10_diagnoses.
        """

    @abstractmethod
    async def get_providers(self) -> list[ProviderInfo]:
        """
        Получить справочник провайдеров (медицинских учреждений).
        Возвращает список с полями: PersID, название.
        Результат кэшируется в Redis на 24 часа.
        Примечание: LiteMed API не предоставляет этот справочник напрямую.
        """

    @abstractmethod
    async def submit_claim(
        self,
        policy_number: str,
        diagnosid: int,
        event_start_date: str,   # "YYYY-MM-DD"
        event_end_date: str,     # "YYYY-MM-DD"
        pers_id: int,
        config_kind: int,
        risks_list: list[dict],  # [{RiskID, FinalAmount, ServDate, serviceid, ServName}]
        file_fields: list[dict], # [{file_data: base64, file_name, fkind}]
        comment: str,            # полный AI-вердикт
    ) -> SubmitClaimResult:
        """
        Финальный шаг: создать убыток через ClaimParsing_UNI.
        Вызывается ВСЕГДА — независимо от уровня уверенности AI.
        comment = полный вердикт Claude (решение + обоснование + уверенность).

        Коды ответа:
          0 → успех (Innum = номер направления)
          1 → не заполнен номер медкарточки
          2 → не заполнен код диагноза
          3 → не заполнен код партнёра
          4 → не заполнен вид направления
          5 → полис не существует
          6 → не указан банковский счёт получателя
          7 → данные получателя пустые
          8 → системное сообщение
          9 → нет завершённого направления
        """
