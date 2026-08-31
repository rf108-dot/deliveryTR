from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from config import Settings
from services.notifications import notify_admins


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_notify_admins_sends_to_every_admin_id(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)  # ADMIN_TELEGRAM_IDS = 111111111,222222222
    bot = _make_bot()

    await notify_admins(bot, settings, "тест")

    assert bot.send_message.await_count == 2
    called_ids = {call.args[0] for call in bot.send_message.call_args_list}
    assert called_ids == {111111111, 222222222}


@pytest.mark.asyncio
async def test_notify_admins_continues_after_one_admin_fails(valid_settings_kwargs):
    """Best-effort: сбой доставки одному админу не должен прерывать рассылку остальным."""
    settings = Settings(**valid_settings_kwargs)
    bot = _make_bot()
    bot.send_message.side_effect = [
        TelegramBadRequest(method=MagicMock(), message="blocked"),
        None,
    ]

    await notify_admins(bot, settings, "тест")  # не должно бросить исключение

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_admins_excludes_matching_user_id(valid_settings_kwargs):
    """
    Живой фидбэк: если человек, разместивший заказ, сам входит в
    ADMIN_TELEGRAM_IDS (типичная ситуация при тестировании одним
    человеком), он не должен получать дублирующее техническое
    админ-сообщение — он уже получил человеческое клиентское.
    """
    settings = Settings(**valid_settings_kwargs)  # ADMIN_TELEGRAM_IDS = 111111111,222222222
    bot = _make_bot()

    await notify_admins(bot, settings, "тест", exclude_user_id=111111111)

    assert bot.send_message.await_count == 1
    bot.send_message.assert_awaited_once_with(222222222, "тест")


@pytest.mark.asyncio
async def test_notify_admins_exclude_matches_by_string_comparison(valid_settings_kwargs):
    """exclude_user_id может прийти строкой (из Sheets) — сравнение должно работать
    независимо от типа (int из settings.admin_ids vs str из order["user_id"])."""
    settings = Settings(**valid_settings_kwargs)
    bot = _make_bot()

    await notify_admins(bot, settings, "тест", exclude_user_id="111111111")

    assert bot.send_message.await_count == 1
    bot.send_message.assert_awaited_once_with(222222222, "тест")


@pytest.mark.asyncio
async def test_notify_admins_exclude_none_sends_to_everyone(valid_settings_kwargs):
    settings = Settings(**valid_settings_kwargs)
    bot = _make_bot()

    await notify_admins(bot, settings, "тест", exclude_user_id=None)

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_admins_continues_after_forbidden_error(valid_settings_kwargs):
    """
    Живой фидбэк: раньше ловился только TelegramBadRequest — но "бот не
    может написать первым" / "бот заблокирован" — это
    TelegramForbiddenError, СОСЕДНИЙ класс исключений (не подкласс
    TelegramBadRequest), раньше не ловился этим except вообще.
    """
    settings = Settings(**valid_settings_kwargs)
    bot = _make_bot()
    bot.send_message.side_effect = [
        TelegramForbiddenError(method=MagicMock(), message="bot was blocked by the user"),
        None,
    ]

    await notify_admins(bot, settings, "тест")  # не должно бросить исключение

    assert bot.send_message.await_count == 2
