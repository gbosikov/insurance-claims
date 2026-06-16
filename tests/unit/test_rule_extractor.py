"""
Unit тесты: rule_extractor.py — детерминированное извлечение из OCR-текста.

Покрывает: personal_id, ICD-10, суммы, даты, ФИО, учреждение,
срочность, confidence, интеграционный сценарий.
"""
from __future__ import annotations

from uuid import UUID, uuid4
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.models.claim import DocType
from layers.ocr.service import OCRResult
from layers.extraction.rule_extractor import (
    _extract_personal_id,
    _extract_birth_date,
    _extract_full_name,
    _extract_event_date,
    _extract_institution,
    _extract_icd10_codes,
    _extract_line_items,
    _extract_total,
    _detect_urgency,
    _compute_confidence,
    _build_cross_document,
    extract_by_rules,
)


# ── Фабрика OCRResult ─────────────────────────────────────────────

def make_ocr(
    text: str,
    doc_type: DocType = DocType.FORM_100,
    doc_id: UUID | None = None,
    avg_confidence: float = 0.95,
) -> OCRResult:
    return OCRResult(
        doc_id=doc_id or uuid4(),
        doc_type=doc_type,
        full_text=text,
        avg_confidence=avg_confidence,
    )


# ── Группа 1: personal_id ─────────────────────────────────────────

def test_personal_id_11digits_without_context():
    ocr = make_ocr("Пациент: Иванов И.И.   01234567890   дата: 12.01.2026")
    value, conf = _extract_personal_id([ocr])
    assert value == "01234567890"
    assert conf == pytest.approx(0.95)


def test_personal_id_11digits_with_context_label():
    ocr = make_ocr("პირადი ნომერი: 01234567890\nგვარი: ივანოვი", doc_type=DocType.ID_DOCUMENT)
    value, conf = _extract_personal_id([ocr])
    assert value == "01234567890"
    assert conf == pytest.approx(0.98)


def test_personal_id_russian_label():
    ocr = make_ocr("Личный номер: 12345678901\nФИО: Иванов")
    value, conf = _extract_personal_id([ocr])
    assert value == "12345678901"
    assert conf == pytest.approx(0.98)


def test_personal_id_12digits_not_extracted():
    # 12 цифр подряд — телефон, не личный номер
    ocr = make_ocr("Тел: 995591234567 обратитесь по номеру")
    value, conf = _extract_personal_id([ocr])
    assert value is None


def test_personal_id_prefers_id_document():
    form = make_ocr("Форма 100 номер: 11111111111", doc_type=DocType.FORM_100)
    id_doc = make_ocr("პირადი ნომერი: 22222222222", doc_type=DocType.ID_DOCUMENT)
    value, conf = _extract_personal_id([form, id_doc])
    # ID-документ приоритетнее формы 100
    assert value == "22222222222"
    assert conf == pytest.approx(0.98)


def test_personal_id_none_when_absent():
    ocr = make_ocr("Нет номера в этом тексте. Обычный документ.")
    value, conf = _extract_personal_id([ocr])
    assert value is None
    assert conf == pytest.approx(0.0)


# ── Группа 2: ICD-10 коды ─────────────────────────────────────────

def test_icd10_standard_code():
    # "Диагноз:" — диагностический контекст → Уровень 1 → conf=0.98
    ocr = make_ocr("Диагноз: J06.9 Острая инфекция ВДП")
    codes = _extract_icd10_codes([ocr])
    assert len(codes) >= 1
    assert codes[0][0] == "J06.9"
    assert codes[0][1] == pytest.approx(0.98)


def test_icd10_code_without_decimal():
    ocr = make_ocr("МКБ: I10 артериальная гипертензия")
    codes = _extract_icd10_codes([ocr])
    assert any(c[0] == "I10" for c in codes)


def test_icd10_lowercase_normalized():
    ocr = make_ocr("diagnosis: i10")
    codes = _extract_icd10_codes([ocr])
    assert any(c[0] == "I10" for c in codes)


def test_icd10_gel_not_extracted():
    # GEL — валюта, не МКБ-10
    ocr = make_ocr("Сумма: 150 GEL итого к оплате")
    codes = _extract_icd10_codes([ocr])
    assert not any(c[0] == "GEL" for c in codes)


