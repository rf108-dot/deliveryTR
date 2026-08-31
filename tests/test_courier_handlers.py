from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest

from config import Settings
from handlers.courier import (
    COURIER_OFFER_ACCEPTED_KEY,
    DELIVERED_CALLBACK_PREFIX,
    PICKED_UP_CALLBACK_PREFIX,
    PROBLEM_CALLBACK_PREFIX,
    _safe_answer,
    cmd_shift_off,
    cmd_shift_on,
    on_courier_offer_accept,
    on_delivered,
    on_picked_up,
    on_problem,
    on_take_order,
)
from services.dispatch import TAKE_ORDER_CALLBACK_PREFIX
from services.sheets import Courier
from texts.ru import (
    ADMIN_COURIER_ASSIGNED_TEMPLATE,
    ADMIN_COURIER_OFF_SHIFT_TEMPLATE,
    ADMIN_COURIER_ON_SHIFT_TEMPLATE,
    ADMIN_MERCHANT_PROBLEM_TEMPLATE,
    ADMIN_ORDER_DELIVERED_TEMPLATE,
    ADMIN_ORDER_PICKED_UP_TEMPLATE,
    CLIENT_ORDER_ASSIGNED_TEMPLATE,
    CLIENT_ORDER_DELIVERED_TEMPLATE,
    CLIENT_ORDER_PICKED_UP_TEMPLATE,
    CLIENT_ORDER_PROBLEM_TEMPLATE,
    COURIER_DELIVERY_THANKS_MESSAGE,
    COURIER_MUST_PICK_UP_FIRST_MESSAGE,
    COURIER_NOT_REGISTERED_MESSAGE,
    COURIER_OFFER_TEXT,
    COURIER_ORDER_ALREADY_TAKEN_MESSAGE,
    COURIER_ORDER_NOT_FOUND_MESSAGE,
    COURIER_ORDER_PAUSED_MESSAGE,
    COURIER_ORDER_TAKEN_BY_YOU_TEMPLATE,
    COURIER_SHIFT_OFF_ACK,
    COURIER_SHIFT_ON_ACK,
    COURIER_TECHNICAL_ERROR_MESSAGE,
)

REGISTERED_COURIER = Courier(
    courier_id="cur_001",
    name="Ahmet Y.",
    phone="+90 5001112233",
    telegram_id="111111111",
    is_registered_legal=True,
    on_shift=False,
    is_active=True,
)


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


def _make_message(user_id: int = 111111111) -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(id=user_id)
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.delete = AsyncMock()
    return message


def _make_query(message: MagicMock, user_id: int = 111111111, data: str = "") -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = MagicMock(id=user_id)
    query.data = data
    query.answer = AsyncMock()
    return query


def _make_sheets(couriers: list[Courier] | None = None, order: dict | None = None) -> MagicMock:
    sheets = MagicMock()
    sheets.get_couriers = AsyncMock(return_value=couriers or [])
    sheets.update_courier_on_shift = AsyncMock(return_value=True)
    sheets.update_order_fields = AsyncMock(return_value=True)
    sheets.get_order = AsyncMock(return_value=order)
    sheets.append_event = AsyncMock()
    return sheets


def _make_redis(capture_result: bool = True, capturing_courier_id: str = "cur_999") -> MagicMock:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=capture_result)
    redis.get = AsyncMock(return_value=capturing_courier_id.encode())
    redis.hgetall = AsyncMock(return_value={})
    redis.delete = AsyncMock()
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


def _make_state(initial_data: dict | None = None) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=dict(initial_data or {}))
    state.update_data = AsyncMock()
    return state


