from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from config import Settings
from services.dispatch import (
    TAKE_ORDER_CALLBACK_PREFIX,
    get_offer_message_ids,
    notify_other_couriers_order_taken,
    record_offer_message,
    send_offer_to_couriers,
    take_order_callback_data,
    try_capture_order,
)
from services.sheets import Courier
from texts.ru import (
    ADMIN_NO_COURIERS_ON_SHIFT_TEMPLATE,
    COURIER_ORDER_ALREADY_TAKEN_MESSAGE,
)


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=lambda *a, **k: MagicMock(message_id=hash(a) % 100000)
    )
    bot.edit_message_text = AsyncMock()
    return bot


def _make_scheduler() -> MagicMock:
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    return scheduler


def _make_redis(set_result: bool = True) -> MagicMock:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=set_result)
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.get = AsyncMock(return_value=None)
    return redis


def _make_sheets(couriers: list[Courier]) -> MagicMock:
    sheets = MagicMock()
    sheets.get_couriers = AsyncMock(return_value=couriers)
    sheets.update_order_fields = AsyncMock(return_value=True)
    sheets.append_event = AsyncMock()
    return sheets


def _courier(courier_id="cur_001", telegram_id="111", on_shift=True, is_active=True) -> Courier:
    return Courier(
        courier_id=courier_id,
        name="Ahmet Y.",
        phone="+90 500",
        telegram_id=telegram_id,
        is_registered_legal=True,
        on_shift=on_shift,
        is_active=is_active,
    )


# ------------------------------------------------------------------ #
# try_capture_order — атомарный захват
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_try_capture_order_wins_when_key_not_set():
    redis = _make_redis(set_result=True)

    won = await try_capture_order(redis, "042", "cur_001")

    assert won is True
    redis.set.assert_awaited_once_with("orders:capture:042", "cur_001", nx=True)


@pytest.mark.asyncio
async def test_try_capture_order_loses_when_key_already_set():
    """Redis SET NX возвращает None/False, если ключ уже существовал —
    гарантированная атомарность даже при гонке двух курьеров."""
    redis = _make_redis(set_result=False)

    won = await try_capture_order(redis, "042", "cur_002")

    assert won is False


# ------------------------------------------------------------------ #
# offer message tracking
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_record_and_get_offer_message_ids():
    redis = _make_redis()
    redis.hgetall = AsyncMock(return_value={b"111": b"5001", b"222": b"5002"})

    await record_offer_message(redis, "042", "111", 5001)
    ids = await get_offer_message_ids(redis, "042")

    redis.hset.assert_awaited_once_with("orders:offer_msgs:042", "111", "5001")
    redis.expire.assert_awaited_once()
    assert ids == {"111": 5001, "222": 5002}


@pytest.mark.asyncio
async def test_notify_other_couriers_skips_winner_and_edits_rest():
    redis = _make_redis()
    redis.hgetall = AsyncMock(return_value={b"111": b"5001", b"222": b"5002", b"333": b"5003"})
    bot = _make_bot()
    scheduler = _make_scheduler()

    await notify_other_couriers_order_taken(bot, redis, "042", winning_telegram_id="222")

    assert bot.edit_message_text.await_count == 2  # все, кроме "222" (победителя)
    edited_chat_ids = {c.kwargs["chat_id"] for c in bot.edit_message_text.call_args_list}
    assert edited_chat_ids == {111, 333}
    for call in bot.edit_message_text.call_args_list:
        assert call.kwargs["text"] == COURIER_ORDER_ALREADY_TAKEN_MESSAGE


@pytest.mark.asyncio
async def test_notify_other_couriers_tolerates_telegram_errors():
    redis = _make_redis()
    redis.hgetall = AsyncMock(return_value={b"111": b"5001"})
    bot = _make_bot()
    scheduler = _make_scheduler()
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="blocked")
    )

    # не должно бросить исключение — курьер мог заблокировать бота
    await notify_other_couriers_order_taken(bot, redis, "042", winning_telegram_id="999")


# ------------------------------------------------------------------ #
# send_offer_to_couriers
# ------------------------------------------------------------------ #


def test_take_order_callback_data_uses_shared_prefix():
    assert take_order_callback_data("042") == f"{TAKE_ORDER_CALLBACK_PREFIX}042"


