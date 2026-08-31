from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from handlers.fallback import (
    FALLBACK_CATEGORIES_CALLBACK_DATA,
    cmd_help,
    on_fallback_categories,
    on_unhandled_message,
)
from texts.ru import FALLBACK_CATEGORIES_BUTTON, FALLBACK_HELP_MESSAGE, HELP_MESSAGE


def _make_message() -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    return message


def _make_query(message: MagicMock) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.answer = AsyncMock()
    return query


def _make_sheets() -> MagicMock:
    sheets = MagicMock()
    sheets.get_merchants = AsyncMock(return_value=[])
    return sheets


@pytest.mark.asyncio
async def test_help_command_explains_both_roles():
    message = _make_message()

    await cmd_help(message)

    message.answer.assert_awaited_once_with(HELP_MESSAGE)


@pytest.mark.asyncio
async def test_unhandled_message_shows_help_with_categories_button():
    message = _make_message()

    await on_unhandled_message(message)

    message.answer.assert_awaited_once()
    args, kwargs = message.answer.call_args
    assert args[0] == FALLBACK_HELP_MESSAGE
    keyboard = kwargs["reply_markup"]
    button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert FALLBACK_CATEGORIES_BUTTON in button_texts


@pytest.mark.asyncio
async def test_fallback_categories_button_shows_categories():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets()

    await on_fallback_categories(query, sheets)

    message.answer.assert_awaited_once()
    query.answer.assert_awaited_once()


def test_callback_data_is_a_plain_string():
    # Простая защита от опечатки/случайного изменения — используется
    # и в хендлере, и в клавиатуре, должны совпадать по построению.
    assert FALLBACK_CATEGORIES_CALLBACK_DATA == "fallback_categories"