def _order_row(**overrides) -> dict:
    base = {
        "order_id": "042",
        "user_id": "999",
        "merchant_name": "MonAmi",
        "delivery_address": "Коньяалты",
        "courier_id": "cur_001",
        "courier_name": "Ahmet Y.",
        "courier_phone": "+90 5001112233",
        "admin_notes": "Борщ x1 (330 ₺)",
        "status": "picked_up",  # по умолчанию — успешный путь, после "Забрал"
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ #
# §8.1: /shift_on, /shift_off — оферта курьера гейтит ПЕРВОЕ включение
# смены (живой фидбэк Day 5: раньше гейт стоял на /start, теперь тут)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_shift_on_registered_courier_updates_sheet_and_acks(valid_settings_kwargs):
    message = _make_message()
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state({COURIER_OFFER_ACCEPTED_KEY: True})  # оферта уже принята раньше

    await cmd_shift_on(message, settings, sheets, bot, state)

    sheets.update_courier_on_shift.assert_awaited_once_with("111111111", True)
    message.answer.assert_awaited_once()
    args, _ = message.answer.call_args
    assert args[0] == COURIER_SHIFT_ON_ACK


@pytest.mark.asyncio
async def test_shift_on_notifies_admins(valid_settings_kwargs):
    message = _make_message()
    settings = _make_settings(valid_settings_kwargs)  # ADMIN_TELEGRAM_IDS=111111111,222222222
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state({COURIER_OFFER_ACCEPTED_KEY: True})

    await cmd_shift_on(message, settings, sheets, bot, state)

    assert bot.send_message.await_count == 2
    text = bot.send_message.call_args_list[0].args[1]
    assert text == ADMIN_COURIER_ON_SHIFT_TEMPLATE.format(name="Ahmet Y.")


@pytest.mark.asyncio
async def test_shift_on_first_time_shows_offer_instead_of_activating(valid_settings_kwargs):
    """
    Регресс-тест на живой фидбэк: первое /shift_on для курьера, ещё не
    принявшего оферту, должно показать оферту, а НЕ сразу включать смену
    (раньше этот гейт ошибочно стоял на /start, уводя курьера от
    обычного клиентского флоу).
    """
    message = _make_message()
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()  # courier_offer_accepted отсутствует

    await cmd_shift_on(message, settings, sheets, bot, state)

    message.answer.assert_awaited_once()
    args, kwargs = message.answer.call_args
    assert args[0] == COURIER_OFFER_TEXT
    assert "reply_markup" in kwargs
    sheets.update_courier_on_shift.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_shift_off_updates_sheet_and_acks(valid_settings_kwargs):
    message = _make_message()
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_shift_off(message, settings, sheets, bot)

    sheets.update_courier_on_shift.assert_awaited_once_with("111111111", False)
    args, _ = message.answer.call_args
    assert args[0] == COURIER_SHIFT_OFF_ACK
    admin_text = bot.send_message.call_args_list[0].args[1]
    assert admin_text == ADMIN_COURIER_OFF_SHIFT_TEMPLATE.format(name="Ahmet Y.")


@pytest.mark.asyncio
async def test_shift_off_does_not_require_offer_acceptance(valid_settings_kwargs):
    """Выключение смены не гейтится офертой — в отличие от включения."""
    message = _make_message()
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_shift_off(message, settings, sheets, bot)  # без какого-либо state

    sheets.update_courier_on_shift.assert_awaited_once_with("111111111", False)


@pytest.mark.asyncio
async def test_shift_on_non_registered_user_gets_polite_rejection(valid_settings_kwargs):
    message = _make_message(user_id=999999999)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])  # 999999999 не в списке
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()

    await cmd_shift_on(message, settings, sheets, bot, state)

    message.answer.assert_awaited_once_with(
        COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=999999999)
    )
    sheets.update_courier_on_shift.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_shift_on_inactive_courier_is_treated_as_not_registered(valid_settings_kwargs):
    inactive = Courier(
        courier_id="cur_002",
        name="Уволенный",
        phone="+90",
        telegram_id="111111111",
        is_registered_legal=True,
        on_shift=False,
        is_active=False,
    )
    message = _make_message()
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[inactive])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()

    await cmd_shift_on(message, settings, sheets, bot, state)

    message.answer.assert_awaited_once_with(
        COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=111111111)
    )


