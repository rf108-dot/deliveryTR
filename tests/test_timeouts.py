from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.jobstores.base import JobLookupError

from config import Settings
from services import timeouts
from texts.ru import (
    ADMIN_DELIVERY_TIMEOUT_TEMPLATE,
    ADMIN_NO_ONE_ACCEPTED_TEMPLATE,
    ADMIN_PICKUP_TIMEOUT_TEMPLATE,
)


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_scheduler() -> MagicMock:
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    return scheduler


def _make_sheets(order: dict | None = None) -> MagicMock:
    sheets = MagicMock()
    sheets.get_order = AsyncMock(return_value=order)
    sheets.update_order_fields = AsyncMock(return_value=True)
    sheets.append_event = AsyncMock()
    return sheets


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture(autouse=True)
def _clear_registry():
    """Реестр модульный (глобальный) — чистим между тестами, чтобы не
    протекали зависимости одного теста в другой."""
    timeouts._registry.clear()
    yield
    timeouts._registry.clear()


# ------------------------------------------------------------------ #
# schedule_* / cancel_* — просто корректный вызов scheduler.add_job/
# remove_job с правильным job id
# ------------------------------------------------------------------ #


def test_schedule_offer_timeout_uses_correct_job_id():
    scheduler = _make_scheduler()

    timeouts.schedule_offer_timeout(scheduler, "042", 60)

    call = scheduler.add_job.call_args
    assert call.kwargs["id"] == "offer_timeout:042"
    assert call.kwargs["replace_existing"] is True


def test_schedule_offer_timeout_uses_timezone_aware_run_date():
    """
    Регресс-тест на живой баг: постановка таймера использовала
    "наивный" datetime.now() (без tzinfo) — планировщик интерпретировал
    его как время в поясе settings.TIMEZONE, но реальные системные часы
    машины могли быть выставлены на другой пояс. Расхождение в 2 часа
    означало, что таймер тихо откладывался почти на 2 часа позже
    задуманного — неотличимо от "вообще не сработал" в рамках короткого
    живого теста.
    """
    from datetime import timezone as tz

    scheduler = _make_scheduler()

    timeouts.schedule_offer_timeout(scheduler, "042", 60)

    run_date = scheduler.add_job.call_args.kwargs["run_date"]
    assert run_date.tzinfo is not None
    assert run_date.tzinfo == tz.utc


def test_schedule_pickup_timeout_uses_timezone_aware_run_date():
    from datetime import timezone as tz

    scheduler = _make_scheduler()

    timeouts.schedule_pickup_timeout(scheduler, "042", 15)

    run_date = scheduler.add_job.call_args.kwargs["run_date"]
    assert run_date.tzinfo == tz.utc


def test_schedule_delivery_timeout_uses_timezone_aware_run_date():
    from datetime import timezone as tz

    scheduler = _make_scheduler()

    timeouts.schedule_delivery_timeout(scheduler, "042", 40)

    run_date = scheduler.add_job.call_args.kwargs["run_date"]
    assert run_date.tzinfo == tz.utc


def test_cancel_offer_timeout_removes_correct_job_id():
    scheduler = _make_scheduler()

    timeouts.cancel_offer_timeout(scheduler, "042")

    scheduler.remove_job.assert_called_once_with("offer_timeout:042")


def test_cancel_is_safe_when_job_does_not_exist():
    scheduler = _make_scheduler()
    scheduler.remove_job.side_effect = JobLookupError("offer_timeout:042")

    timeouts.cancel_offer_timeout(scheduler, "042")  # не должно бросить исключение


def test_schedule_pickup_timeout_uses_correct_job_id():
    scheduler = _make_scheduler()

    timeouts.schedule_pickup_timeout(scheduler, "042", 15)

    assert scheduler.add_job.call_args.kwargs["id"] == "pickup_timeout:042"


def test_cancel_pickup_timeout_removes_correct_job_id():
    scheduler = _make_scheduler()

    timeouts.cancel_pickup_timeout(scheduler, "042")

    scheduler.remove_job.assert_called_once_with("pickup_timeout:042")


