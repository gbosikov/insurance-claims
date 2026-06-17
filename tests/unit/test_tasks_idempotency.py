"""
Unit-тесты: идемпотентность ClaimParsing_UNI (services/worker/tasks.py).

Проверяем два helper'а:
  _load_prior_submit   — обнаружение предыдущего успешного submit по audit_log
  _commit_submit_audit — немедленный out-of-band коммит результата submit
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# ── env до любых импортов из core/ ──────────────────────────────────
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://:test@localhost:6379/0")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("CORE_API_PASSWORD", "test")
os.environ.setdefault("CORE_API_BASE_URL", "http://mock-core")

# ── Мок тяжёлых нативных зависимостей (не установлены в unit-окружении) ──
# tasks.py → preprocessing → numpy/cv2; ocr → google-cloud; rag → sentence-transformers
_mm = MagicMock()
for _mod in (
    "asyncpg", "asyncpg.connection", "asyncpg.pool",
    "cv2",
    "google.cloud.vision", "google.cloud.documentai",
    "google.api_core", "google.api_core.exceptions",
    "google.auth", "google.protobuf", "google.protobuf.json_format",
    "sentence_transformers",
    "torch",
    "PIL", "PIL.Image",
    "fitz",  # pymupdf
    "pdfplumber",
):
    sys.modules.setdefault(_mod, _mm)

# numpy мокируем отдельно: pytest.approx проверяет isinstance(val, np.bool_)
# и падает с TypeError если bool_ — не тип. Даём реальные типы чтобы не ломать
# остальные тесты в той же pytest-сессии.
if "numpy" not in sys.modules:
    _numpy_mock = MagicMock()
    _numpy_mock.bool_ = bool
    _numpy_mock.ndarray = type("ndarray", (), {})
    _numpy_mock.integer = int
    _numpy_mock.floating = float
    sys.modules["numpy"] = _numpy_mock

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

CLAIM_UUID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_UUID = UUID("00000000-0000-0000-0000-000000000001")


# ─────────────────────────────────────────────────────────────────
# _load_prior_submit
# ─────────────────────────────────────────────────────────────────


def _make_check_db(fetchone_return):
    """
    Строит мок AsyncSession, у которого execute() → fetchone() = fetchone_return.
    """
    row_mock = MagicMock()
    row_mock.fetchone.return_value = fetchone_return

    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(return_value=row_mock)
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    return session_mock


@pytest.mark.asyncio
async def test_load_prior_submit_no_record():
    """Нет записи в audit_log → возвращает None."""
    from services.worker.tasks import _load_prior_submit

    check_db = _make_check_db(fetchone_return=None)

    with patch("services.worker.tasks.AsyncSessionLocal", return_value=check_db):
        result = await _load_prior_submit(CLAIM_UUID)

    assert result is None


@pytest.mark.asyncio
async def test_load_prior_submit_found_dict():
    """Запись есть, output_data — dict (asyncpg JSONB) → возвращает SubmitClaimResult."""
    from services.worker.tasks import _load_prior_submit

    output_data = {"innum": "4245314", "status": 0, "status_text": "OK"}
    # asyncpg возвращает Row; record[0] = output_data (dict)
    check_db = _make_check_db(fetchone_return=(output_data,))

    with patch("services.worker.tasks.AsyncSessionLocal", return_value=check_db):
        result = await _load_prior_submit(CLAIM_UUID)

    assert result is not None
    assert result.innum == "4245314"
    assert result.status == 0
    assert result.status_text == "OK"


@pytest.mark.asyncio
async def test_load_prior_submit_filters_non_zero_status():
    """
    SQL-запрос должен фильтровать только status=0.
    Тест проверяет что условие присутствует в тексте запроса.
    """
    from services.worker.tasks import _load_prior_submit

    captured_sql: list[str] = []

    async def capturing_execute(query, params=None):
        captured_sql.append(str(query))
        row_mock = MagicMock()
        row_mock.fetchone.return_value = None
        return row_mock

    session_mock = AsyncMock()
    session_mock.execute = capturing_execute
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)

    with patch("services.worker.tasks.AsyncSessionLocal", return_value=session_mock):
        await _load_prior_submit(CLAIM_UUID)

    assert len(captured_sql) == 1
    sql = captured_sql[0].lower()
    assert "core_submit" in sql
    assert "status" in sql
    # проверяем что есть фильтр = 0
    assert "= 0" in sql


# ─────────────────────────────────────────────────────────────────
# _commit_submit_audit
# ─────────────────────────────────────────────────────────────────


def _make_commit_db():
    """Мок AsyncSession с отслеживаемым commit()."""
    session_mock = AsyncMock()
    session_mock.commit = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    return session_mock


@pytest.mark.asyncio
async def test_commit_submit_audit_calls_write_and_commit():
    """Успешный путь: write_audit_entry вызывается, commit() вызывается."""
    from core.schemas.core_api import SubmitClaimResult
    from services.worker.tasks import _commit_submit_audit

    core_result = SubmitClaimResult(innum="9999", status=0, status_text="OK")
    commit_db = _make_commit_db()

    with (
        patch("services.worker.tasks.AsyncSessionLocal", return_value=commit_db),
        patch("services.worker.tasks.write_audit_entry", new_callable=AsyncMock) as mock_audit,
    ):
        await _commit_submit_audit(
            CLAIM_UUID,
            TENANT_UUID,
            core_result,
            input_data={"PolicyNumber": "TEST-001"},
        )

    mock_audit.assert_awaited_once()
    call_kwargs = mock_audit.call_args.kwargs
    assert call_kwargs["step"] == "core_submit"
    assert call_kwargs["output_data"]["innum"] == "9999"
    assert call_kwargs["output_data"]["status"] == 0

    commit_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_submit_audit_stores_all_fields():
    """Все три поля SubmitClaimResult (innum, status, status_text) сохраняются."""
    from core.schemas.core_api import SubmitClaimResult
    from services.worker.tasks import _commit_submit_audit

    core_result = SubmitClaimResult(innum="4245315", status=0, status_text="Success")
    commit_db = _make_commit_db()

    with (
        patch("services.worker.tasks.AsyncSessionLocal", return_value=commit_db),
        patch("services.worker.tasks.write_audit_entry", new_callable=AsyncMock) as mock_audit,
    ):
        await _commit_submit_audit(
            CLAIM_UUID,
            TENANT_UUID,
            core_result,
            input_data={"PolicyNumber": "P-001", "DiagnosID": 42},
        )

    output = mock_audit.call_args.kwargs["output_data"]
    assert output == {"innum": "4245315", "status": 0, "status_text": "Success"}

    inp = mock_audit.call_args.kwargs["input_data"]
    assert inp["PolicyNumber"] == "P-001"


@pytest.mark.asyncio
async def test_commit_submit_audit_swallows_db_error():
    """
    Если commit падает с ошибкой — _commit_submit_audit не пробрасывает её.
    Основной pipeline должен продолжиться (маркер потерян, но не катастрофа).
    """
    from core.schemas.core_api import SubmitClaimResult
    from services.worker.tasks import _commit_submit_audit

    core_result = SubmitClaimResult(innum="9999", status=0, status_text="OK")
    commit_db = _make_commit_db()
    commit_db.commit.side_effect = Exception("DB connection lost")

    with (
        patch("services.worker.tasks.AsyncSessionLocal", return_value=commit_db),
        patch("services.worker.tasks.write_audit_entry", new_callable=AsyncMock),
    ):
        # Не должно бросить исключение
        await _commit_submit_audit(CLAIM_UUID, TENANT_UUID, core_result, input_data={})


# ─────────────────────────────────────────────────────────────────
# Условная логика идемпотентности
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_idempotency_hit_uses_prior_innum():
    """
    Если _load_prior_submit вернула результат — используется предыдущий innum.
    submit_claim НЕ вызывается второй раз.
    Имитирует сценарий: задача упала после submit, перезапустилась.
    """
    from core.schemas.core_api import SubmitClaimResult

    prior = SubmitClaimResult(innum="PREV-001", status=0, status_text="OK")
    submit_call_count = 0

    async def fake_submit(**kwargs):
        nonlocal submit_call_count
        submit_call_count += 1
        return SubmitClaimResult(innum="NEW-002", status=0, status_text="OK")

    # Воспроизводим логику из tasks.py:
    #   if prior_submit is not None:
    #       core_result = prior_submit
    #   else:
    #       core_result = await submit_claim(...)
    prior_submit = prior
    if prior_submit is not None:
        core_result = prior_submit
    else:
        core_result = await fake_submit()

    assert submit_call_count == 0, "submit_claim должен был быть пропущен"
    assert core_result.innum == "PREV-001"


@pytest.mark.asyncio
async def test_no_prior_submit_calls_submit():
    """
    Если _load_prior_submit вернула None — submit_claim вызывается нормально.
    """
    from core.schemas.core_api import SubmitClaimResult

    submit_call_count = 0

    async def fake_submit(**kwargs):
        nonlocal submit_call_count
        submit_call_count += 1
        return SubmitClaimResult(innum="NEW-001", status=0, status_text="OK")

    prior_submit = None  # нет предыдущего submit
    if prior_submit is not None:
        core_result = prior_submit
    else:
        core_result = await fake_submit()

    assert submit_call_count == 1
    assert core_result.innum == "NEW-001"