# ------------------------------------------------------------------ #
# on_courier_offer_accept
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_courier_offer_accept_first_time_sets_flag_and_activates_shift(
    valid_settings_kwargs,
):
    message = _make_message()
    query = _make_query(message)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()  # ещё не принята

    await on_courier_offer_accept(query, state, sheets, settings, bot)

    state.update_data.assert_awaited_once_with(**{COURIER_OFFER_ACCEPTED_KEY: True})
    sheets.append_event.assert_awaited_once()
    args, kwargs = sheets.append_event.call_args
    assert kwargs.get("event_type") == "courier_offer_accepted"
    message.delete.assert_awaited_once()
    # Оферта принята -> смена сразу же включается
    sheets.update_courier_on_shift.assert_awaited_once_with("111111111", True)
    message.answer.assert_awaited_once()
    args, _ = message.answer.call_args
    assert args[0] == COURIER_SHIFT_ON_ACK
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_courier_offer_accept_when_already_accepted_still_activates_shift(
    valid_settings_kwargs,
):
    message = _make_message()
    query = _make_query(message)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state({COURIER_OFFER_ACCEPTED_KEY: True})

    await on_courier_offer_accept(query, state, sheets, settings, bot)

    state.update_data.assert_not_called()
    sheets.append_event.assert_not_called()
    sheets.update_courier_on_shift.assert_awaited_once_with("111111111", True)


@pytest.mark.asyncio
async def test_courier_offer_accept_non_registered_rejected(valid_settings_kwargs):
    message = _make_message(user_id=999999999)
    query = _make_query(message, user_id=999999999)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()

    await on_courier_offer_accept(query, state, sheets, settings, bot)

    query.answer.assert_awaited_once_with(
        COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=999999999), show_alert=True
    )
    sheets.update_courier_on_shift.assert_not_called()


# ------------------------------------------------------------------ #
# §8.2: "Взять"
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_take_order_winner_gets_assigned_and_active_order_card(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER], order=_order_row(status="offered"))
    redis = _make_redis(capture_result=True)
    bot = _make_bot()
    scheduler = _make_scheduler()

    with patch(
        "handlers.courier.notify_other_couriers_order_taken", new=AsyncMock()
    ) as notify_mock:
        await on_take_order(query, sheets, redis, settings, bot, scheduler)

    sheets.update_order_fields.assert_awaited_once()
    call_args = sheets.update_order_fields.call_args
    assert call_args.args[0] == "042"
    assert call_args.args[1]["status"] == "assigned"
    assert call_args.args[1]["courier_id"] == "cur_001"

    notify_mock.assert_awaited_once()
    message.edit_text.assert_awaited_once_with(
        COURIER_ORDER_TAKEN_BY_YOU_TEMPLATE.format(order_id="042")
    )
    message.answer.assert_awaited_once()  # карточка активного заказа
    query.answer.assert_awaited_once()

    # Day 6 (ТЗ §9): offer_timeout отменён, pickup_timeout поставлен
    scheduler.remove_job.assert_called_once_with("offer_timeout:042")
    scheduler.add_job.assert_called_once()
    assert scheduler.add_job.call_args.kwargs["id"] == "pickup_timeout:042"


@pytest.mark.asyncio
async def test_take_order_winner_notifies_client_that_courier_accepted(valid_settings_kwargs):
    """Живой фидбэк: клиент раньше не получал вообще ничего на этапе
    «курьер принял заказ» — только на «забрал»/«доставил»."""
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER], order=_order_row(status="offered"))
    redis = _make_redis(capture_result=True)
    bot = _make_bot()
    scheduler = _make_scheduler()

    with patch("handlers.courier.notify_other_couriers_order_taken", new=AsyncMock()):
        await on_take_order(query, sheets, redis, settings, bot, scheduler)

    client_call = next(c for c in bot.send_message.call_args_list if c.args[0] == 999)
    assert client_call.args[1] == CLIENT_ORDER_ASSIGNED_TEMPLATE.format(
        courier_name="Ahmet Y.", items="Борщ x1 (330 ₺)", merchant_name="MonAmi"
    )