def test_schedule_delivery_timeout_uses_correct_job_id():
    scheduler = _make_scheduler()

    timeouts.schedule_delivery_timeout(scheduler, "042", 40)

    assert scheduler.add_job.call_args.kwargs["id"] == "delivery_timeout:042"


def test_cancel_delivery_timeout_removes_correct_job_id():
    scheduler = _make_scheduler()

    timeouts.cancel_delivery_timeout(scheduler, "042")

    scheduler.remove_job.assert_called_once_with("delivery_timeout:042")


# ------------------------------------------------------------------ #
# _check_offer_timeout
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_check_offer_timeout_escalates_when_still_offered(valid_settings_kwargs):
    bot = _make_bot()
    sheets = _make_sheets(order={"order_id": "042", "status": "offered"})
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_offer_timeout("042")

    sheets.update_order_fields.assert_awaited_once_with("042", {"status": "no_courier"})
    sheets.append_event.assert_awaited_once()
    assert sheets.append_event.call_args.kwargs["event_type"] == "stuck_order_escalated"
    assert sheets.append_event.call_args.kwargs["details"] == "offer_timeout"
    # ADMIN_TELEGRAM_IDS в фикстуре — два админа, notify_admins шлёт обоим
    assert bot.send_message.await_count == 2
    text = bot.send_message.call_args.args[1]
    assert text == ADMIN_NO_ONE_ACCEPTED_TEMPLATE.format(order_id="042")


@pytest.mark.asyncio
async def test_check_offer_timeout_noop_when_already_taken(valid_settings_kwargs):
    """Заказ вовремя перешёл в assigned — таймер должен был отмениться,
    но даже если сработал впритык (гонка на границе), проверка статуса
    не даёт лишний раз эскалировать уже взятый заказ."""
    bot = _make_bot()
    sheets = _make_sheets(order={"order_id": "042", "status": "assigned"})
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_offer_timeout("042")

    sheets.update_order_fields.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_check_offer_timeout_noop_when_order_not_found(valid_settings_kwargs):
    bot = _make_bot()
    sheets = _make_sheets(order=None)
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_offer_timeout("042")

    sheets.update_order_fields.assert_not_called()


# ------------------------------------------------------------------ #
# _check_pickup_timeout
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_check_pickup_timeout_escalates_when_still_assigned(valid_settings_kwargs):
    bot = _make_bot()
    sheets = _make_sheets(
        order={"order_id": "042", "status": "assigned", "courier_name": "Ahmet Y."}
    )
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_pickup_timeout("042")

    assert sheets.append_event.call_args.kwargs["details"] == "pickup_timeout"
    text = bot.send_message.call_args.args[1]
    assert text == ADMIN_PICKUP_TIMEOUT_TEMPLATE.format(
        order_id="042", courier_name="Ahmet Y.", pickup_timeout_min=settings.PICKUP_TIMEOUT_MIN
    )


@pytest.mark.asyncio
async def test_check_pickup_timeout_noop_when_already_picked_up(valid_settings_kwargs):
    bot = _make_bot()
    sheets = _make_sheets(order={"order_id": "042", "status": "picked_up"})
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_pickup_timeout("042")

    bot.send_message.assert_not_called()


# ------------------------------------------------------------------ #
# _check_delivery_timeout
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_check_delivery_timeout_escalates_when_still_picked_up(valid_settings_kwargs):
    bot = _make_bot()
    sheets = _make_sheets(order={"order_id": "042", "status": "picked_up"})
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_delivery_timeout("042")

    assert sheets.append_event.call_args.kwargs["details"] == "delivery_timeout"
    text = bot.send_message.call_args.args[1]
    assert text == ADMIN_DELIVERY_TIMEOUT_TEMPLATE.format(
        order_id="042", delivery_timeout_min=settings.DELIVERY_TIMEOUT_MIN
    )


@pytest.mark.asyncio
async def test_check_delivery_timeout_noop_when_already_delivered(valid_settings_kwargs):
    bot = _make_bot()
    sheets = _make_sheets(order={"order_id": "042", "status": "delivered"})
    settings = _make_settings(valid_settings_kwargs)
    timeouts.register_dependencies(bot, sheets, settings)

    await timeouts._check_delivery_timeout("042")

    bot.send_message.assert_not_called()