def test_icd10_exclusions_not_extracted():
    # OK, ID, RU, KA — частые аббревиатуры, не МКБ коды
    ocr = make_ocr("Status: OK. ID: 123. Lang: RU, KA, EN")
    codes = _extract_icd10_codes([ocr])
    assert not any(c[0] in {"OK", "ID", "RU", "KA", "EN"} for c in codes)


def test_icd10_multiple_codes():
    ocr = make_ocr("Диагноз 1: J06.9\nДиагноз 2: Z00.0\nДиагноз 3: K29.7")
    codes = _extract_icd10_codes([ocr])
    code_values = [c[0] for c in codes]
    assert "J06.9" in code_values
    assert "Z00.0" in code_values
    assert "K29.7" in code_values


def test_icd10_deduplication():
    # Один и тот же код в разных документах — только один раз
    form = make_ocr("Диагноз: J06.9", doc_type=DocType.FORM_100)
    receipt = make_ocr("Диагноз: J06.9 ОРВИ", doc_type=DocType.RECEIPT)
    codes = _extract_icd10_codes([form, receipt])
    assert sum(1 for c in codes if c[0] == "J06.9") == 1


# ── Группа 2б: контекстная привязка ICD-10 (Улучшение #1) ────────

def test_icd10_in_context_gets_high_confidence():
    # Код после диагностической метки → Уровень 1 → conf=0.98
    for label in ["Диагноз:", "МКБ-10:", "ICD-10:", "diagnosis:", "დიაგნოზი:", "კოდი:"]:
        ocr = make_ocr(f"{label} J06.9 описание")
        codes = _extract_icd10_codes([ocr])
        matched = [c for c in codes if c[0] == "J06.9"]
        assert matched, f"J06.9 не найден для метки '{label}'"
        assert matched[0][1] == pytest.approx(0.98), (
            f"Ожидался conf=0.98 для метки '{label}', получен {matched[0][1]}"
        )


def test_icd10_outside_context_gets_lower_confidence():
    # Код вне любой диагностической метки → Уровень 2 → conf=0.90 (есть точка)
    ocr = make_ocr("Пациент обратился. J06.9 подтверждён. Выписан домой.")
    codes = _extract_icd10_codes([ocr])
    matched = [c for c in codes if c[0] == "J06.9"]
    assert matched, "J06.9 должен быть найден даже вне контекста"
    assert matched[0][1] == pytest.approx(0.90), (
        f"Ожидался conf=0.90 (Уровень 2), получен {matched[0][1]}"
    )


def test_icd10_false_positive_short_code_low_confidence():
    # "A4" — формат бумаги, не МКБ-10 код. Вне контекста → conf=0.65
    # Downstream validation (Улучшение #3) отфильтрует по БД.
    ocr = make_ocr("Документ распечатан на бумаге формата A4 в двух экземплярах.")
    codes = _extract_icd10_codes([ocr])
    matched = [c for c in codes if c[0] == "A4"]
    if matched:
        # Если всё же попал — conf должен быть низким (Уровень 2, len<3)
        assert matched[0][1] == pytest.approx(0.65), (
            f"Ложный позитив A4 должен иметь conf=0.65, получен {matched[0][1]}"
        )


def test_icd10_context_wins_over_fullscan():
    # Один и тот же код встречается и в контексте, и в произвольном тексте.
    # Уровень 1 должен победить — conf=0.98, не перезаписывается Уровнем 2.
    ocr = make_ocr(
        "Пациент направлен. Наблюдался случай J06.9 ранее.\n"
        "Диагноз: J06.9 Острая инфекция верхних дыхательных путей"
    )
    codes = _extract_icd10_codes([ocr])
    matched = [c for c in codes if c[0] == "J06.9"]
    assert len(matched) == 1, "Дедупликация: только одна запись для J06.9"
    assert matched[0][1] == pytest.approx(0.98), (
        "Контекстный conf=0.98 должен победить над Уровнем 2"
    )


# ── Группа 3: суммы ───────────────────────────────────────────────

