from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import Settings
from handlers.catalog import CustomOrderEntryCallback
from handlers.custom_order import (
    CUSTOM_ORDER_CANCEL_CALLBACK_DATA,
    _check_custom_order_timeout,
    _job_id,
    _pending_key,
    cmd_confirm_custom,
    cmd_reject_custom,
    on_custom_order_cancel,
    on_custom_order_description_received,
    on_custom_order_entry,
    on_reject_reason_received,
    register_dependencies,
)
from services.sheets import Merchant
from states.user_states import CustomOrderStates
from texts.ru import (
    ADMIN_CONFIRM_CUSTOM_NOT_FOUND_MESSAGE,
    ADMIN_CUSTOM_ORDER_CONFIRMED_ACK,
    ADMIN_CUSTOM_ORDER_REJECTED_ACK,
    ADMIN_REJECT_CUSTOM_REASON_PROMPT,
    CUSTOM_ORDER_CANCELLED_ACK,
    CUSTOM_ORDER_CONFIRMED_MESSAGE,
    CUSTOM_ORDER_EMPTY_TEXT_MESSAGE,
    CUSTOM_ORDER_PROMPT,
    CUSTOM_ORDER_REJECTED_TEMPLATE,
    CUSTOM_ORDER_SUBMITTED_ACK,
    CUSTOM_ORDER_TIMEOUT_CLIENT_MESSAGE,
)

ADMIN_ID = 111111111
NON_ADMIN_ID = 999999999
CLIENT_ID = 301746349


def _merchant(**overrides) -> Merchant:
    defaults = dict(
        merchant_id="rest_001",
        category="restaurant",
        name="MonAmi",
        description="Европейская кухня",
        is_active=True,
        today_confirmed=True,
        working_hours="10:00-21:00",
    )
    defaults.update(overrides)
    return Merchant(**defaults)


def _make_message(**overrides) -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.from_user = MagicMock(id=CLIENT_ID, username="RF108")
    message.text = None
    message.caption = None
    message.photo = None
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _make_query(message: MagicMock, from_user=None) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = from_user or MagicMock(id=CLIENT_ID, username="RF108")
    query.answer = AsyncMock()
    return query


def _make_sheets(merchants: list[Merchant] | None = None) -> MagicMock:
    sheets = MagicMock()
    sheets.get_merchants = AsyncMock(return_value=merchants or [_merchant()])
    return sheets


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


def _make_redis() -> MagicMock:
    redis = MagicMock()
    redis._store: dict[str, bytes] = {}

    async def _get(key):
        return redis._store.get(key)

    async def _set(key, value, ex=None):
        redis._store[key] = value.encode() if isinstance(value, str) else value
        return True

    async def _delete(key):
        redis._store.pop(key, None)

    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)
    redis.delete = AsyncMock(side_effect=_delete)
    return redis


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.id = 8987350738
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
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


# ------------------------------------------------------------------ #
# Вход: кнопка на карточке позиции
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_custom_order_entry_prompts_and_sets_state():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_merchant(merchant_id="rest_001", name="MonAmi")])
    state = _make_state()

    await on_custom_order_entry(
        query, CustomOrderEntryCallback(merchant_id="rest_001"), sheets, state
    )

    message.answer.assert_awaited_once()
    assert message.answer.call_args.args[0] == CUSTOM_ORDER_PROMPT.format(merchant_name="MonAmi")
    state.set_state.assert_awaited_once_with(CustomOrderStates.waiting_for_description)
    data = await state.get_data()
    assert data["custom_order_merchant_id"] == "rest_001"
    assert data["custom_order_merchant_name"] == "MonAmi"
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_custom_order_cancel_resets_state():
    message = _make_message()
    query = _make_query(message)
    state = _make_state({"custom_order_merchant_id": "rest_001"})

    await on_custom_order_cancel(query, state)

    state.set_state.assert_awaited_once_with(None)
    message.edit_text.assert_awaited_once_with(CUSTOM_ORDER_CANCELLED_ACK)
    data = await state.get_data()
    assert data["custom_order_merchant_id"] is None


# ------------------------------------------------------------------ #
# Получение описания
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_empty_description_is_rejected(valid_settings_kwargs):
    message = _make_message(text="   ")
    state = _make_state({"custom_order_merchant_id": "rest_001", "custom_order_merchant_name": "MonAmi"})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_custom_order_description_received(message, state, settings, redis, bot, scheduler)

    message.answer.assert_awaited_once_with(CUSTOM_ORDER_EMPTY_TEXT_MESSAGE)
    redis.set.assert_not_called()
    scheduler.add_job.assert_not_called()


