"""
Вопросы клиента в поддержку (ТЗ §7.5, §11).

Флоу: кнопка [❓ Вопрос] / команда /support → пользователь пишет вопрос
текстом → сообщение с антифлуд-защитой (SUPPORT_FLOOD_MAX_MESSAGES за
SUPPORT_FLOOD_WINDOW_SEC) уходит всем админам с подсказкой команды
/reply_[user_id] [ответ] → админ отвечает этой командой → бот
пересылает ответ клиенту от своего имени.

ТЗ формулирует доступность кнопки как "на всех активных экранах" — в
проекте нет единого механизма постоянной клавиатуры, которая была бы
видна поверх ЛЮБОГО экрана одновременно (см. комментарий у
SUPPORT_CALLBACK_DATA в handlers/catalog.py); прагматичное решение для
MVP — команда /support работает из любого места (кроме тех немногих
шагов в других модулях, что уже перехватывают "любой текст" на своём
шаге ввода — существующее свойство архитектуры, не специфичное для
этого модуля), плюс кнопка на самом часто посещаемом экране (список
категорий).

Антифлуд — простой фиксированный ("fixed window", не "sliding window")
счётчик в Redis: ключ на user_id с TTL = SUPPORT_FLOOD_WINDOW_SEC,
инкрементируется на каждый вопрос; после SUPPORT_FLOOD_MAX_MESSAGES в
пределах этого окна — вопрос НЕ пересылается админу (чтобы не спамить
его), пользователю показывается сообщение "подождите".
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis

from config import Settings
from handlers.catalog import SUPPORT_CALLBACK_DATA
from services.notifications import notify_admins
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

logger = logging.getLogger(__name__)

router = Router(name=__name__)

_FLOOD_KEY_TEMPLATE = "support_flood:{user_id}"


def _contact_label(username: str | None, user_id: int) -> str:
    return f"@{username}" if username else f"ID {user_id}"


async def _check_and_register_question(redis: Redis, settings: Settings, user_id: int) -> bool:
    """Инкрементирует счётчик вопросов пользователя за текущее окно;
    возвращает True, если ещё в пределах лимита (можно пересылать
    админу), False — если лимит уже исчерпан за это окно.

    Fixed window (не sliding): TTL ставится ТОЛЬКО на первый вопрос в
    окне (redis.incr вернёт 1) — все последующие в это же окно просто
    инкрементируют без обновления TTL, поэтому окно отсчитывается от
    ПЕРВОГО вопроса, а не "скользит" от каждого нового. Для антифлуда
    (не точного rate-limiting) этого более чем достаточно."""
    key = _FLOOD_KEY_TEMPLATE.format(user_id=user_id)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, settings.SUPPORT_FLOOD_WINDOW_SEC)
    return count <= settings.SUPPORT_FLOOD_MAX_MESSAGES


# ---------------------------------------------------------------------- #
# Вход
# ---------------------------------------------------------------------- #


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_for_question)
    await message.answer(SUPPORT_QUESTION_PROMPT)


@router.callback_query(F.data == SUPPORT_CALLBACK_DATA)
async def on_support_button(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_for_question)
    await query.message.answer(SUPPORT_QUESTION_PROMPT)
    await query.answer()


@router.message(StateFilter(SupportStates.waiting_for_question), F.text)
async def on_support_question_received(
    message: Message, state: FSMContext, settings: Settings, redis: Redis, bot: Bot
) -> None:
    text = message.text.strip()
    if not text:
        await message.answer(SUPPORT_QUESTION_PROMPT)
        return

    user = message.from_user
    user_id = user.id if user else None
    if user_id is None:
        # Защитный случай — не должен наступать в приватном чате.
        await state.set_state(None)
        return

    await state.set_state(None)

    allowed = await _check_and_register_question(redis, settings, user_id)
    if not allowed:
        minutes = max(1, settings.SUPPORT_FLOOD_WINDOW_SEC // 60)
        await message.answer(SUPPORT_FLOOD_LIMIT_MESSAGE_TEMPLATE.format(minutes=minutes))
        logger.info("support_question_flood_limited: user_id=%s", user_id)
        return

    await message.answer(SUPPORT_QUESTION_SUBMITTED_ACK)
    logger.info("support_question: user_id=%s", user_id)

    contact = _contact_label(user.username, user_id)
    admin_text = ADMIN_SUPPORT_QUESTION_TEMPLATE.format(
        contact=contact, question=text, user_id=user_id
    )
    await notify_admins(bot, settings, admin_text)


# ---------------------------------------------------------------------- #
# Команда Админа: /reply_[user_id] [текст]
# ---------------------------------------------------------------------- #


@router.message(F.text.startswith("/reply_"))
async def cmd_reply(message: Message, settings: Settings, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    rest = (message.text or "")[len("/reply_"):]
    parts = rest.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return
    target_user_id = int(parts[0])
    reply_text = parts[1].strip() if len(parts) > 1 else ""

    if not reply_text:
        await message.answer(ADMIN_REPLY_EMPTY_MESSAGE.format(user_id=target_user_id))
        return

    try:
        await bot.send_message(
            target_user_id, CLIENT_SUPPORT_REPLY_TEMPLATE.format(text=reply_text)
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось доставить ответ поддержки клиенту %s: %s", target_user_id, exc)
        await message.answer(ADMIN_REPLY_DELIVERY_FAILED_MESSAGE.format(user_id=target_user_id))
        return

    logger.info(
        "support_reply_sent: admin_id=%s target_user_id=%s", message.from_user.id, target_user_id
    )
    await message.answer(ADMIN_REPLY_SENT_ACK)