def test_total_with_georgian_keyword():
    receipt = make_ocr("კონსულტაცია   80 GEL\nსულ: 150.00 GEL", doc_type=DocType.RECEIPT)
    total, conf = _extract_total([receipt], [])
    assert total == pytest.approx(150.0)
    assert conf == pytest.approx(0.95)


def test_total_with_russian_keyword():
    receipt = make_ocr("Консультация 100 GEL\nИтого: 100 GEL", doc_type=DocType.RECEIPT)
    total, conf = _extract_total([receipt], [])
    assert total == pytest.approx(100.0)
    assert conf >= 0.90


def test_total_decimal_comma_normalized():
    receipt = make_ocr("სულ: 150,50 ₾", doc_type=DocType.RECEIPT)
    total, conf = _extract_total([receipt], [])
    assert total == pytest.approx(150.5)


def test_total_fallback_sum_of_line_items():
    from core.schemas.claim import LineItem
    items = [LineItem(description="А", amount=100.0), LineItem(description="Б", amount=50.0)]
    # Нет ключевого слова итого — сумма из строк
    receipt = make_ocr("А 100 GEL\nБ 50 GEL", doc_type=DocType.RECEIPT)
    total, conf = _extract_total([receipt], items)
    assert total == pytest.approx(150.0)


def test_line_items_from_receipt():
    receipt = make_ocr(
        "კონსულტაცია       80 GEL\n"
        "ანალიზი სისხლის   45.50 GEL\n"
        "სულ:              125.50 GEL",
        doc_type=DocType.RECEIPT,
    )
    items = _extract_line_items([receipt])
    amounts = [i.amount for i in items]
    assert 80.0 in amounts
    assert 45.5 in amounts
    # total-строка не должна быть в items
    assert 125.5 not in amounts


# ── Группа 3б: двухстрочное окно line_items (Улучшение #2) ───────

def test_line_items_amount_on_next_line():
    # OCR разбил запись: название на i, сумма на i+1
    receipt = make_ocr(
        "კონსულტაცია პირველადი\n"
        "45.00 GEL\n"
        "სულ: 45.00 GEL",
        doc_type=DocType.RECEIPT,
    )
    items = _extract_line_items([receipt])
    assert len(items) == 1
    assert items[0].amount == pytest.approx(45.0)
    assert "კონსულტაცია" in items[0].description


def test_line_items_numeric_junk_description_uses_prev_line():
    # Строка "1   80 GEL" — "1" это количество, не название услуги
    receipt = make_ocr(
        "ანალიზი სისხლის\n"
        "1   80.00 GEL\n"
        "სულ: 80.00 GEL",
        doc_type=DocType.RECEIPT,
    )
    items = _extract_line_items([receipt])
    assert len(items) == 1
    assert items[0].amount == pytest.approx(80.0)
    assert "ანალიზი" in items[0].description


def test_line_items_multiple_two_line_items():
    # Несколько позиций в формате «название / сумма»
    receipt = make_ocr(
        "კონსულტაცია\n"
        "80.00 GEL\n"
        "სისხლის ანალიზი\n"
        "45.00 GEL\n"
        "სულ: 125.00 GEL",
        doc_type=DocType.RECEIPT,
    )
    items = _extract_line_items([receipt])
    amounts = {i.amount for i in items}
    descs = " ".join(i.description for i in items)
    assert 80.0 in amounts
    assert 45.0 in amounts
    assert "კონსულტაცია" in descs
    assert "სისხლის" in descs


def test_line_items_prev_line_not_reused_for_two_amounts():
    # Одно название → две суммы: вторая сумма не должна получить то же описание
    receipt = make_ocr(
        "კონსულტაცია\n"
        "80.00 GEL\n"
        "90.00 GEL\n"
        "სულ: 170.00 GEL",
        doc_type=DocType.RECEIPT,
    )
    items = _extract_line_items([receipt])
    # "კონსულტაცია" не должна быть использована дважды
    konsult_items = [i for i in items if "კონსულტაცია" in i.description]
    assert len(konsult_items) == 1


def test_line_items_amount_alone_without_any_prev_skipped():
    # Сумма в самом начале чека — нет строки выше для описания
    receipt = make_ocr(
        "45.00 GEL\n"
        "სულ: 45.00 GEL",
        doc_type=DocType.RECEIPT,
    )
    items = _extract_line_items([receipt])
    # Без описания — пропускаем
    assert len(items) == 0