@pytest.mark.asyncio
async def test_description_received_saves_pending_and_notifies_admin(valid_settings_kwargs):
    message = _make_message(text="Хочу торт Наполеон, любой размер")
    state = _make_state({"custom_order_merchant_id": "rest_001", "custom_order_merchant_name": "MonAmi"})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_custom_order_description_received(message, state, settings, redis, bot, scheduler)

    # Заявка сохранена в Redis под ключом на user_id
    pending_raw = await redis.get(_pending_key(CLIENT_ID))
    assert pending_raw is not None
    pending = json.loads(pending_raw)
    assert pending["merchant_id"] == "rest_001"
    assert pending["description"] == "Хочу торт Наполеон, любой размер"

    # Таймаут запланирован
    scheduler.add_job.assert_called_once()
    assert scheduler.add_job.call_args.kwargs["id"] == _job_id(CLIENT_ID)

    # Клиент получил подтверждение приёма заявки
    message.answer.assert_awaited_once_with(CUSTOM_ORDER_SUBMITTED_ACK)

    # Админ (единственный в ADMIN_TELEGRAM_IDS из фикстуры — их два) уведомлён
    assert bot.send_message.await_count == 2  # оба admin_id из valid_settings_kwargs

    # Состояние сброшено
    state.set_state.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_description_with_photo_notifies_admin_via_photo(valid_settings_kwargs):
    photo = MagicMock()
    photo.file_id = "AgACfake123"
    message = _make_message(text="Ваза как на фото", photo=[photo])
    state = _make_state({"custom_order_merchant_id": "rest_001", "custom_order_merchant_name": "MonAmi"})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_custom_order_description_received(message, state, settings, redis, bot, scheduler)

    bot.send_photo.assert_awaited()
    bot.send_message.assert_not_called()


# ------------------------------------------------------------------ #
# /confirm_custom_[id]
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_confirm_custom_by_non_admin_is_ignored(valid_settings_kwargs):
    message = _make_message(text=f"/confirm_custom_{CLIENT_ID}")
    message.from_user = MagicMock(id=NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    dispatcher = Dispatcher(storage=MemoryStorage())

    await cmd_confirm_custom(message, settings, redis, bot, scheduler, dispatcher)

    message.answer.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_custom_not_found_replies_gracefully(valid_settings_kwargs):
    message = _make_message(text=f"/confirm_custom_{CLIENT_ID}")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    dispatcher = Dispatcher(storage=MemoryStorage())

    await cmd_confirm_custom(message, settings, redis, bot, scheduler, dispatcher)

    message.answer.assert_awaited_once_with(ADMIN_CONFIRM_CUSTOM_NOT_FOUND_MESSAGE)


@pytest.mark.asyncio
async def test_confirm_custom_happy_path_starts_checkout_for_client(valid_settings_kwargs):
    message = _make_message(text=f"/confirm_custom_{CLIENT_ID}")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    dispatcher = Dispatcher(storage=MemoryStorage())

    pending = {
        "merchant_id": "rest_001",
        "merchant_name": "MonAmi",
        "description": "Хочу торт",
        "photo_file_id": "",
        "username": "RF108",
    }
    await redis.set(_pending_key(CLIENT_ID), json.dumps(pending, ensure_ascii=False))

    await cmd_confirm_custom(message, settings, redis, bot, scheduler, dispatcher)

    # Заявка снята из Redis, таймер отменён
    assert await redis.get(_pending_key(CLIENT_ID)) is None
    scheduler.remove_job.assert_called_once_with(_job_id(CLIENT_ID))

    # Клиент получил подтверждение + запрос геолокации (start_checkout_for_user)
    sent_texts = [call.args[1] for call in bot.send_message.await_args_list]
    assert CUSTOM_ORDER_CONFIRMED_MESSAGE in sent_texts

    # FSM клиента реально перешло в чекаут с order_kind=custom
    target_state_storage_key = None
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=bot.id, chat_id=CLIENT_ID, user_id=CLIENT_ID)
    saved_data = await dispatcher.storage.get_data(key)
    assert saved_data["order_draft"]["order_kind"] == "custom"
    assert saved_data["order_draft"]["custom_description"] == "Хочу торт"
    saved_state = await dispatcher.storage.get_state(key)
    assert saved_state == "OrderStates:waiting_for_location"

    message.answer.assert_awaited_once_with(ADMIN_CUSTOM_ORDER_CONFIRMED_ACK)


