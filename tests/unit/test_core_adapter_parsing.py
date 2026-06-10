"""
Unit тесты: парсинг реального формата getpolicylist (верифицирован 2026-06-11).

Фикстура повторяет структуру реального ответа кор-системы
(анонимизированные данные): вложенность PolicyList.Policy[].ObjectList.Objects
.InsuranceTypeList.InsuranceType[].RiskList.Risk[], строковые числа,
даты DD/MM/YYYY, одноэлементные списки свёрнутые в dict.
"""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from layers.core_adapter.rest_adapter import (
    LiteGroupAdapter,
    _ensure_list,
    _norm_number,
    _parse_core_date,
    _to_float,
    _to_int_or_none,
)

PERSONAL_NUMBER = "00000000001"

# Анонимизированная копия реальной структуры ответа:
# 2 полиса (медицинский + имущественный), у медицинского 2 типа страхования
# (несчастный случай с ОДНИМ риском-dict + медицинский со СПИСКОМ рисков)
POLICY_RESPONSE = {
    "PolicyList": {
        "Policy": [
            {
                "Number": "MED 100001",
                "OldNumber": "MED00001",
                "CardNumber": "UNI 900001/1",
                "ProductName": "სამედიცინო (ჯანმრთელობის) დაზღვევა",
                "RiskGroup": "100000",
                "PremiumCurrency": "GEL",
                "StartDate": "01/01/2026",
                "EndDate": "01/01/2027",
                "StopDate": "",
                "CustomerName": "ТестКомпания",
                "PersonalNumber": "400000000",
                "Person": "1",
                "ObjectList": {
                    "Objects": {  # одноэлементный список свёрнут в dict
                        "Name": "Тест",
                        "Surname": "Тестов",
                        "PersonalNumber": PERSONAL_NUMBER,
                        "BirthDay": "02/02/1980",
                        "StartDate": "01/01/2026",
                        "EndDate": "01/01/2027",
                        "StopDate": "",
                        "MarketPrice": "0.00",
                        "ObjectData": "არ ეკუთვნის მოცდის პერიოდი",
                        "InsuranceTypeList": {
                            "InsuranceType": [
                                {
                                    "Amount": "5000.00",
                                    "AmountCurrency": "GEL",
                                    "TypeID": "4",
                                    "TypeName": "დაზღვევა უბედური შემთხვევებისაგან",
                                    "Premium": "24.00",
                                    "PremiumCurrency": "GEL",
                                    "RiskList": {
                                        "Risk": {  # ОДИН риск → dict, не list
                                            "RiskId": "114191",
                                            "RiskParentId": "0",
                                            "hasChild": "0",
                                            "RiskName": "უბედური შემთხვევა",
                                            "LimitAmount": "5000.00",
                                            "LimitCount": "",
                                            "LinitPercent": "100",
                                            "LimitAmountLeft": "5000.00",
                                            "LimitCountLeft": "0",
                                        }
                                    },
                                },
                                {
                                    "Amount": "27000.00",
                                    "AmountCurrency": "GEL",
                                    "TypeID": "23",
                                    "TypeName": "სამედიცინო (ჯანმრთელობის) დაზღვევა",
                                    "Premium": "936.00",
                                    "PremiumCurrency": "GEL",
                                    "RiskList": {
                                        "Risk": [
                                            {
                                                "RiskId": "102063",
                                                "RiskParentId": "0",
                                                "hasChild": "0",
                                                "RiskName": "მედიკამენტები / Medications",
                                                "LimitAmount": "3000.00",
                                                "LimitCount": "",
                                                "LinitPercent": "80",
                                                "LimitAmountLeft": "2927.81",
                                                "LimitCountLeft": "0",
                                            },
                                            {
                                                "RiskId": "101933",
                                                "RiskParentId": "102063",
                                                "hasChild": "1",
                                                "RiskName": "მედიკამენტები ექიმის დანიშნულებით",
                                                "LimitAmount": "0.00",
                                                "LimitCount": "",
                                                "LinitPercent": "70",
                                                "LimitAmountLeft": "0.00",
                                                "LimitCountLeft": "0",
                                            },
                                            {
                                                "RiskId": "102203",
                                                "RiskParentId": "0",
                                                "hasChild": "0",
                                                "RiskName": "პროფილაქტიკური კვლევები",
                                                "LimitAmount": "0.00",
                                                "LimitCount": "2",
                                                "LinitPercent": "100",
                                                "LimitAmountLeft": "0.00",
                                                "LimitCountLeft": "1",
                                            },
                                            {
                                                "RiskId": "103765",
                                                "RiskParentId": "0",
                                                "hasChild": "0",
                                                "RiskName": "ჰოსპიტალიზაცია / Hospital services",
                                                "LimitAmount": "12000.00",
                                                "LimitCount": "",
                                                "LinitPercent": "100",
                                                "LimitAmountLeft": "11920.00",
                                                "LimitCountLeft": "0",
                                            },
                                        ]
                                    },
                                },
                            ]
                        },
                    }
                },
                "ClaimList": "",
                "PayersList": {},
                "ClauseList": "",
            },
            {
                "Number": "PRO 200001",
                "OldNumber": "",
                "CardNumber": "",
                "ProductName": "ქონების დაზღვევა",
                "StartDate": "08/03/2026",
                "EndDate": "08/03/2027",
                "StopDate": "",
                "PersonalNumber": PERSONAL_NUMBER,
                "Person": "0",
                "ObjectList": {"Objects": {
                    "PersonalNumber": PERSONAL_NUMBER,
                    "StartDate": "08/03/2026",
                    "EndDate": "08/03/2027",
                    "ObjectData": "",
                    "InsuranceTypeList": {"InsuranceType": {
                        "Amount": "46140.00", "AmountCurrency": "GEL",
                        "TypeID": "37", "TypeName": "ქონების დაზღვევა",
                        "RiskList": {"Risk": {
                            "RiskId": "103678", "RiskParentId": "0", "hasChild": "0",
                            "RiskName": "ქონების დაზღვევა",
                            "LimitAmount": "0.00", "LimitCount": "",
                            "LinitPercent": "100",
                            "LimitAmountLeft": "0.00", "LimitCountLeft": "0",
                        }},
                    }},
                }},
                "ClaimList": "",
                "ClauseList": "",
            },
        ]
    }
}