# ── Группа 4: даты ────────────────────────────────────────────────

def test_event_date_dmy_format():
    ocr = make_ocr("Дата обращения: 15.01.2026\nДиагноз: J06.9")
    value, conf = _extract_event_date([ocr])
    assert value == "2026-01-15"
    assert conf >= 0.88


def test_event_date_iso_format():
    ocr = make_ocr("Date: 2026-01-15 Clinic visit")
    value, conf = _extract_event_date([ocr])
    assert value == "2026-01-15"
    assert conf >= 0.90


def test_birth_date_from_id_document():
    id_doc = make_ocr(
        "სახელი: ნინო\nდაბადების თარიღი: 15.03.1990\nპირადი ნომერი: 01234567890",
        doc_type=DocType.ID_DOCUMENT,
    )
    value, conf = _extract_birth_date([id_doc])
    assert value == "1990-03-15"
    assert conf >= 0.90


def test_birth_date_russian_label():
    id_doc = make_ocr("Дата рождения: 20.05.1985", doc_type=DocType.ID_DOCUMENT)
    value, conf = _extract_birth_date([id_doc])
    assert value == "1985-05-20"


def test_invalid_date_not_extracted():
    ocr = make_ocr("Дата: 32.13.2026 — ошибочная дата")
    value, conf = _extract_event_date([ocr])
    # Невалидная дата (32-й день, 13-й месяц) не должна быть извлечена
    assert value != "2026-13-32"


def test_event_date_excludes_birth_date_context():
    id_doc = make_ocr(
        "Дата рождения: 15.03.1985\nДата обращения: 12.01.2026",
        doc_type=DocType.ID_DOCUMENT,
    )
    # event_date должна быть 2026, не 1985
    value, conf = _extract_event_date([id_doc])
    assert value == "2026-01-12"


# ── Группа 5: ФИО ─────────────────────────────────────────────────

def test_full_name_from_fio_label():
    ocr = make_ocr("ФИО: Иванов Иван Иванович\nДата: 12.01.2026")
    name, conf = _extract_full_name([ocr])
    assert name is not None
    assert "Иванов" in name
    assert conf >= 0.80


def test_full_name_from_georgian_label():
    ocr = make_ocr("სახელი/გვარი: ნინო ჩიქოვანი\nდაბადების: 01.01.1990", doc_type=DocType.ID_DOCUMENT)
    name, conf = _extract_full_name([ocr])
    assert name is not None
    assert conf >= 0.80


def test_full_name_fallback_georgian_words():
    # Нет метки, но есть грузинские слова в заголовке ID
    ocr = make_ocr("ნინო ჩიქოვანი\n01234567890\n01.01.1990", doc_type=DocType.ID_DOCUMENT)
    name, conf = _extract_full_name([ocr])
    assert name is not None
    assert conf >= 0.55  # fallback — низкий confidence


def test_full_name_none_when_absent():
    ocr = make_ocr("15.01.2026 Консультация 100 GEL J06.9", doc_type=DocType.RECEIPT)
    name, conf = _extract_full_name([ocr])
    # Из чека без метки ФИО не извлекается надёжно
    assert conf < 0.70 or name is None


# ── Группа 6: учреждение ──────────────────────────────────────────

def test_institution_from_georgian_keyword():
    ocr = make_ocr("კლინიკა: ავრორა\nდათ: 15.01.2026", doc_type=DocType.FORM_100)
    institution = _extract_institution([ocr])
    assert institution is not None
    assert "ავრორა" in institution or "aurora" in institution.lower()


def test_institution_from_header_lines():
    # Первые строки формы 100 — часто шапка с названием клиники
    ocr = make_ocr(
        "AvroRa Medical Center\nთბილისი, ჭავჭავაძის 12\n\nპაციენტი: ნინო",
        doc_type=DocType.FORM_100,
    )
    institution = _extract_institution([ocr])
    assert institution is not None


def test_institution_none_when_absent():
    ocr = make_ocr("J06.9\n01234567890\n150 GEL", doc_type=DocType.RECEIPT)
    institution = _extract_institution([ocr])
    # Может быть None если нет явных признаков
    # Тест просто проверяет что функция не выбрасывает исключение
    assert institution is None or isinstance(institution, str)


