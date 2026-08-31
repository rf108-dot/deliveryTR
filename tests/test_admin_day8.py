from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from handlers.admin import (
    DayStartCallback,
    cmd_add_courier_start,
    cmd_assign_order,
    cmd_broadcast_start,
    cmd_cancel_order,
    cmd_couriers,
    cmd_day_start,
    cmd_force_deliver,
    cmd_order_card,
    cmd_orders,
    cmd_stats,
    cmd_toggle_courier,
    cmd_toggle_item,
    cmd_toggle_merchant,
    on_add_courier_name,
    on_add_courier_phone,
    on_add_courier_telegram_id,
    on_broadcast_cancel,
    on_broadcast_confirm,
    on_day_start_action,
)
from services.sheets import Courier, Merchant, MenuItem
from states.user_states import AdminStates
from texts.ru import (
    ADMIN_ASSIGN_ACK_TEMPLATE,
    ADMIN_ASSIGN_COURIER_NOT_FOUND_TEMPLATE,
    ADMIN_BROADCAST_CANCELLED_ACK,
    ADMIN_BROADCAST_DONE_TEMPLATE,
    ADMIN_BROADCAST_EMPTY_MESSAGE,
    ADMIN_CANCEL_ACK_TEMPLATE,
    ADMIN_COURIER_TOGGLED_TEMPLATE,
    ADMIN_COURIERS_EMPTY,
    ADMIN_DELIVER_ACK_TEMPLATE,
    ADMIN_ITEM_TOGGLED_TEMPLATE,
    ADMIN_MERCHANT_TOGGLED_ACTIVE_TEMPLATE,
    ADMIN_ORDER_NOT_FOUND_TEMPLATE,
    ADMIN_ORDERS_EMPTY,
    ADMIN_STATS_NO_ORDERS,
    CLIENT_ORDER_CANCELLED_TEMPLATE,
)

ADMIN_ID = 111111111
NON_ADMIN_ID = 999999999
CLIENT_ID = 301746349


def _make_message(**overrides) -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.from_user = MagicMock(id=ADMIN_ID, username="admin")
    message.text = None
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _make_query(message: MagicMock, from_user=None) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = from_user or MagicMock(id=ADMIN_ID)
    query.answer = AsyncMock()
    return query


def _make_state(initial_data: dict | None = None) -> MagicMock:
    storage: dict = dict(initial_data or {})

    async def _get_data():
        return dict(storage)

    async def _update_data(**kwargs):
        storage.update(kwargs)
        return dict(storage)

    state = MagicMock()
    state.get_data = AsyncMock(side_effect=_get_data)
    state.update_data = AsyncMock(side_effect=_update_data)
    state.set_state = AsyncMock()
    return state


def _make_redis(seq: int = 1) -> MagicMock:
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=seq)
    return redis


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _make_scheduler() -> MagicMock:
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    return scheduler


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_sheets(**overrides) -> MagicMock:
    sheets = MagicMock()
    sheets.get_all_orders = AsyncMock(return_value=[])
    sheets.get_all_events = AsyncMock(return_value=[])
    sheets.get_order = AsyncMock(return_value=None)
    sheets.get_merchants = AsyncMock(return_value=[])
    sheets.get_items = AsyncMock(return_value=[])
    sheets.get_couriers = AsyncMock(return_value=[])
    sheets.update_order_fields = AsyncMock(return_value=True)
    sheets.update_merchant_fields = AsyncMock(return_value=True)
    sheets.update_item_fields = AsyncMock(return_value=True)
    sheets.update_courier_fields = AsyncMock(return_value=True)
    sheets.add_courier = AsyncMock()
    sheets.append_event = AsyncMock()
    for key, value in overrides.items():
        setattr(sheets, key, AsyncMock(return_value=value))
    return sheets


def _merchant(**overrides) -> Merchant:
    defaults = dict(
        merchant_id="rest_001", category="restaurant", name="MonAmi", description="",
        is_active=True, today_confirmed=True, working_hours="10:00-21:00",
    )
    defaults.update(overrides)
    return Merchant(**defaults)


def _item(**overrides) -> MenuItem:
    defaults = dict(
        item_id="item_001", merchant_id="rest_001", name="Борщ", description="",
        price_try=330.0, photo_url="", is_active=True, is_available=True,
    )
    defaults.update(overrides)
    return MenuItem(**defaults)