@pytest.mark.asyncio
async def test_take_order_does_not_duplicate_message_when_client_is_admin(valid_settings_kwargs):
    """
    Живой фидбэк: если человек, оформивший заказ, сам входит в
    ADMIN_TELEGRAM_IDS (типичная ситуация при тестировании одним
    человеком), он должен получить только человеческое клиентское
    сообщение, без дублирующего технического админского.
    """
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)  # ADMIN_TELEGRAM_IDS=111111111,222222222
    sheets = _make_sheets(
        couriers=[REGISTERED_COURIER], order=_order_row(status="offered", user_id="111111111")
    )
    redis = _make_redis(capture_result=True)
    bot = _make_bot()
    scheduler = _make_scheduler()

    with patch("handlers.courier.notify_other_couriers_order_taken", new=AsyncMock()):
        await on_take_order(query, sheets, redis, settings, bot, scheduler)

    recipients = [c.args[0] for c in bot.send_message.call_args_list]
    assert recipients.count(111111111) == 1  # только клиентское, не + админское
    assert 222222222 in recipients  # второй админ по-прежнему уведомлён


@pytest.mark.asyncio
async def test_take_order_winner_notifies_admin_with_courier_name(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER], order=_order_row(status="offered"))
    redis = _make_redis(capture_result=True)
    bot = _make_bot()
    scheduler = _make_scheduler()

    with patch("handlers.courier.notify_other_couriers_order_taken", new=AsyncMock()):
        await on_take_order(query, sheets, redis, settings, bot, scheduler)

    admin_texts = [c.args[1] for c in bot.send_message.call_args_list]
    assert (
        ADMIN_COURIER_ASSIGNED_TEMPLATE.format(order_id="042", courier_name="Ahmet Y.")
        in admin_texts
    )


@pytest.mark.asyncio
async def test_take_order_loser_sees_alert_and_own_message_updated(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    redis = _make_redis(capture_result=False)  # кто-то другой уже захватил
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_take_order(query, sheets, redis, settings, bot, scheduler)

    query.answer.assert_awaited_once_with(COURIER_ORDER_ALREADY_TAKEN_MESSAGE, show_alert=True)
    message.edit_text.assert_awaited_once_with(COURIER_ORDER_ALREADY_TAKEN_MESSAGE)
    sheets.update_order_fields.assert_not_called()  # не назначаем проигравшему


@pytest.mark.asyncio
async def test_take_order_same_courier_retry_shows_active_card_not_taken_alert(
    valid_settings_kwargs,
):
    """
    Живой фидбэк, реальный случай: сетевая задержка на первом клике —
    курьер не увидел мгновенного ответа и нажал "Взять" ещё раз. Второй
    try_capture_order() корректно вернёт False (замок уже занят), но
    это ТОТ ЖЕ курьер, не кто-то другой — не должно пугать "уже принят
    другим курьером", вместо этого просто повторно показываем карточку
    активного заказа.
    """
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER], order=_order_row(status="assigned"))
    redis = _make_redis(capture_result=False, capturing_courier_id="cur_001")  # тот же курьер
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_take_order(query, sheets, redis, settings, bot, scheduler)

    query.answer.assert_awaited_once()
    args, _ = query.answer.call_args
    assert not args  # обычный тихий answer(), не алерт "уже занято"
    message.answer.assert_awaited_once()
    args, kwargs = message.answer.call_args
    assert "reply_markup" in kwargs
    sheets.update_order_fields.assert_not_called()  # не пере-назначаем повторно


@pytest.mark.asyncio
async def test_take_order_non_registered_courier_rejected(valid_settings_kwargs):
    message = _make_message(user_id=999999999)
    query = _make_query(message, user_id=999999999, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_take_order(query, sheets, redis, settings, bot, scheduler)

    query.answer.assert_awaited_once_with(
        COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=999999999), show_alert=True
    )
    redis.set.assert_not_called()  # даже не пытаемся захватить


