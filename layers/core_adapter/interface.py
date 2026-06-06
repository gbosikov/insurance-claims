"""
Слой 6 — Core System Adapter: абстрактный интерфейс.

Кор-система: Lite GROUP (http://192.168.0.249:8077)
Аутентификация: POST /api/User/authenticate → JWT-токен
Вызов методов: POST /LiteApi/LiteServiceJSON
"""

from abc import ABC, abstractmethod

from core.schemas.core_api import ContractData, ICD10Item, ProviderInfo, RisksAndLimits, SubmitClaimResult


class CoreSystemAdapter(ABC):
    """Абстрактный интерфейс кор-системы Lite GROUP."""

    @abstractmethod
    async def get_contract(self, policy_number: str) -> ContractData:
        """
        Получить генеральный договор по номеру медицинской карточки.
        Текст договора используется для RAG-индексации.
        Метод: TODO_CONTRACT_METHOD (уточнить у владельца кор-системы)
        """

    @abstractmethod
    async def get_risks_and_limits(self, policy_number: str) -> RisksAndLimits:
        """
        Получить список рисков, % покрытия, лимиты и остатки.
        Метод: TODO_RISKS_METHOD (уточнить у владельца кор-системы)
        """

    @abstractmethod
    async def get_icd10_list(self) -> list[ICD10Item]:
        """
        Получить справочник диагнозов ICD10.
        Результат кэшируется в Redis на 24 часа.
        Метод: TODO_ICD10_METHOD (уточнить у владельца кор-системы)
        """

    @abstractmethod
    async def get_providers(self) -> list[ProviderInfo]:
        """
        Получить справочник провайдеров (медицинских учреждений).
        Возвращает список с полями: PersID, название, ИНН.
        Результат кэшируется в Redis на 24 часа.
        Метод: TODO_PROVIDERS_METHOD (уточнить у владельца кор-системы)
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
        Финальный шаг: создать убыток в кор-системе (ClaimParsing_UNI).
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