# ── Группа 7: срочность ───────────────────────────────────────────

def test_urgency_georgian_urgent():
    ocr = make_ocr("გადაუდებელი დახმარება გაიწია პაციენტს")
    assert _detect_urgency([ocr]) == "urgent"


def test_urgency_russian_urgent():
    ocr = make_ocr("Срочная помощь. Пациент поступил в тяжёлом состоянии.")
    assert _detect_urgency([ocr]) == "urgent"


def test_urgency_diagnostic():
    ocr = make_ocr("Направление на диагностику. Исследование крови.")
    assert _detect_urgency([ocr]) == "diagnostic"


def test_urgency_georgian_diagnostic():
    ocr = make_ocr("პირველადი დიაგნოსტიკა და სკრინინგი")
    assert _detect_urgency([ocr]) == "diagnostic"


def test_urgency_planned_default():
    ocr = make_ocr("Консультация врача. Диагноз: J06.9. Лечение назначено.")
    # Нет явных маркеров → None (неизвестно, не предполагаем)
    result = _detect_urgency([ocr])
    assert result in (None, "planned")


def test_urgency_none_when_no_markers():
    ocr = make_ocr("Дата: 15.01.2026\nСумма: 100 GEL")
    assert _detect_urgency([ocr]) is None


# ── Группа 8: вычисление confidence ──────────────────────────────

def test_confidence_all_fields_present():
    fields = {
        "personal_id": ("01234567890", 0.98),
        "birth_date":  ("1985-01-01", 0.92),
        "full_name":   ("Иванов И.", 0.85),
        "event_date":  ("2026-01-15", 0.92),
        "institution": ("Клиника Аврора", 0.80),
        "diagnoses":   [("J06.9", 0.95)],
        "total":       (150.0, 0.95),
    }
    confidence, flags = _compute_confidence(fields)
    assert confidence >= 0.85
    assert len(flags) == 0


def test_confidence_penalty_missing_personal_id():
    fields = {
        "personal_id": (None, 0.0),
        "birth_date":  ("1985-01-01", 0.92),
        "full_name":   ("Иванов И.", 0.85),
        "event_date":  ("2026-01-15", 0.92),
        "institution": (None, 0.0),
        "diagnoses":   [("J06.9", 0.95)],
        "total":       (150.0, 0.95),
    }
    confidence, flags = _compute_confidence(fields)
    assert confidence < 0.80
    assert "missing_personal_id" in flags


def test_confidence_penalty_missing_diagnosis():
    fields = {
        "personal_id": ("01234567890", 0.95),
        "birth_date":  ("1985-01-01", 0.92),
        "full_name":   ("Иванов И.", 0.85),
        "event_date":  ("2026-01-15", 0.92),
        "institution": (None, 0.0),
        "diagnoses":   [],
        "total":       (150.0, 0.95),
    }
    confidence, flags = _compute_confidence(fields)
    assert confidence < 0.85
    assert "missing_diagnosis" in flags


def test_confidence_penalty_missing_event_date():
    fields = {
        "personal_id": ("01234567890", 0.95),
        "birth_date":  (None, 0.0),
        "full_name":   ("Иванов И.", 0.85),
        "event_date":  (None, 0.0),
        "institution": (None, 0.0),
        "diagnoses":   [("J06.9", 0.95)],
        "total":       (150.0, 0.95),
    }
    confidence, flags = _compute_confidence(fields)
    assert confidence < 0.80
    assert "missing_date" in flags


def test_confidence_low_name_confidence_adds_flag():
    fields = {
        "personal_id": ("01234567890", 0.95),
        "birth_date":  ("1985-01-01", 0.92),
        "full_name":   ("ნინო", 0.60),   # низкий confidence имени
        "event_date":  ("2026-01-15", 0.92),
        "institution": ("Клиника", 0.80),
        "diagnoses":   [("J06.9", 0.95)],
        "total":       (150.0, 0.95),
    }
    confidence, flags = _compute_confidence(fields)
    assert "low_confidence_name" in flags


