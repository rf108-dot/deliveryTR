from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from services import health_monitor


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _make_redis(ping_result: bool = True) -> MagicMock:
    redis_client = MagicMock()
    redis_client.ping = AsyncMock(return_value=ping_result)
    return redis_client


def _make_sheets(raise_error: bool = False) -> MagicMock:
    sheets = MagicMock()
    if raise_error:
        sheets.health_check = AsyncMock(side_effect=Exception("Sheets unavailable"))
    else:
        sheets.health_check = AsyncMock()
    return sheets


def _make_geocoding(raise_error: bool = False) -> MagicMock:
    geocoding = MagicMock()
    if raise_error:
        geocoding.health_check = MagicMock(side_effect=Exception("Maps unavailable"))
    else:
        geocoding.health_check = MagicMock()
    return geocoding


@pytest.fixture(autouse=True)
def _clear_registry():
    health_monitor._registry.clear()
    yield
    health_monitor._registry.clear()


@pytest.mark.asyncio
async def test_periodic_health_check_all_ok_does_not_notify(valid_settings_kwargs):
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    health_monitor.register_dependencies(
        bot, _make_redis(), _make_sheets(), _make_geocoding(), settings
    )

    await health_monitor.run_periodic_health_check()

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_periodic_health_check_redis_down_notifies_admins(valid_settings_kwargs):
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    health_monitor.register_dependencies(
        bot, _make_redis(ping_result=False), _make_sheets(), _make_geocoding(), settings
    )

    await health_monitor.run_periodic_health_check()

    bot.send_message.assert_awaited()
    text = bot.send_message.call_args_list[0].args[1]
    assert "Redis" in text


@pytest.mark.asyncio
async def test_periodic_health_check_sheets_down_notifies_admins(valid_settings_kwargs):
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    health_monitor.register_dependencies(
        bot, _make_redis(), _make_sheets(raise_error=True), _make_geocoding(), settings
    )

    await health_monitor.run_periodic_health_check()

    text = bot.send_message.call_args_list[0].args[1]
    assert "Google Sheets" in text


@pytest.mark.asyncio
async def test_periodic_health_check_maps_down_notifies_admins(valid_settings_kwargs):
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    health_monitor.register_dependencies(
        bot, _make_redis(), _make_sheets(), _make_geocoding(raise_error=True), settings
    )

    await health_monitor.run_periodic_health_check()

    text = bot.send_message.call_args_list[0].args[1]
    assert "Google Maps" in text


@pytest.mark.asyncio
async def test_periodic_health_check_never_raises_even_on_failure(valid_settings_kwargs):
    """В отличие от startup health-check, периодическая проверка не
    должна ронять бот — только уведомлять."""
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    health_monitor.register_dependencies(
        bot,
        _make_redis(ping_result=False),
        _make_sheets(raise_error=True),
        _make_geocoding(raise_error=True),
        settings,
    )

    await health_monitor.run_periodic_health_check()  # не должно бросить исключение