@pytest.mark.asyncio
async def test_take_order_rejects_when_offer_already_expired(valid_settings_kwargs):
    """
    Живой фидбэк, реальный случай: заказ успел эскалироваться в
    no_courier (истёк OFFER_TIMEOUT_SEC) ровно между тем, как курьер
    увидел кнопку "Взять", и моментом клика — атомарный захват в Redis
    сам по себе не проверяет актуальность статуса в Sheets. Без этой
    проверки заказ молча "воскресал" обратно в assigned поверх уже
    случившейся эскалации "Никто не взял".
    """
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(
        couriers=[REGISTERED_COURIER], order=_order_row(status="no_courier")
    )
    redis = _make_redis(capture_result=True)  # Redis-захват технически успешен
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_take_order(query, sheets, redis, settings, bot, scheduler)

    query.answer.assert_awaited_once_with(COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)
    sheets.update_order_fields.assert_not_called()  # НЕ перезаписываем статус обратно


@pytest.mark.asyncio
async def test_take_order_releases_capture_on_unexpected_failure_after_capturing(
    valid_settings_kwargs,
):
    """
    Живой фидбэк, реальный случай: сбой (сетевой) ПОСЛЕ успешного
    Redis-захвата (например, на sheets.get_order() чуть ниже) оставлял
    замок висеть навсегда — повторный клик того же курьера получал
    "заказ уже принят другим курьером", хотя это была та же самая
    попытка. redis.delete должен быть вызван для отката замка.
    """
    message = _make_message()
    query = _make_query(message, data=f"{TAKE_ORDER_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(couriers=[REGISTERED_COURIER])
    sheets.get_order = AsyncMock(side_effect=OSError("Can't assign requested address"))
    redis = _make_redis(capture_result=True)
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_take_order(query, sheets, redis, settings, bot, scheduler)  # не должно бросить исключение

    redis.delete.assert_awaited_once()
    query.answer.assert_awaited_once_with(COURIER_TECHNICAL_ERROR_MESSAGE, show_alert=True)


# ------------------------------------------------------------------ #
# §8.3: этапы
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_picked_up_updates_status_and_notifies_admin_and_client(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{PICKED_UP_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row())
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_picked_up(query, sheets, settings, bot, scheduler)

    call_args = sheets.update_order_fields.call_args
    assert call_args.args[1]["status"] == "picked_up"

    texts = [c.args[1] for c in bot.send_message.call_args_list]
    assert ADMIN_ORDER_PICKED_UP_TEMPLATE.format(order_id="042") in texts
    assert (
        CLIENT_ORDER_PICKED_UP_TEMPLATE.format(
            courier_name="Ahmet Y.", courier_phone="+90 5001112233"
        )
        in texts
    )
    client_call = next(c for c in bot.send_message.call_args_list if c.args[0] == 999)
    assert client_call is not None
    query.answer.assert_awaited_once()

    # Day 6 (ТЗ §9): pickup_timeout отменён, delivery_timeout поставлен
    scheduler.remove_job.assert_called_once_with("pickup_timeout:042")
    assert scheduler.add_job.call_args.kwargs["id"] == "delivery_timeout:042"


@pytest.mark.asyncio
async def test_picked_up_unknown_order_shows_not_found_alert(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{PICKED_UP_CALLBACK_PREFIX}999")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=None)
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_picked_up(query, sheets, settings, bot, scheduler)

    sheets.update_order_fields.assert_not_called()
    bot.send_message.assert_not_called()
    query.answer.assert_awaited_once_with(COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)


@pytest.mark.asyncio
async def test_delivered_unknown_order_shows_not_found_alert(valid_settings_kwargs):
    """
    Регресс-тест на живой фидбэк: устаревшая/чужая кнопка «Доставил»
    раньше молчала (query.answer() без текста) — неотличимо от «всё ок,
    но без ответа». Теперь явный алерт, чтобы курьер понял, что что-то
    не так, а не решил, что бот завис.
    """
    message = _make_message()
    query = _make_query(message, data=f"{DELIVERED_CALLBACK_PREFIX}999")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=None)
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_delivered(query, sheets, settings, bot, scheduler)

    sheets.update_order_fields.assert_not_called()
    bot.send_message.assert_not_called()
    query.answer.assert_awaited_once_with(COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)


@pytest.mark.asyncio
async def test_problem_unknown_order_shows_not_found_alert(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{PROBLEM_CALLBACK_PREFIX}999")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=None)
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_problem(query, sheets, settings, bot, scheduler)

    sheets.update_order_fields.assert_not_called()
    bot.send_message.assert_not_called()
    query.answer.assert_awaited_once_with(COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)


@pytest.mark.asyncio
async def test_delivered_before_pickup_shows_must_pick_up_alert(valid_settings_kwargs):
    """
    По запросу: строгая последовательность этапов — «Доставил» и
    «Проблема на точке» доступны только ПОСЛЕ «Забрал заказ», раньше
    все три кнопки были равноправны и работали в любом порядке.
    """
    message = _make_message()
    query = _make_query(message, data=f"{DELIVERED_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row(status="assigned"))  # ещё не забрал
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_delivered(query, sheets, settings, bot, scheduler)

    query.answer.assert_awaited_once_with(COURIER_MUST_PICK_UP_FIRST_MESSAGE, show_alert=True)
    sheets.update_order_fields.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_problem_is_allowed_before_pickup(valid_settings_kwargs):
    """
    По запросу: "Проблема на точке" развёрнута обратно — доступна и ДО
    "Забрал заказ" (заведение закрыто, нет позиции и т.п. — это вполне
    может обнаружиться ещё до фактического получения заказа). Гейт по
    статусу picked_up остаётся ТОЛЬКО у "Доставил".
    """
    message = _make_message()
    query = _make_query(message, data=f"{PROBLEM_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row(status="assigned"))  # ещё не забрал
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_problem(query, sheets, settings, bot, scheduler)

    call_args = sheets.update_order_fields.call_args
    assert call_args.args[1] == {"status": "merchant_problem"}

    # Day 6 (ТЗ §9): оба таймера отменены превентивно — неважно, какой
    # реально был активен (заказ ещё не забран в этом тесте — pickup_timeout)
    scheduler.remove_job.assert_any_call("pickup_timeout:042")
    scheduler.remove_job.assert_any_call("delivery_timeout:042")


@pytest.mark.asyncio
async def test_delivered_sends_thanks_with_order_again_button(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{DELIVERED_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row())
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_delivered(query, sheets, settings, bot, scheduler)

    call_args = sheets.update_order_fields.call_args
    assert call_args.args[1]["status"] == "delivered"

    client_call = next(c for c in bot.send_message.call_args_list if c.args[0] == 999)
    assert client_call.args[1] == CLIENT_ORDER_DELIVERED_TEMPLATE
    assert "reply_markup" in client_call.kwargs

    admin_texts = [c.args[1] for c in bot.send_message.call_args_list]
    assert ADMIN_ORDER_DELIVERED_TEMPLATE.format(order_id="042") in admin_texts

    message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_delivered_shows_courier_shift_status_again(valid_settings_kwargs):
    """
    По запросу: после "Доставил" курьер должен увидеть благодарность и
    ту же карточку статуса смены, что и при /shift_on — с той же
    кнопкой "Уйти со смены" (он всё ещё на смене, on_shift не менялся
    этим переходом, менялся только статус ЗАКАЗА).
    """
    message = _make_message()
    query = _make_query(message, data=f"{DELIVERED_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row())
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_delivered(query, sheets, settings, bot, scheduler)

    message.answer.assert_awaited_once()
    args, kwargs = message.answer.call_args
    assert COURIER_DELIVERY_THANKS_MESSAGE in args[0]
    assert COURIER_SHIFT_ON_ACK in args[0]
    assert "reply_markup" in kwargs

    # Day 6 (ТЗ §9): delivery_timeout отменён — доставлено вовремя
    scheduler.remove_job.assert_called_once_with("delivery_timeout:042")


@pytest.mark.asyncio
async def test_problem_pauses_order_and_notifies_admin(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message, data=f"{PROBLEM_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row())
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_problem(query, sheets, settings, bot, scheduler)

    call_args = sheets.update_order_fields.call_args
    assert call_args.args[1] == {"status": "merchant_problem"}

    admin_texts = [c.args[1] for c in bot.send_message.call_args_list]
    assert (
        ADMIN_MERCHANT_PROBLEM_TEMPLATE.format(
            order_id="042",
            merchant_name="MonAmi",
            courier_name="Ahmet Y.",
            courier_phone="+90 5001112233",
        )
        in admin_texts
    )
    message.edit_text.assert_awaited_once_with(COURIER_ORDER_PAUSED_MESSAGE, reply_markup=None)


@pytest.mark.asyncio
async def test_problem_notifies_client_without_order_id(valid_settings_kwargs):
    """Живой фидбэк: клиент раньше не получал вообще ничего на "Проблема"
    — то, что видели с номером заказа, было админским сообщением."""
    message = _make_message()
    query = _make_query(message, data=f"{PROBLEM_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=_order_row())
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_problem(query, sheets, settings, bot, scheduler)

    client_call = next(c for c in bot.send_message.call_args_list if c.args[0] == 999)
    assert client_call.args[1] == CLIENT_ORDER_PROBLEM_TEMPLATE
    assert "042" not in client_call.args[1]


# ------------------------------------------------------------------ #
# _safe_answer — защита от устаревших callback_query после простоя бота
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_safe_answer_swallows_telegram_bad_request():
    """
    Живой фидбэк: если бот долго не работал (ноут выключен/спал),
    Telegram может отдать очень старый callback_query после рестарта —
    query.answer() на него кидает TelegramBadRequest ("query is too
    old..."). Раньше это нигде не ловилось и роняло обработку конкретно
    этого нажатия — хотя вся реальная работа (запись в Sheets,
    уведомления) к этому моменту уже выполнена.
    """
    query = MagicMock()
    query.answer = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="query is too old")
    )

    await _safe_answer(query, "текст", show_alert=True)  # не должно бросить исключение

    query.answer.assert_awaited_once_with("текст", show_alert=True)


@pytest.mark.asyncio
async def test_safe_answer_passes_through_normally():
    query = MagicMock()
    query.answer = AsyncMock()

    await _safe_answer(query, "текст")

    query.answer.assert_awaited_once_with("текст")


@pytest.mark.asyncio
async def test_resilient_decorator_shows_technical_error_on_unexpected_exception(
    valid_settings_kwargs,
):
    """
    Живой фидбэк — реальный случай: ноутбук вышел из сна, первый же
    запрос к Google Sheets API кинул низкоуровневый OSError ("Can't
    assign requested address"). Это происходит ДО query.answer()
    (внутри самого хендлера, при обращении к sheets.get_order()) — та
    ошибка не ловится _safe_answer, нужен более широкий "последний
    рубеж" на уровне всего хендлера.
    """
    message = _make_message()
    query = _make_query(message, data=f"{PICKED_UP_CALLBACK_PREFIX}042")
    settings = _make_settings(valid_settings_kwargs)
    sheets = MagicMock()
    sheets.get_order = AsyncMock(side_effect=OSError("Can't assign requested address"))
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_picked_up(query, sheets, settings, bot, scheduler)  # не должно бросить исключение

    query.answer.assert_awaited_once_with(COURIER_TECHNICAL_ERROR_MESSAGE, show_alert=True)
