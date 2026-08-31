from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from handlers.admin import cmd_service_resume, cmd_service_stop
from services.service_hours import SERVICE_STOP_REDIS_KEY
from texts.ru import ADMIN_SERVICE_RESUMED_ACK, ADMIN_SERVICE_STOPPED_ACK

ADMIN_ID = 111111111
NON_ADMIN_ID = 999999999


def _make_message(user_id: int) -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=user_id)
    message.answer = AsyncMock()
    return message


def _make_redis() -> MagicMock:
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    return redis


def _make_sheets() -> MagicMock:
    sheets = MagicMock()
    sheets.append_event = AsyncMock()
    return sheets


def _make_settings(valid_settings_kwargs) -> Settings:
    return Settings(**valid_settings_kwargs)  # ADMIN_TELEGRAM_IDS = 111111111,222222222


# ------------------------------------------------------------------ #
# /service_stop
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_service_stop_by_admin_sets_flag_and_acks(valid_settings_kwargs):
    message = _make_message(ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    sheets = _make_sheets()

    await cmd_service_stop(message, settings, redis, sheets)

    redis.set.assert_awaited_once_with(SERVICE_STOP_REDIS_KEY, "1")
    message.answer.assert_awaited_once_with(ADMIN_SERVICE_STOPPED_ACK)
    sheets.append_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_stop_by_non_admin_is_silently_ignored(valid_settings_kwargs):
    message = _make_message(NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    sheets = _make_sheets()

    await cmd_service_stop(message, settings, redis, sheets)

    redis.set.assert_not_called()
    message.answer.assert_not_called()
    sheets.append_event.assert_not_called()


# ------------------------------------------------------------------ #
# /service_resume
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_service_resume_by_admin_clears_flag_and_acks(valid_settings_kwargs):
    message = _make_message(ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    sheets = _make_sheets()

    await cmd_service_resume(message, settings, redis, sheets)

    redis.delete.assert_awaited_once_with(SERVICE_STOP_REDIS_KEY)
    message.answer.assert_awaited_once_with(ADMIN_SERVICE_RESUMED_ACK)
    sheets.append_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_resume_by_non_admin_is_silently_ignored(valid_settings_kwargs):
    message = _make_message(NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    sheets = _make_sheets()

    await cmd_service_resume(message, settings, redis, sheets)

    redis.delete.assert_not_called()
    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_service_stop_ignores_message_without_from_user(valid_settings_kwargs):
    """Защитный случай — message.from_user может быть None (напр. из канала)."""
    message = _make_message(ADMIN_ID)
    message.from_user = None
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    sheets = _make_sheets()

    await cmd_service_stop(message, settings, redis, sheets)

    redis.set.assert_not_called()
    message.answer.assert_not_called()