def make_adapter(response: dict = POLICY_RESPONSE) -> LiteGroupAdapter:
    adapter = LiteGroupAdapter()
    adapter._call_rest = AsyncMock(return_value=response)
    return adapter


# ── Хелперы нормализации ──────────────────────────────────────────


def test_ensure_list_handles_xml_json_artifacts():
    assert _ensure_list([1, 2]) == [1, 2]
    assert _ensure_list({"a": 1}) == [{"a": 1}]   # одноэлементный → dict
    assert _ensure_list("") == []                  # пустой → ""
    assert _ensure_list(None) == []


def test_parse_core_date_ddmmyyyy():
    assert _parse_core_date("01/01/2026") == date(2026, 1, 1)
    assert _parse_core_date("02/02/1980") == date(1980, 2, 2)
    assert _parse_core_date("") is None
    assert _parse_core_date("2026-01-01") is None  # не наш формат → None, не краш


def test_to_int_or_none_distinguishes_empty_from_zero():
    assert _to_int_or_none("") is None    # лимита нет
    assert _to_int_or_none("0") == 0      # лимит исчерпан — значимо!
    assert _to_int_or_none("2") == 2


def test_norm_number():
    assert _norm_number("UNI 900001/1") == "UNI900001/1"
    assert _norm_number("med 100001") == "MED100001"


# ── get_policy_list ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_list_parsed_from_nested_object():
    adapter = make_adapter()
    policies = await adapter.get_policy_list(PERSONAL_NUMBER)
    assert len(policies) == 2
    assert policies[0]["Number"] == "MED 100001"


@pytest.mark.asyncio
async def test_policy_list_empty_string_returns_empty():
    adapter = make_adapter({"PolicyList": ""})
    assert await adapter.get_policy_list(PERSONAL_NUMBER) == []


@pytest.mark.asyncio
async def test_policy_list_single_policy_dict_normalized():
    """Один полис → Policy приходит как dict."""
    single = {"PolicyList": {"Policy": POLICY_RESPONSE["PolicyList"]["Policy"][0]}}
    adapter = make_adapter(single)
    policies = await adapter.get_policy_list(PERSONAL_NUMBER)
    assert len(policies) == 1


# ── _find_policy ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_policy_by_card_number():
    """Основной идентификатор ДМС — CardNumber (номер медкарточки)."""
    adapter = make_adapter()
    policy = await adapter._find_policy("UNI 900001/1", PERSONAL_NUMBER)
    assert policy["Number"] == "MED 100001"


@pytest.mark.asyncio
async def test_find_policy_by_number_ignoring_spaces_and_case():
    adapter = make_adapter()
    policy = await adapter._find_policy("med100001", PERSONAL_NUMBER)
    assert policy["CardNumber"] == "UNI 900001/1"


@pytest.mark.asyncio
async def test_find_policy_only_medical_product():
    """Берутся только полисы с ProductName медицинского страхования —
    имущественный полис (ქონების დაზღვევა) не подбирается даже по номеру."""
    adapter = make_adapter()

    from core.exceptions import PolicyNotFoundError
    with pytest.raises(PolicyNotFoundError):
        await adapter._find_policy("PRO 200001", PERSONAL_NUMBER)


@pytest.mark.asyncio
async def test_find_policy_skips_terminated():
    """Расторгнутый полис (StopDate непустой) не подбирается."""
    import copy
    response = copy.deepcopy(POLICY_RESPONSE)
    response["PolicyList"]["Policy"][0]["StopDate"] = "01/06/2026"
    adapter = make_adapter(response)

    from core.exceptions import PolicyNotFoundError
    with pytest.raises(PolicyNotFoundError):
        await adapter._find_policy("UNI 900001/1", PERSONAL_NUMBER)


