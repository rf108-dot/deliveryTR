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


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Живой баг из прод-эксплуатации привёл к добавлению одной
    повторной попытки с паузой _RETRY_DELAY_SECONDS (см.
    services/health_monitor.py) — тесты не должны реально ждать эти
    секунды при каждом прогоне провальной проверки."""
    monkeypatch.setattr(health_monitor.asyncio, "sleep", AsyncMock())


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


@pytest.mark.asyncio
async def test_transient_sheets_failure_is_forgiven_by_retry(valid_settings_kwargs):
    """Живой баг из прод-эксплуатации: разовый тайм-аут Google Sheets
    API дал ложный алерт Админу при полностью здоровой системе. Теперь
    первая неудачная попытка не должна приводить к алерту, если вторая
    (после паузы) прошла успешно."""
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    sheets = MagicMock()
    sheets.health_check = AsyncMock(side_effect=[Exception("временный затык"), None])
    health_monitor.register_dependencies(bot, _make_redis(), sheets, _make_geocoding(), settings)

    await health_monitor.run_periodic_health_check()

    assert sheets.health_check.await_count == 2
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_persistent_sheets_failure_still_notifies_after_retry(valid_settings_kwargs):
    """А устойчивую проблему (падает и вторая попытка тоже) ретрай
    маскировать не должен — Админ всё равно должен узнать."""
    bot = _make_bot()
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(raise_error=True)  # падает на КАЖДОМ вызове
    health_monitor.register_dependencies(bot, _make_redis(), sheets, _make_geocoding(), settings)

    await health_monitor.run_periodic_health_check()

    assert sheets.health_check.await_count == 2  # обе попытки были сделаны
    bot.send_message.assert_awaited()
    text = bot.send_message.call_args_list[0].args[1]
    assert "Google Sheets" in text
