from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from handlers.support import (
    cmd_reply,
    cmd_support,
    on_support_button,
    on_support_question_received,
)
from states.user_states import SupportStates
from texts.ru import (
    ADMIN_REPLY_DELIVERY_FAILED_MESSAGE,
    ADMIN_REPLY_EMPTY_MESSAGE,
    ADMIN_REPLY_SENT_ACK,
    ADMIN_SUPPORT_QUESTION_TEMPLATE,
    CLIENT_SUPPORT_REPLY_TEMPLATE,
    SUPPORT_FLOOD_LIMIT_MESSAGE_TEMPLATE,
    SUPPORT_QUESTION_PROMPT,
    SUPPORT_QUESTION_SUBMITTED_ACK,
)

ADMIN_ID = 111111111
NON_ADMIN_ID = 999999999
CLIENT_ID = 301746349


def _make_message(**overrides) -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.from_user = MagicMock(id=CLIENT_ID, username="RF108")
    message.text = None
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _make_query(message: MagicMock) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.answer = AsyncMock()
    return query


def _make_state() -> MagicMock:
    state = MagicMock()
    state.set_state = AsyncMock()
    return state


def _make_redis(counter_values: dict[str, int] | None = None) -> MagicMock:
    """counter_values — предзаданное значение счётчика (для симуляции
    "уже отправил N вопросов ранее")."""
    counters = dict(counter_values or {})
    redis = MagicMock()

    async def _incr(key):
        counters[key] = counters.get(key, 0) + 1
        return counters[key]

    redis.incr = AsyncMock(side_effect=_incr)
    redis.expire = AsyncMock()
    return redis


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


# ------------------------------------------------------------------ #
# Вход
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_cmd_support_prompts_for_question():
    message = _make_message()
    state = _make_state()

    await cmd_support(message, state)

    state.set_state.assert_awaited_once_with(SupportStates.waiting_for_question)
    message.answer.assert_awaited_once_with(SUPPORT_QUESTION_PROMPT)


@pytest.mark.asyncio
async def test_support_button_prompts_for_question():
    message = _make_message()
    query = _make_query(message)
    state = _make_state()

    await on_support_button(query, state)

    state.set_state.assert_awaited_once_with(SupportStates.waiting_for_question)
    message.answer.assert_awaited_once_with(SUPPORT_QUESTION_PROMPT)
    query.answer.assert_awaited_once()


# ------------------------------------------------------------------ #
# Получение вопроса + пересылка админам
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_question_received_forwards_to_admins(valid_settings_kwargs):
    message = _make_message(text="Где мой заказ #042?")
    state = _make_state()
    settings = _make_settings(valid_settings_kwargs)  # 2 админа в фикстуре
    redis = _make_redis()
    bot = _make_bot()

    await on_support_question_received(message, state, settings, redis, bot)

    state.set_state.assert_awaited_once_with(None)
    message.answer.assert_awaited_once_with(SUPPORT_QUESTION_SUBMITTED_ACK)
    assert bot.send_message.await_count == 2  # оба admin_id
    sent_text = bot.send_message.await_args_list[0].args[1]
    assert "Где мой заказ #042?" in sent_text
    assert f"/reply_{CLIENT_ID}" in sent_text


@pytest.mark.asyncio
async def test_empty_question_is_reprompted():
    message = _make_message(text="   ")
    state = _make_state()
    settings = MagicMock()
    redis = _make_redis()
    bot = _make_bot()

    await on_support_question_received(message, state, settings, redis, bot)

    message.answer.assert_awaited_once_with(SUPPORT_QUESTION_PROMPT)
    state.set_state.assert_not_called()
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_flood_limit_blocks_forwarding_after_max_messages(valid_settings_kwargs):
    settings = _make_settings(
        valid_settings_kwargs, SUPPORT_FLOOD_MAX_MESSAGES=3, SUPPORT_FLOOD_WINDOW_SEC=600
    )
    redis = _make_redis()
    bot = _make_bot()

    # Первые 3 вопроса — в пределах лимита, пересылаются
    for i in range(3):
        message = _make_message(text=f"Вопрос {i}")
        state = _make_state()
        await on_support_question_received(message, state, settings, redis, bot)
        message.answer.assert_awaited_once_with(SUPPORT_QUESTION_SUBMITTED_ACK)

    assert bot.send_message.await_count == 3 * 2  # 3 вопроса × 2 админа

    # 4-й вопрос — лимит исчерпан, НЕ пересылается
    message4 = _make_message(text="Четвёртый вопрос")
    state4 = _make_state()
    await on_support_question_received(message4, state4, settings, redis, bot)

    message4.answer.assert_awaited_once_with(
        SUPPORT_FLOOD_LIMIT_MESSAGE_TEMPLATE.format(minutes=10)
    )
    assert bot.send_message.await_count == 3 * 2  # не увеличилось


@pytest.mark.asyncio
async def test_flood_counter_sets_ttl_only_on_first_message(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()

    message1 = _make_message(text="Первый")
    await on_support_question_received(message1, _make_state(), settings, redis, bot)
    redis.expire.assert_awaited_once()

    message2 = _make_message(text="Второй")
    await on_support_question_received(message2, _make_state(), settings, redis, bot)
    redis.expire.assert_awaited_once()  # TTL не переустанавливается повторно


# ------------------------------------------------------------------ #
# /reply_[user_id] [текст]
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_reply_by_non_admin_is_ignored(valid_settings_kwargs):
    message = _make_message(text=f"/reply_{CLIENT_ID} Ваш заказ уже в пути")
    message.from_user = MagicMock(id=NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    bot = _make_bot()

    await cmd_reply(message, settings, bot)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_reply_empty_text_is_rejected(valid_settings_kwargs):
    message = _make_message(text=f"/reply_{CLIENT_ID}")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    bot = _make_bot()

    await cmd_reply(message, settings, bot)

    bot.send_message.assert_not_called()
    message.answer.assert_awaited_once_with(ADMIN_REPLY_EMPTY_MESSAGE.format(user_id=CLIENT_ID))


@pytest.mark.asyncio
async def test_reply_happy_path_sends_to_client(valid_settings_kwargs):
    message = _make_message(text=f"/reply_{CLIENT_ID} Ваш заказ уже в пути")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    bot = _make_bot()

    await cmd_reply(message, settings, bot)

    bot.send_message.assert_awaited_once_with(
        CLIENT_ID, CLIENT_SUPPORT_REPLY_TEMPLATE.format(text="Ваш заказ уже в пути")
    )
    message.answer.assert_awaited_once_with(ADMIN_REPLY_SENT_ACK)


@pytest.mark.asyncio
async def test_reply_delivery_failure_notifies_admin(valid_settings_kwargs):
    from aiogram.exceptions import TelegramAPIError

    message = _make_message(text=f"/reply_{CLIENT_ID} текст")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    bot = _make_bot()
    bot.send_message = AsyncMock(side_effect=TelegramAPIError(method=MagicMock(), message="blocked"))

    await cmd_reply(message, settings, bot)

    message.answer.assert_awaited_once_with(
        ADMIN_REPLY_DELIVERY_FAILED_MESSAGE.format(user_id=CLIENT_ID)
    )