# ── Группа 9: интеграционный тест ────────────────────────────────

FORM_100_TEXT = """
ავრორა სამედიცინო ცენტრი
თბილისი, ჭავჭავაძის 12

სახელი/გვარი: ნინო ჩიქოვანი
პირადი ნომერი: 01234567890
დაბადების თარიღი: 15.03.1990

მომსახურების თარიღი: 12.01.2026
დიაგნოზი: J06.9 მწვავე სასუნთქი ინფექცია
კლინიკა: ავრორა

მომსახურება:
კონსულტაცია

სულ: 80.00 GEL
"""

ID_DOC_TEXT = """
საქართველოს მოქალაქის პირადობის მოწმობა
სახელი: ნინო
გვარი: ჩიქოვანი
პირადი ნომერი: 01234567890
დაბადების თარიღი: 15.03.1990
"""

RECEIPT_TEXT = """
ავრორა სამედიცინო ცენტრი
12.01.2026

კონსულტაცია              80.00 GEL

სულ: 80.00 GEL
"""


def _make_icd10_db_mock(found: bool, name_r: str = "Острая инфекция верхних дыхательных путей") -> AsyncMock:
    """
    Фабрика мока БД для _extract_diagnoses.

    found=True  → точное совпадение (enrich_diagnosis CTE возвращает строку с name_r).
    found=False → точное совпадение не найдено (CTE пуст); prefix-поиск тоже пуст.
    """
    if found:
        mock_row = MagicMock()
        mock_row.name_r = name_r
        mock_row.name_g = None
        mock_row.name_e = None
        mock_row.id = 1234
        mock_row.pid = 100
        mock_row.extcod = "J06.9"
        mock_row.depth = 0
        cte_result = MagicMock()
        cte_result.fetchall = MagicMock(return_value=[mock_row])
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=cte_result)
    else:
        cte_result = MagicMock()
        cte_result.fetchall = MagicMock(return_value=[])
        prefix_result = MagicMock()
        prefix_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=[cte_result, prefix_result])
    return mock_db


@pytest.mark.asyncio
async def test_extract_by_rules_full_claim():
    """Интеграционный тест: полный пакет документов, J06.9 найден в БД."""
    form = make_ocr(FORM_100_TEXT, doc_type=DocType.FORM_100)
    id_doc = make_ocr(ID_DOC_TEXT, doc_type=DocType.ID_DOCUMENT)
    receipt = make_ocr(RECEIPT_TEXT, doc_type=DocType.RECEIPT)

    mock_db = _make_icd10_db_mock(found=True)

    claim_id = uuid4()
    result = await extract_by_rules([form, id_doc, receipt], claim_id, mock_db)

    assert result.insured.personal_id == "01234567890"
    assert "2026-01-12" in result.event.date or result.event.date == "2026-01-12"
    assert result.event.total_claimed == pytest.approx(80.0)
    assert any(d.icd10_code == "J06.9" for d in result.event.diagnoses)
    assert result.extraction_confidence > 0.0
    assert result.cross_document is not None


@pytest.mark.asyncio
async def test_extract_by_rules_returns_extraction_result_type():
    """Проверяет тип возвращаемого значения."""
    from core.schemas.claim import ExtractionResult
    form = make_ocr(FORM_100_TEXT, doc_type=DocType.FORM_100)
    mock_db = _make_icd10_db_mock(found=True)
    result = await extract_by_rules([form], uuid4(), mock_db)
    assert isinstance(result, ExtractionResult)


@pytest.mark.asyncio
async def test_extract_by_rules_empty_ocr():
    """Пустой набор документов не вызывает исключения."""
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=MagicMock(fetchall=MagicMock(return_value=[])))
    result = await extract_by_rules([], uuid4(), mock_db)
    # Должен вернуть результат с низким confidence и флагами
    assert result.extraction_confidence < 0.60
    assert len(result.flags) > 0


# ──────────────────────────────────────────────────────────────────────────────
# Группа 2в: валидация ICD10 против локальной БД (#3)
# ──────────────────────────────────────────────────────────────────────────────

from layers.extraction.rule_extractor import _extract_diagnoses  # noqa: E402