# ------------------------------------------------------------------ #
# /reject_custom_[id]
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_reject_custom_happy_path_notifies_client_with_reason(valid_settings_kwargs):
    message = _make_message(text=f"/reject_custom_{CLIENT_ID} не работаем с этим районом")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()

    pending = {"merchant_id": "rest_001", "merchant_name": "MonAmi", "description": "x", "photo_file_id": "", "username": ""}
    await redis.set(_pending_key(CLIENT_ID), json.dumps(pending))

    await cmd_reject_custom(message, settings, redis, bot, scheduler, state)

    assert await redis.get(_pending_key(CLIENT_ID)) is None
    scheduler.remove_job.assert_called_once_with(_job_id(CLIENT_ID))
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.args[1]
    assert "не работаем с этим районом" in sent_text
    message.answer.assert_awaited_once_with(ADMIN_CUSTOM_ORDER_REJECTED_ACK)


@pytest.mark.asyncio
async def test_reject_custom_without_reason_prompts_and_remembers(valid_settings_kwargs):
    """Живой фидбэк из тестирования: голая /reject_custom_[id] без
    причины больше не отклоняет молча шаблонным текстом — запрашивает
    причину у Админа и запоминает получателя."""
    message = _make_message(text=f"/reject_custom_{CLIENT_ID}")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    state = _make_state()

    pending = {"merchant_id": "rest_001", "merchant_name": "MonAmi", "description": "x", "photo_file_id": "", "username": ""}
    await redis.set(_pending_key(CLIENT_ID), json.dumps(pending))

    await cmd_reject_custom(message, settings, redis, bot, scheduler, state)

    bot.send_message.assert_not_called()  # клиенту рано отправлять — причина не введена
    message.answer.assert_awaited_once_with(ADMIN_REJECT_CUSTOM_REASON_PROMPT)
    state.set_state.assert_awaited_once_with(CustomOrderStates.waiting_for_reject_reason)
    data = await state.get_data()
    assert data["custom_order_reject_target_user_id"] == CLIENT_ID
    # заявка НЕ снята из Redis и таймер НЕ отменён — отказ ещё не завершён
    assert await redis.get(_pending_key(CLIENT_ID)) is not None
    scheduler.remove_job.assert_not_called()


@pytest.mark.asyncio
async def test_followup_reject_reason_completes_rejection(valid_settings_kwargs):
    state = _make_state({"custom_order_reject_target_user_id": CLIENT_ID})
    message = _make_message(text="К сожалению, такой товар не доставляем")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    pending = {"merchant_id": "rest_001", "merchant_name": "MonAmi", "description": "x", "photo_file_id": "", "username": ""}
    await redis.set(_pending_key(CLIENT_ID), json.dumps(pending))

    await on_reject_reason_received(message, settings, redis, bot, scheduler, state)

    assert await redis.get(_pending_key(CLIENT_ID)) is None
    scheduler.remove_job.assert_called_once_with(_job_id(CLIENT_ID))
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.args[1]
    assert "К сожалению, такой товар не доставляем" in sent_text
    message.answer.assert_awaited_once_with(ADMIN_CUSTOM_ORDER_REJECTED_ACK)
    state.set_state.assert_awaited_once_with(None)
    data = await state.get_data()
    assert data["custom_order_reject_target_user_id"] is None


@pytest.mark.asyncio
async def test_followup_reject_reason_from_non_admin_is_ignored(valid_settings_kwargs):
    state = _make_state({"custom_order_reject_target_user_id": CLIENT_ID})
    message = _make_message(text="я не админ")
    message.from_user = MagicMock(id=NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_reject_reason_received(message, settings, redis, bot, scheduler, state)

    bot.send_message.assert_not_called()


# ------------------------------------------------------------------ #
# Таймаут
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_timeout_auto_rejects_when_still_pending(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    register_dependencies(bot, settings, redis)

    pending = {"merchant_id": "rest_001", "merchant_name": "MonAmi", "description": "x", "photo_file_id": "", "username": "RF108"}
    await redis.set(_pending_key(CLIENT_ID), json.dumps(pending))

    await _check_custom_order_timeout(str(CLIENT_ID))

    assert await redis.get(_pending_key(CLIENT_ID)) is None
    sent_texts = [call.args[1] for call in bot.send_message.await_args_list]
    assert CUSTOM_ORDER_TIMEOUT_CLIENT_MESSAGE in sent_texts


@pytest.mark.asyncio
async def test_timeout_is_noop_when_already_resolved(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    register_dependencies(bot, settings, redis)

    # Заявки нет в Redis — уже обработана (confirm/reject раньше таймера)
    await _check_custom_order_timeout(str(CLIENT_ID))

    bot.send_message.assert_not_called()