def _courier(**overrides) -> Courier:
    defaults = dict(
        courier_id="cur_001", name="Роман", phone="905075480108", telegram_id="500",
        is_registered_legal=True, on_shift=False, is_active=True,
    )
    defaults.update(overrides)
    return Courier(**defaults)


# ------------------------------------------------------------------ #
# Доступ не-админам
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_orders_ignored_for_non_admin(valid_settings_kwargs):
    message = _make_message(text="/orders")
    message.from_user = MagicMock(id=NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()

    await cmd_orders(message, settings, sheets)

    sheets.get_all_orders.assert_not_called()
    message.answer.assert_not_called()


# ------------------------------------------------------------------ #
# /orders, /order_042
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_orders_empty(valid_settings_kwargs):
    message = _make_message(text="/orders")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()

    await cmd_orders(message, settings, sheets)

    message.answer.assert_awaited_once_with(ADMIN_ORDERS_EMPTY)


@pytest.mark.asyncio
async def test_orders_excludes_delivered_and_cancelled(valid_settings_kwargs):
    message = _make_message(text="/orders")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_all_orders = AsyncMock(
        return_value=[
            {"order_id": "001", "status": "delivered", "order_kind": "standard"},
            {"order_id": "002", "status": "cancelled", "order_kind": "standard"},
            {"order_id": "003", "status": "assigned", "order_kind": "standard", "merchant_name": "MonAmi"},
        ]
    )

    await cmd_orders(message, settings, sheets)

    text = message.answer.await_args.args[0]
    assert "003" in text
    assert "001" not in text
    assert "002" not in text


@pytest.mark.asyncio
async def test_order_card_not_found(valid_settings_kwargs):
    message = _make_message(text="/order_999")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(get_order=None)

    await cmd_order_card(message, settings, sheets)

    message.answer.assert_awaited_once_with(ADMIN_ORDER_NOT_FOUND_TEMPLATE.format(order_id="999"))


@pytest.mark.asyncio
async def test_order_card_shows_p2p_fields(valid_settings_kwargs):
    message = _make_message(text="/order_043")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(
        get_order={
            "order_id": "043", "order_kind": "p2p", "status": "pending_review",
            "timestamp_created": "2026-08-31 10:00:00", "username": "RF108",
            "pickup_address": "Точка А", "dropoff_address": "Точка Б", "p2p_description": "Забрать документы",
        }
    )

    await cmd_order_card(message, settings, sheets)

    text = message.answer.await_args.args[0]
    assert "Точка А" in text
    assert "Точка Б" in text


# ------------------------------------------------------------------ #
# /assign_042_[courier_id]
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_assign_courier_not_found(valid_settings_kwargs):
    message = _make_message(text="/assign_042_cur_999")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(get_order={"order_id": "042", "status": "no_courier"})
    sheets.get_couriers = AsyncMock(return_value=[])
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_assign_order(message, settings, sheets, redis, bot, scheduler)

    message.answer.assert_awaited_once_with(
        ADMIN_ASSIGN_COURIER_NOT_FOUND_TEMPLATE.format(courier_id="cur_999")
    )


@pytest.mark.asyncio
async def test_assign_happy_path(valid_settings_kwargs):
    message = _make_message(text="/assign_042_cur_001")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(
        get_order={
            "order_id": "042", "status": "no_courier", "user_id": str(CLIENT_ID),
            "merchant_name": "MonAmi", "delivery_address": "Çağlayan 1", "admin_notes": "Борщ x1",
        }
    )
    sheets.get_couriers = AsyncMock(return_value=[_courier()])
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_assign_order(message, settings, sheets, redis, bot, scheduler)

    sheets.update_order_fields.assert_awaited_once()
    fields = sheets.update_order_fields.await_args.args[1]
    assert fields["status"] == "assigned"
    assert fields["courier_id"] == "cur_001"
    scheduler.add_job.assert_called_once()  # schedule_pickup_timeout
    assert bot.send_message.await_count >= 2  # курьер + клиент
    message.answer.assert_awaited_once_with(
        ADMIN_ASSIGN_ACK_TEMPLATE.format(order_id="042", courier_name="Роман")
    )


# ------------------------------------------------------------------ #
# /cancel_042, /deliver_042
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_cancel_order_notifies_client_with_reason(valid_settings_kwargs):
    message = _make_message(text="/cancel_042 клиент передумал")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(get_order={"order_id": "042", "status": "assigned", "user_id": str(CLIENT_ID)})
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_cancel_order(message, settings, sheets, bot, scheduler)

    sheets.update_order_fields.assert_awaited_once_with(
        "042", {"status": "cancelled", "cancel_reason": "клиент передумал"}
    )
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.args[1]
    assert "клиент передумал" in sent_text
    message.answer.assert_awaited_once_with(ADMIN_CANCEL_ACK_TEMPLATE.format(order_id="042"))


@pytest.mark.asyncio
async def test_force_deliver_happy_path(valid_settings_kwargs):
    message = _make_message(text="/deliver_042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(get_order={"order_id": "042", "status": "picked_up", "user_id": str(CLIENT_ID)})
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_force_deliver(message, settings, sheets, bot, scheduler)

    fields = sheets.update_order_fields.await_args.args[1]
    assert fields["status"] == "delivered"
    bot.send_message.assert_awaited_once()
    message.answer.assert_awaited_once_with(ADMIN_DELIVER_ACK_TEMPLATE.format(order_id="042"))


# ------------------------------------------------------------------ #
# /day_start
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_day_start_lists_active_merchants(valid_settings_kwargs):
    message = _make_message(text="/day_start")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_merchants = AsyncMock(
        return_value=[_merchant(merchant_id="rest_001", is_active=True), _merchant(merchant_id="rest_002", is_active=False)]
    )

    await cmd_day_start(message, settings, sheets)

    assert message.answer.await_count == 2  # заголовок + 1 активный мерчант


@pytest.mark.asyncio
async def test_day_start_confirm_sets_today_confirmed(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_merchants = AsyncMock(return_value=[_merchant(merchant_id="rest_001", name="MonAmi")])

    await on_day_start_action(query, DayStartCallback(merchant_id="rest_001", action="confirm"), settings, sheets)

    sheets.update_merchant_fields.assert_awaited_once_with("rest_001", {"today_confirmed": "TRUE"})


# ------------------------------------------------------------------ #
# /toggle_merchant_, /toggle_item_, /toggle_courier_
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_toggle_merchant_flips_is_active(valid_settings_kwargs):
    message = _make_message(text="/toggle_merchant_rest_001")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_merchants = AsyncMock(return_value=[_merchant(merchant_id="rest_001", is_active=True, name="MonAmi")])

    await cmd_toggle_merchant(message, settings, sheets)

    sheets.update_merchant_fields.assert_awaited_once_with("rest_001", {"is_active": "FALSE"})
    message.answer.assert_awaited_once_with(
        ADMIN_MERCHANT_TOGGLED_ACTIVE_TEMPLATE.format(name="MonAmi", value="FALSE")
    )


@pytest.mark.asyncio
async def test_toggle_item_flips_is_available(valid_settings_kwargs):
    message = _make_message(text="/toggle_item_item_001")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_items = AsyncMock(return_value=[_item(item_id="item_001", is_available=True, name="Борщ")])

    await cmd_toggle_item(message, settings, sheets)

    sheets.update_item_fields.assert_awaited_once_with("item_001", {"is_available": "FALSE"})
    message.answer.assert_awaited_once_with(ADMIN_ITEM_TOGGLED_TEMPLATE.format(name="Борщ", value="FALSE"))


@pytest.mark.asyncio
async def test_toggle_courier_flips_is_active(valid_settings_kwargs):
    message = _make_message(text="/toggle_courier_cur_001")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_couriers = AsyncMock(return_value=[_courier(courier_id="cur_001", is_active=True, name="Роман")])

    await cmd_toggle_courier(message, settings, sheets)

    sheets.update_courier_fields.assert_awaited_once_with("cur_001", {"is_active": "FALSE"})
    message.answer.assert_awaited_once_with(ADMIN_COURIER_TOGGLED_TEMPLATE.format(name="Роман", value="FALSE"))


# ------------------------------------------------------------------ #
# /couriers, /add_courier
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_couriers_empty(valid_settings_kwargs):
    message = _make_message(text="/couriers")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()

    await cmd_couriers(message, settings, sheets)

    message.answer.assert_awaited_once_with(ADMIN_COURIERS_EMPTY)


@pytest.mark.asyncio
async def test_add_courier_full_flow(valid_settings_kwargs):
    message1 = _make_message(text="/add_courier")
    state = _make_state()
    settings = _make_settings(valid_settings_kwargs)

    await cmd_add_courier_start(message1, settings, state)
    state.set_state.assert_awaited_once_with(AdminStates.adding_courier_name)

    message2 = _make_message(text="Ahmet Y.")
    await on_add_courier_name(message2, state)

    message3 = _make_message(text="+90 5xx")
    await on_add_courier_phone(message3, state)

    message4 = _make_message(text="123456789")
    sheets = _make_sheets()
    redis = _make_redis(seq=1)

    await on_add_courier_telegram_id(message4, state, sheets, redis)

    sheets.add_courier.assert_awaited_once()
    kwargs = sheets.add_courier.await_args.kwargs
    assert kwargs["courier_id"] == "cur_001"
    assert kwargs["name"] == "Ahmet Y."
    assert kwargs["telegram_id"] == "123456789"
    message4.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_courier_invalid_telegram_id_reprompts(valid_settings_kwargs):
    state = _make_state({"admin_new_courier_name": "X", "admin_new_courier_phone": "Y"})
    message = _make_message(text="not-a-number")
    sheets = _make_sheets()
    redis = _make_redis()

    await on_add_courier_telegram_id(message, state, sheets, redis)

    sheets.add_courier.assert_not_called()


# ------------------------------------------------------------------ #
# /stats
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_stats_no_orders_today(valid_settings_kwargs):
    message = _make_message(text="/stats")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()

    await cmd_stats(message, settings, sheets)

    message.answer.assert_awaited_once_with(ADMIN_STATS_NO_ORDERS)


@pytest.mark.asyncio
async def test_stats_computes_conversion(valid_settings_kwargs, monkeypatch):
    message = _make_message(text="/stats")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()

    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
    sheets.get_all_orders = AsyncMock(
        return_value=[
            {"order_id": "001", "status": "delivered", "timestamp_created": f"{today} 10:00:00"},
            {"order_id": "002", "status": "cancelled", "timestamp_created": f"{today} 11:00:00"},
            {"order_id": "003", "status": "delivered", "timestamp_created": "2020-01-01 10:00:00"},  # не сегодня
        ]
    )

    await cmd_stats(message, settings, sheets)

    text = message.answer.await_args.args[0]
    assert "delivered: 1" in text
    assert "cancelled: 1" in text
    assert "50%" in text  # 1 из 2 сегодняшних доставлен


# ------------------------------------------------------------------ #
# /broadcast
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_broadcast_empty_text_rejected(valid_settings_kwargs):
    message = _make_message(text="/broadcast")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    state = _make_state()
    command = MagicMock(args=None)

    await cmd_broadcast_start(message, settings, sheets, command, state)

    message.answer.assert_awaited_once_with(ADMIN_BROADCAST_EMPTY_MESSAGE)


@pytest.mark.asyncio
async def test_broadcast_confirm_sends_to_all_clients(valid_settings_kwargs):
    message = _make_message(text="/broadcast Всем привет!")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    sheets.get_all_events = AsyncMock(
        return_value=[
            {"actor_role": "client", "event_type": "bot_start", "actor_id": "111"},
            {"actor_role": "client", "event_type": "bot_start", "actor_id": "222"},
            {"actor_role": "admin", "event_type": "bot_start", "actor_id": "333"},  # не клиент
        ]
    )
    state = _make_state()
    command = MagicMock(args="Всем привет!")

    await cmd_broadcast_start(message, settings, sheets, command, state)
    message.answer.assert_awaited_once()

    query = _make_query(_make_message())
    bot = _make_bot()

    await on_broadcast_confirm(query, settings, sheets, state, bot)

    assert bot.send_message.await_count == 2  # только 2 клиента, не админ
    query.message.edit_text.assert_awaited_once_with(ADMIN_BROADCAST_DONE_TEMPLATE.format(sent=2, failed=0))


@pytest.mark.asyncio
async def test_broadcast_cancel_clears_pending(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    state = _make_state({"admin_broadcast_pending_text": "текст"})
    query = _make_query(_make_message())

    await on_broadcast_cancel(query, settings, state)

    query.message.edit_text.assert_awaited_once_with(ADMIN_BROADCAST_CANCELLED_ACK)
    data = await state.get_data()
    assert data["admin_broadcast_pending_text"] is None
