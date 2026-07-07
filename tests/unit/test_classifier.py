"""Unit тесты: layers/extraction/classifier.py — regex-классификатор (rule-based путь)."""

from core.models.claim import DocType
from layers.extraction.classifier import MIN_MATCHES, classify_by_ocr_text


def test_classify_form_100_by_content():
    text = "НАПРАВЛЕНИЕ форма №100. Диагноз: J06.9. МКБ-10. Лечащий врач: Иванов И.И."
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.FORM_100
    assert result.match_count >= MIN_MATCHES


def test_classify_receipt_by_content():
    text = "Квитанция к оплате. Итого: 128.50 GEL. Плательщик: Иванов И.И."
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.RECEIPT


def test_classify_id_document_by_content():
    text = "საქართველო. პირადი ნომერი: 01234567890. დაბადების თარიღი: 01.01.1990"
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.ID_DOCUMENT


def test_classify_discharge_summary_by_content():
    text = "Выписной эпикриз. Находился на лечении с 01.01.2026. Рекомендации при выписке: покой."
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.DISCHARGE_SUMMARY


def test_classify_lab_result_by_content():
    text = "Результаты анализов. Общий анализ крови. Референсные значения: 4.0-9.0"
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.LAB_RESULT


def test_classify_prescription_by_content():
    text = "Рецепт №12345. Назначение препарата: Амоксициллин. Принимать по 1 таблетке 2 раза в день."
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.PRESCRIPTION


def test_classify_insufficient_matches_falls_back_to_other():
    """Меньше MIN_MATCHES совпадений — DocType.OTHER, а не текущий тип
    (раньше здесь молча оставляли current_type — это была скрытая ошибка)."""
    text = "случайный нечитаемый текст без ключевых слов"
    result = classify_by_ocr_text(text, current_type=DocType.FORM_100)
    assert result.doc_type == DocType.OTHER
    assert result.changed_from == DocType.FORM_100


def test_classify_insufficient_matches_no_change_flag_if_already_other():
    text = "случайный нечитаемый текст без ключевых слов"
    result = classify_by_ocr_text(text, current_type=DocType.OTHER)
    assert result.doc_type == DocType.OTHER
    assert result.changed_from is None