@pytest.mark.asyncio
async def test_icd10_db_exact_match_included():
    """Код найден через enrich_diagnosis (точное совпадение) — включается в результат."""
    mock_row = MagicMock()
    mock_row.name_r = "Острая инфекция верхних дыхательных путей"
    mock_row.name_g = None
    mock_row.name_e = None
    mock_row.id = 1
    mock_row.pid = 0
    mock_row.extcod = "J06.9"
    mock_row.depth = 0

    cte_result = MagicMock()
    cte_result.fetchall = MagicMock(return_value=[mock_row])

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=cte_result)

    ocr = make_ocr("Диагноз: J06.9", doc_type=DocType.FORM_100)
    diagnoses, flags = await _extract_diagnoses([ocr], mock_db)

    assert len(diagnoses) == 1
    assert diagnoses[0].icd10_code == "J06.9"
    assert "Острая инфекция" in diagnoses[0].description
    assert not any("icd10_not_in_db" in f for f in flags)
    assert "no_valid_icd10_codes" not in flags


@pytest.mark.asyncio
async def test_icd10_db_prefix_fallback_included():
    """Точного кода нет, но prefix (J06%) есть — код включается через prefix-поиск."""
    cte_result = MagicMock()
    cte_result.fetchall = MagicMock(return_value=[])  # exact not found

    prefix_row = MagicMock()
    prefix_row.name_r = "Острые инфекции верхних дыхательных путей"
    prefix_row.name_e = None
    prefix_row.name_g = None
    prefix_result = MagicMock()
    prefix_result.scalar_one_or_none = MagicMock(return_value=prefix_row)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[cte_result, prefix_result])

    ocr = make_ocr("Диагноз: J06.9", doc_type=DocType.FORM_100)
    diagnoses, flags = await _extract_diagnoses([ocr], mock_db)

    assert len(diagnoses) == 1
    assert diagnoses[0].icd10_code == "J06.9"
    assert "не найден" not in flags
    assert "no_valid_icd10_codes" not in flags
    assert not any("icd10_not_in_db" in f for f in flags)


@pytest.mark.asyncio
async def test_icd10_db_not_found_produces_flag():
    """Код не найден ни точно ни через prefix → флаг icd10_not_in_db:{code}."""
    cte_result = MagicMock()
    cte_result.fetchall = MagicMock(return_value=[])

    prefix_result = MagicMock()
    prefix_result.scalar_one_or_none = MagicMock(return_value=None)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[cte_result, prefix_result])

    ocr = make_ocr("Диагноз: G1", doc_type=DocType.FORM_100)
    diagnoses, flags = await _extract_diagnoses([ocr], mock_db)

    assert diagnoses == []
    assert any(f == "icd10_not_in_db:G1" for f in flags)
    assert "no_valid_icd10_codes" in flags


@pytest.mark.asyncio
async def test_icd10_db_mixed_valid_and_invalid():
    """Один код в БД есть, другой нет — валидный проходит, невалидный → флаг."""
    # J06.9 найден точно
    valid_row = MagicMock()
    valid_row.name_r = "Острая инфекция"
    valid_row.name_g = None
    valid_row.name_e = None
    valid_row.id = 1
    valid_row.pid = 0
    valid_row.extcod = "J06.9"
    valid_row.depth = 0

    cte_valid = MagicMock()
    cte_valid.fetchall = MagicMock(return_value=[valid_row])

    # X99.9 — точного нет
    cte_invalid = MagicMock()
    cte_invalid.fetchall = MagicMock(return_value=[])
    # prefix тоже нет
    prefix_none = MagicMock()
    prefix_none.scalar_one_or_none = MagicMock(return_value=None)

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=[cte_valid, cte_invalid, prefix_none])

    # Оба кода в тексте с явным диагностическим контекстом
    ocr = make_ocr("Диагноз: J06.9\nДиагноз 2: X99.9", doc_type=DocType.FORM_100)
    diagnoses, flags = await _extract_diagnoses([ocr], mock_db)

    codes = [d.icd10_code for d in diagnoses]
    assert "J06.9" in codes
    assert "X99.9" not in codes
    assert any(f == "icd10_not_in_db:X99.9" for f in flags)
    assert "no_valid_icd10_codes" not in flags  # J06.9 прошёл