# ── get_risks_and_limits ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_risks_parsed_from_medical_insurance_type():
    """Берутся риски медицинского типа (TypeID=23), не несчастного случая."""
    adapter = make_adapter()
    rl = await adapter.get_risks_and_limits("UNI 900001/1", PERSONAL_NUMBER)

    risk_ids = {r.risk_id for r in rl.risks}
    assert risk_ids == {102063, 101933, 102203, 103765}
    assert 114191 not in risk_ids  # несчастный случай (TypeID=4) не включён


@pytest.mark.asyncio
async def test_risk_field_mapping():
    """Маппинг полей: LinitPercent→coverage_pct, LimitAmountLeft→remaining_limit."""
    adapter = make_adapter()
    rl = await adapter.get_risks_and_limits("UNI 900001/1", PERSONAL_NUMBER)

    meds = next(r for r in rl.risks if r.risk_id == 102063)
    assert meds.coverage_pct == 80.0          # LinitPercent (опечатка API)
    assert meds.total_limit == 3000.0         # LimitAmount
    assert meds.remaining_limit == 2927.81    # LimitAmountLeft
    assert meds.sublimit == 3000.0            # лимит есть → суб-лимит для Шага 23
    assert meds.parent_risk_id is None        # RiskParentId="0" → корневой


@pytest.mark.asyncio
async def test_child_risk_and_count_limits():
    adapter = make_adapter()
    rl = await adapter.get_risks_and_limits("UNI 900001/1", PERSONAL_NUMBER)

    child = next(r for r in rl.risks if r.risk_id == 101933)
    assert child.parent_risk_id == 102063     # дочерний риск медикаментов
    assert child.sublimit is None             # LimitAmount=0 → нет своего лимита

    prophylactic = next(r for r in rl.risks if r.risk_id == 102203)
    assert prophylactic.limit_count == 2       # 2 осмотра в год
    assert prophylactic.limit_count_left == 1  # остался 1


@pytest.mark.asyncio
async def test_annual_limit_and_remaining():
    """annual_limit = Amount медицинского типа; remaining = max остатков суб-лимитов."""
    adapter = make_adapter()
    rl = await adapter.get_risks_and_limits("UNI 900001/1", PERSONAL_NUMBER)

    assert rl.annual_limit == 27000.0
    assert rl.currency == "GEL"
    # max(2927.81 медикаменты, 11920.00 госпитализация)
    assert rl.remaining == 11920.0


@pytest.mark.asyncio
async def test_policy_dates_and_object_data_extracted():
    """Шаг 23 активируется: даты полиса DD/MM/YYYY + маркер периода ожидания."""
    adapter = make_adapter()
    rl = await adapter.get_risks_and_limits("UNI 900001/1", PERSONAL_NUMBER)

    assert rl.policy_start_date == date(2026, 1, 1)
    assert rl.policy_end_date == date(2027, 1, 1)
    assert "მოცდის პერიოდი" in rl.object_data  # маркер освобождения


@pytest.mark.asyncio
async def test_exhausted_sublimits_give_zero_remaining():
    """Все денежные суб-лимиты исчерпаны → remaining=0 → manual_review в decision."""
    import copy
    response = copy.deepcopy(POLICY_RESPONSE)
    med_type = response["PolicyList"]["Policy"][0]["ObjectList"]["Objects"][
        "InsuranceTypeList"]["InsuranceType"][1]
    for risk in med_type["RiskList"]["Risk"]:
        if float(risk["LimitAmount"]) > 0:
            risk["LimitAmountLeft"] = "0.00"

    adapter = make_adapter(response)
    rl = await adapter.get_risks_and_limits("UNI 900001/1", PERSONAL_NUMBER)
    assert rl.remaining == 0.0


# ── get_contract ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contract_text_absent_in_real_response():
    """Текст договора в getpolicylist отсутствует → content='' (manual_review)."""
    adapter = make_adapter()
    contract = await adapter.get_contract("UNI 900001/1", PERSONAL_NUMBER)
    assert contract.content == ""


# ── Auth header ───────────────────────────────────────────────────


def test_auth_header_with_bearer_scheme():
    with patch("layers.core_adapter.rest_adapter.settings") as mock_settings:
        mock_settings.core_api_auth_scheme = "Bearer"
        assert LiteGroupAdapter._auth_header_value("TOK") == "Bearer TOK"


def test_auth_header_raw_token_scheme():
    """Документация показывает сырой GUID без префикса — схема настраивается."""
    with patch("layers.core_adapter.rest_adapter.settings") as mock_settings:
        mock_settings.core_api_auth_scheme = ""
        assert LiteGroupAdapter._auth_header_value("TOK") == "TOK"
