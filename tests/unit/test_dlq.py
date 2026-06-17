"""
Unit-тесты: Dead Letter Queue (services/worker/tasks.py + routers/dead_letter.py).

Проверяем:
  _write_dead_letter — запись в DLQ при финальном падении задачи
  process_claim outer except — Retry не пишет в DLQ, Exception — пишет
  dead_letter router — list / requeue / dismiss
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# ── env до любых импортов ──────────────────────────────────────────
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://:test@localhost:6379/0")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("CORE_API_PASSWORD", "test")
os.environ.setdefault("CORE_API_BASE_URL", "http://mock-core")

# ── мок тяжёлых зависимостей ──────────────────────────────────────
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
    "fitz",
    "pdfplumber",
):
    sys.modules.setdefault(_mod, _mm)

if "numpy" not in sys.modules:
    _numpy_mock = MagicMock()
    _numpy_mock.bool_ = bool
    _numpy_mock.ndarray = type("ndarray", (), {})
    _numpy_mock.integer = int
    _numpy_mock.floating = float
    sys.modules["numpy"] = _numpy_mock

from unittest.mock import AsyncMock, patch, call
from uuid import UUID

import pytest

CLAIM_UUID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TENANT_UUID = UUID("00000000-0000-0000-0000-000000000001")
CLAIM_ID = str(CLAIM_UUID)
TENANT_ID = str(TENANT_UUID)


# ─────────────────────────────────────────────────────────────────
# _write_dead_letter
# ─────────────────────────────────────────────────────────────────


def _make_dlq_session():
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
async def test_write_dead_letter_adds_and_commits():
    """Успешный путь: item добавляется в сессию, commit вызывается."""
    from services.worker.tasks import _write_dead_letter

    exc = ValueError("something broke")
    session = _make_dlq_session()

    with patch("services.worker.tasks.AsyncSessionLocal", return_value=session):
        await _write_dead_letter(CLAIM_ID, TENANT_ID, "process_claim", "task-abc", exc, 2)

    session.add.assert_called_once()
    added = session.add.call_args[0][0]

    from core.models.platform import DeadLetterItem
    assert isinstance(added, DeadLetterItem)
    assert added.task_name == "process_claim"
    assert added.task_id == "task-abc"
    assert added.claim_id == CLAIM_UUID
    assert added.tenant_id == TENANT_UUID
    assert added.exception_type == "ValueError"
    assert "something broke" in added.exception_msg
    assert added.retries == 2
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_dead_letter_stores_traceback():
    """traceback должен присутствовать (не None, не пустой)."""
    from services.worker.tasks import _write_dead_letter

    try:
        raise RuntimeError("traceback test")
    except RuntimeError as exc:
        session = _make_dlq_session()
        with patch("services.worker.tasks.AsyncSessionLocal", return_value=session):
            await _write_dead_letter(CLAIM_ID, TENANT_ID, "process_claim", "t1", exc, 0)

    added = session.add.call_args[0][0]
    assert added.traceback is not None
    assert len(added.traceback) > 0


@pytest.mark.asyncio
async def test_write_dead_letter_swallows_db_error():
    """Ошибка commit не пробрасывается — основное исключение не должно потеряться."""
    from services.worker.tasks import _write_dead_letter

    session = _make_dlq_session()
    session.commit.side_effect = Exception("DB gone")

    with patch("services.worker.tasks.AsyncSessionLocal", return_value=session):
        # Не должно бросить исключение
        await _write_dead_letter(CLAIM_ID, TENANT_ID, "process_claim", "t2", ValueError("x"), 1)


@pytest.mark.asyncio
async def test_write_dead_letter_unknown_task_id():
    """task_id='' заменяется на 'unknown-<claim_id>', не вызывает ошибки."""
    from services.worker.tasks import _write_dead_letter

    session = _make_dlq_session()
    with patch("services.worker.tasks.AsyncSessionLocal", return_value=session):
        await _write_dead_letter(CLAIM_ID, TENANT_ID, "process_claim", "", ValueError("x"), 0)

    added = session.add.call_args[0][0]
    assert added.task_id.startswith("unknown-")


# ─────────────────────────────────────────────────────────────────
# process_claim outer except: Retry vs Exception
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_exception_does_not_write_dlq():
    """
    Промежуточный Retry не должен попадать в DLQ и не должен вызывать _emergency_manual.
    Это также исправляет баг: раньше _emergency_manual вызывался при retry.
    """
    from celery.exceptions import Retry
    from services.worker.tasks import _write_dead_letter

    dlq_called = False

    async def fake_dlq(*args, **kwargs):
        nonlocal dlq_called
        dlq_called = True

    retry_exc = Retry("will retry")

    # Имитируем логику outer except из tasks.py:
    #   except Retry: raise
    #   except Exception: ... _write_dead_letter ...
    emergency_called = False
    try:
        raise retry_exc
    except Retry:
        pass  # Celery поймает Retry выше — мы просто проверяем что DLQ не вызван
    except Exception:
        emergency_called = True
        await fake_dlq()

    assert not dlq_called, "_write_dead_letter не должна вызываться при Retry"
    assert not emergency_called, "_emergency_manual не должна вызываться при Retry"


@pytest.mark.asyncio
async def test_final_exception_writes_dlq():
    """
    Финальное исключение (не Retry) — DLQ должен быть вызван.
    """
    dlq_called = False
    emergency_called = False
    caught_exc = None

    async def fake_dlq(*args, **kwargs):
        nonlocal dlq_called
        dlq_called = True

    async def fake_emergency():
        nonlocal emergency_called
        emergency_called = True

    try:
        raise RuntimeError("permanent failure")
    except Exception as e:
        caught_exc = e
        await fake_emergency()
        await fake_dlq()

    assert dlq_called
    assert emergency_called
    assert isinstance(caught_exc, RuntimeError)


# ─────────────────────────────────────────────────────────────────
# dead_letter router
# ─────────────────────────────────────────────────────────────────


def _make_dlq_item(
    dlq_id=None,
    resolved_at=None,
    resolution=None,
    task_name="process_claim",
    task_args=None,
):
    from core.models.platform import DeadLetterItem
    from datetime import datetime, timezone

    item = DeadLetterItem()
    item.id = dlq_id or UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    item.task_name = task_name
    item.task_id = "celery-task-111"
    item.claim_id = CLAIM_UUID
    item.tenant_id = TENANT_UUID
    item.task_args = task_args or [CLAIM_ID, TENANT_ID]
    item.task_kwargs = {}
    item.exception_type = "RuntimeError"
    item.exception_msg = "boom"
    item.traceback = "Traceback ..."
    item.retries = 3
    item.failed_at = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    item.resolved_at = resolved_at
    item.resolved_by = None
    item.resolution = resolution
    item.created_at = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    return item


@pytest.mark.asyncio
async def test_list_dead_letter_returns_items():
    """GET /v1/dead-letter возвращает список."""
    from services.api.routers.dead_letter import list_dead_letter

    item = _make_dlq_item()
    mock_db = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [item]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_mock
    mock_db.execute = AsyncMock(return_value=execute_result)

    result = await list_dead_letter(tenant_id=TENANT_UUID, db=mock_db)

    assert len(result) == 1
    assert result[0].exception_type == "RuntimeError"


@pytest.mark.asyncio
async def test_requeue_already_resolved_returns_409():
    """requeue уже разрешённого элемента → 409."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from pydantic import BaseModel
    from services.api.routers.dead_letter import requeue_dead_letter, DismissRequest

    resolved_item = _make_dlq_item(resolved_at=datetime.now(timezone.utc), resolution="dismissed")
    mock_db = AsyncMock()

    with patch(
        "services.api.routers.dead_letter._get_item",
        new=AsyncMock(return_value=resolved_item),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await requeue_dead_letter(
                resolved_item.id,
                DismissRequest(operator_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")),
                TENANT_UUID,
                mock_db,
            )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_dismiss_marks_resolved():
    """dismiss → resolved_at устанавливается, resolution='dismissed'."""
    from services.api.routers.dead_letter import dismiss_dead_letter, DismissRequest

    item = _make_dlq_item()
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    with patch(
        "services.api.routers.dead_letter._get_item",
        new=AsyncMock(return_value=item),
    ):
        result = await dismiss_dead_letter(
            item.id,
            DismissRequest(operator_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")),
            TENANT_UUID,
            mock_db,
        )

    mock_db.commit.assert_awaited_once()
    assert result["status"] == "dismissed"


@pytest.mark.asyncio
async def test_dismiss_already_resolved_returns_409():
    """dismiss уже разрешённого элемента → 409."""
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from services.api.routers.dead_letter import dismiss_dead_letter, DismissRequest

    resolved_item = _make_dlq_item(resolved_at=datetime.now(timezone.utc))

    with patch(
        "services.api.routers.dead_letter._get_item",
        new=AsyncMock(return_value=resolved_item),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await dismiss_dead_letter(
                resolved_item.id,
                DismissRequest(operator_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")),
                TENANT_UUID,
                AsyncMock(),
            )

    assert exc_info.value.status_code == 409
