from __future__ import annotations

from datetime import time as dt_time
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from services.service_hours import (
    SERVICE_STOP_REDIS_KEY,
    ServiceStatus,
    get_service_status,
    is_manually_stopped,
    set_manually_stopped,
)


def _make_redis(stopped_value: bytes | None = None) -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=stopped_value)
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    return redis


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    kwargs.setdefault("SERVICE_OPEN", "10:00")
    kwargs.setdefault("LAST_ORDER", "20:15")
    kwargs.setdefault("SERVICE_CLOSE", "21:00")
    return Settings(**kwargs)


# ------------------------------------------------------------------ #
# is_manually_stopped / set_manually_stopped
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_is_manually_stopped_true_when_flag_set():
    redis = _make_redis(stopped_value=b"1")

    assert await is_manually_stopped(redis) is True


@pytest.mark.asyncio
async def test_is_manually_stopped_false_when_flag_absent():
    redis = _make_redis(stopped_value=None)

    assert await is_manually_stopped(redis) is False


@pytest.mark.asyncio
async def test_set_manually_stopped_true_sets_redis_key():
    redis = _make_redis()

    await set_manually_stopped(redis, True)

    redis.set.assert_awaited_once_with(SERVICE_STOP_REDIS_KEY, "1")


@pytest.mark.asyncio
async def test_set_manually_stopped_false_deletes_redis_key():
    redis = _make_redis()

    await set_manually_stopped(redis, False)

    redis.delete.assert_awaited_once_with(SERVICE_STOP_REDIS_KEY)


# ------------------------------------------------------------------ #
# get_service_status — четыре ветки (ТЗ §6, §10.5)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_manually_stopped_has_priority_over_open_hours(valid_settings_kwargs):
    """/service_stop приоритетнее расписания, даже в часы полной работы."""
    redis = _make_redis(stopped_value=b"1")
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(15, 0))

    assert status == ServiceStatus.MANUALLY_STOPPED


@pytest.mark.asyncio
async def test_status_open_during_full_hours(valid_settings_kwargs):
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(15, 0))

    assert status == ServiceStatus.OPEN


@pytest.mark.asyncio
async def test_status_open_at_exact_opening_time(valid_settings_kwargs):
    """Границы включительно: ровно 10:00 — уже открыто."""
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(10, 0))

    assert status == ServiceStatus.OPEN


@pytest.mark.asyncio
async def test_status_last_order_passed_window(valid_settings_kwargs):
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(20, 30))

    assert status == ServiceStatus.LAST_ORDER_PASSED


@pytest.mark.asyncio
async def test_status_last_order_passed_at_exact_boundary(valid_settings_kwargs):
    """Ровно 20:15 — уже "последний заказ прошёл", не "открыто"."""
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(20, 15))

    assert status == ServiceStatus.LAST_ORDER_PASSED


@pytest.mark.asyncio
async def test_status_closed_at_night(valid_settings_kwargs):
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(2, 0))

    assert status == ServiceStatus.CLOSED


@pytest.mark.asyncio
async def test_status_closed_at_exact_close_time(valid_settings_kwargs):
    """Ровно 21:00 — уже закрыто, не "последний заказ прошёл"."""
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(21, 0))

    assert status == ServiceStatus.CLOSED


@pytest.mark.asyncio
async def test_status_closed_right_before_opening(valid_settings_kwargs):
    redis = _make_redis(stopped_value=None)
    settings = _make_settings(valid_settings_kwargs)

    status = await get_service_status(settings, redis, now=dt_time(9, 59))

    assert status == ServiceStatus.CLOSED
