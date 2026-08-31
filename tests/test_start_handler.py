from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import Settings
from handlers.start import (
    OFFER_ACCEPTED_KEY,
    cmd_start,
    on_offer_accept,
    on_offer_back,
    on_offer_full_text,
)
from texts.ru import OFFER_FULL_TEXT, OFFER_TEXT, START_GREETING


def _make_message() -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=123456789)
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    return message


def _make_query(message: MagicMock) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = MagicMock(id=123456789)
    query.answer = AsyncMock()
    return query


def _make_sheets() -> MagicMock:
    sheets = MagicMock()
    sheets.append_event = AsyncMock()
    sheets.get_merchants = AsyncMock(return_value=[])
    return sheets


def _make_state(initial_data: dict | None = None) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=dict(initial_data or {}))
    state.update_data = AsyncMock()
    return state


# ------------------------------------------------------------------ #
# cmd_start — ВСЕГДА клиентский флоу, независимо от роли (Day 5 hotfix,
# живой фидбэк: курьер тоже может просто хотеть заказать что-то, /start
# не должен решать это за него — роутинг по роли живёт только в
# handlers/courier.py, на /shift_on, не здесь)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_start_sends_text_greeting_without_banner(valid_settings_kwargs):
    kwargs = dict(valid_settings_kwargs)
    kwargs["WELCOME_IMAGE_URL"] = None
    settings = Settings(**kwargs)
    message = _make_message()
    sheets = _make_sheets()
    state = _make_state()

    await cmd_start(message, settings, sheets, state)

    message.answer.assert_any_call(START_GREETING)
    message.answer_photo.assert_not_called()


@pytest.mark.asyncio
async def test_start_sends_photo_with_caption_when_banner_configured(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)  # WELCOME_IMAGE_URL задан в фикстуре
    message = _make_message()
    sheets = _make_sheets()
    state = _make_state()

    await cmd_start(message, settings, sheets, state)

    message.answer_photo.assert_awaited_once_with(
        photo=settings.WELCOME_IMAGE_URL, caption=START_GREETING
    )


@pytest.mark.asyncio
async def test_start_does_not_raise_when_user_is_missing(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)
    message = _make_message()
    message.from_user = None
    sheets = _make_sheets()
    state = _make_state()

    await cmd_start(message, settings, sheets, state)

    message.answer_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_logs_bot_start_event(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)
    message = _make_message()
    sheets = _make_sheets()
    state = _make_state()

    await cmd_start(message, settings, sheets, state)

    sheets.append_event.assert_awaited_once()
    args, kwargs = sheets.append_event.call_args
    assert kwargs.get("event_type") == "bot_start" or "bot_start" in args


@pytest.mark.asyncio
async def test_start_shows_offer_when_not_yet_accepted(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)
    message = _make_message()
    sheets = _make_sheets()
    state = _make_state()  # offer_accepted отсутствует

    await cmd_start(message, settings, sheets, state)

    # Последний answer() — именно текст оферты (после приветствия)
    last_args, last_kwargs = message.answer.call_args
    assert last_args[0] == OFFER_TEXT
    assert "reply_markup" in last_kwargs


@pytest.mark.asyncio
async def test_start_skips_offer_and_shows_categories_when_already_accepted(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)
    message = _make_message()
    sheets = _make_sheets()
    state = _make_state({OFFER_ACCEPTED_KEY: True})

    with patch("handlers.start.show_categories", new=AsyncMock()) as show_categories_mock:
        await cmd_start(message, settings, sheets, state)

    show_categories_mock.assert_awaited_once_with(message, sheets)
    # Оферта не должна показываться повторно
    for call in message.answer.call_args_list:
        assert call.args[0] != OFFER_TEXT


# ------------------------------------------------------------------ #
# on_offer_accept
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_offer_accept_first_time_sets_flag_and_logs_event():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets()
    state = _make_state()  # ещё не принята

    with patch("handlers.start.show_categories", new=AsyncMock()) as show_categories_mock:
        await on_offer_accept(query, state, sheets)

    state.update_data.assert_awaited_once_with(**{OFFER_ACCEPTED_KEY: True})
    sheets.append_event.assert_awaited_once()
    args, kwargs = sheets.append_event.call_args
    assert kwargs.get("event_type") == "offer_accepted" or "offer_accepted" in args
    message.delete.assert_awaited_once()
    show_categories_mock.assert_awaited_once_with(message, sheets)
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_offer_accept_when_already_accepted_does_not_log_again():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets()
    state = _make_state({OFFER_ACCEPTED_KEY: True})

    with patch("handlers.start.show_categories", new=AsyncMock()):
        await on_offer_accept(query, state, sheets)

    state.update_data.assert_not_called()
    sheets.append_event.assert_not_called()


# ------------------------------------------------------------------ #
# on_offer_full_text / on_offer_back
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_offer_full_text_shows_expanded_text():
    message = _make_message()
    query = _make_query(message)

    await on_offer_full_text(query)

    message.edit_text.assert_awaited_once()
    args, kwargs = message.edit_text.call_args
    assert args[0] == OFFER_FULL_TEXT
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_offer_back_returns_to_short_offer_text():
    message = _make_message()
    query = _make_query(message)

    await on_offer_back(query)

    message.edit_text.assert_awaited_once()
    args, kwargs = message.edit_text.call_args
    assert args[0] == OFFER_TEXT
    assert "reply_markup" in kwargs
