from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from utils.telegram_safety import resilient, safe_answer


@pytest.mark.asyncio
async def test_safe_answer_passes_through_normally():
    query = MagicMock()
    query.answer = AsyncMock()

    await safe_answer(query, "текст")

    query.answer.assert_awaited_once_with("текст")


@pytest.mark.asyncio
async def test_safe_answer_swallows_telegram_bad_request():
    query = MagicMock()
    query.answer = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="query is too old")
    )

    await safe_answer(query, "текст", show_alert=True)  # не должно бросить исключение

    query.answer.assert_awaited_once_with("текст", show_alert=True)


@pytest.mark.asyncio
async def test_resilient_passes_through_on_success():
    calls = []

    @resilient("ошибка")
    async def handler(query, extra):
        calls.append(extra)

    query = MagicMock()
    query.answer = AsyncMock()

    await handler(query, "аргумент")

    assert calls == ["аргумент"]
    query.answer.assert_not_called()  # успех — никакого fallback-алерта


@pytest.mark.asyncio
async def test_resilient_shows_error_message_on_unexpected_exception():
    @resilient("своя ошибка для этой роли")
    async def handler(query):
        raise OSError("Can't assign requested address")

    query = MagicMock()
    query.answer = AsyncMock()

    await handler(query)  # не должно бросить исключение наружу

    query.answer.assert_awaited_once_with("своя ошибка для этой роли", show_alert=True)


@pytest.mark.asyncio
async def test_resilient_preserves_handler_signature_for_di():
    """aiogram определяет, какие зависимости инжектить, по сигнатуре
    хендлера — functools.wraps должен сохранять её видимой."""
    import inspect

    async def original(query, sheets, settings):
        pass

    wrapped = resilient("ошибка")(original)

    assert list(inspect.signature(wrapped).parameters) == list(
        inspect.signature(original).parameters
    )