@pytest.mark.asyncio
async def test_send_offer_escalates_to_admin_when_zero_couriers_on_shift(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[_courier(on_shift=False)])  # никого на смене
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await send_offer_to_couriers(
        bot,
        sheets,
        redis,
        settings,
        scheduler,
        order_id="042",
        merchant_name="MonAmi",
        merchant_description="ул. Лара 12",
        delivery_address="Коньяалты",
        total_try="810.0",
    )

    sheets.update_order_fields.assert_awaited_once_with("042", {"status": "no_courier"})
    bot.send_message.assert_awaited()
    call_args = bot.send_message.call_args_list[0]
    assert call_args.args[1] == ADMIN_NO_COURIERS_ON_SHIFT_TEMPLATE.format(order_id="042")
    sheets.append_event.assert_awaited_once()
    assert sheets.append_event.call_args.kwargs["event_type"] == "stuck_order_escalated"
    scheduler.add_job.assert_not_called()  # синхронная эскалация — таймер тут не нужен


@pytest.mark.asyncio
async def test_send_offer_sends_to_all_on_shift_couriers(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    couriers = [
        _courier(courier_id="cur_001", telegram_id="111", on_shift=True),
        _courier(courier_id="cur_002", telegram_id="222", on_shift=True),
        _courier(courier_id="cur_003", telegram_id="333", on_shift=False),  # не на смене
    ]
    sheets = _make_sheets(couriers=couriers)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await send_offer_to_couriers(
        bot,
        sheets,
        redis,
        settings,
        scheduler,
        order_id="042",
        merchant_name="MonAmi",
        merchant_description="ул. Лара 12",
        delivery_address="Коньяалты",
        total_try="810.0",
    )

    # Отправлено ровно двум курьерам на смене, не третьему (не на смене)
    sent_chat_ids = {c.args[0] for c in bot.send_message.call_args_list}
    assert sent_chat_ids == {111, 222}

    # Day 6 (ТЗ §9): offer_timeout поставлен на этот заказ
    scheduler.add_job.assert_called_once()
    assert scheduler.add_job.call_args.kwargs["id"] == "offer_timeout:042"

    status_call = sheets.update_order_fields.call_args
    assert status_call.args[0] == "042"
    assert status_call.args[1]["status"] == "offered"
    assert "timestamp_offered" in status_call.args[1]

    event_call = sheets.append_event.call_args
    assert event_call.kwargs["event_type"] == "courier_offer_sent"


@pytest.mark.asyncio
async def test_send_offer_records_message_ids_for_each_courier(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[_courier(telegram_id="111")])
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await send_offer_to_couriers(
        bot,
        sheets,
        redis,
        settings,
        scheduler,
        order_id="042",
        merchant_name="MonAmi",
        merchant_description="ул. Лара 12",
        delivery_address="Коньяалты",
        total_try="810.0",
    )

    redis.hset.assert_awaited_once()
    assert redis.hset.call_args.args[0] == "orders:offer_msgs:042"
    assert redis.hset.call_args.args[1] == "111"


@pytest.mark.asyncio
async def test_send_offer_continues_when_one_courier_send_fails(valid_settings_kwargs):
    """Один заблокировавший бота курьер не должен срывать рассылку остальным."""
    settings = _make_settings(valid_settings_kwargs)
    couriers = [
        _courier(courier_id="cur_001", telegram_id="111"),
        _courier(courier_id="cur_002", telegram_id="222"),
    ]
    sheets = _make_sheets(couriers=couriers)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    bot.send_message = AsyncMock(
        side_effect=[
            TelegramBadRequest(method=MagicMock(), message="blocked"),
            MagicMock(message_id=999),
        ]
    )

    await send_offer_to_couriers(
        bot,
        sheets,
        redis,
        settings,
        scheduler,
        order_id="042",
        merchant_name="MonAmi",
        merchant_description="ул. Лара 12",
        delivery_address="Коньяалты",
        total_try="810.0",
    )

    assert bot.send_message.await_count == 2
    event_call = sheets.append_event.call_args
    assert "couriers=1" in event_call.kwargs["details"]  # только один долетел
