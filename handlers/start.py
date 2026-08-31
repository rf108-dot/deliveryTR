"""
Обработчик /start и оферта клиента (ТЗ §7.0, §7.0.1).

Day 0: минимальный ответ (приветствие + баннер).
Day 1: сразу после приветствия — категории (ТЗ §7.1).
Day 4: между приветствием и каталогом встала оферта — показывается один
раз (флаг в FSMContext.data, переживает рестарт бота — Redis с Day 0),
факт принятия дополнительно пишется в лист «События» Google Sheets.

Day 5 hotfix (живой фидбэк, отменяет более раннее решение этого же дня):
изначально /start проверял регистрацию курьера и уводил зарегистрированных
курьеров в отдельный курьерский флоу, минуя клиентский, — это оказалось
неверно: курьер тоже может просто хотеть заказать что-то как обычный
клиент, и /start не должен решать это за него. Теперь /start ВСЕГДА
обычный клиентский флоу, независимо от роли. Вход в курьерский флоу —
только явно, через /shift_on (см. handlers/courier.py, там же теперь и
оферта курьера — гейт стоит перед ПЕРВЫМ включением смены, а не перед
/start).

Архитектурное решение: проверка часов работы («не пускать в
оформление» из ТЗ §7.0.1) НЕ делается здесь, в /start — она относится к
попытке ОФОРМИТЬ заказ, а каталог по ТЗ §6 остаётся смотрибельным даже
когда сервис закрыт («Меню можно смотреть»). Поэтому реальная проверка
часов работы — в handlers/order.py, на кнопке «Оформить заказ». См.
CHECKLIST.md, раздел Day 4, для развёрнутого обоснования этого выбора.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from handlers.catalog import show_categories
from services.sheets import SheetsClient
from texts.ru import (
    OFFER_ACCEPT_BUTTON,
    OFFER_BACK_BUTTON,
    OFFER_FULL_TEXT,
    OFFER_FULL_TEXT_BUTTON,
    OFFER_TEXT,
    START_GREETING,
)

logger = logging.getLogger(__name__)

router = Router(name=__name__)

# Ключ в FSMContext.data — переживает рестарт бота (Redis, Day 0).
OFFER_ACCEPTED_KEY = "offer_accepted"

OFFER_ACCEPT_CALLBACK_DATA = "offer_accept"
OFFER_FULL_TEXT_CALLBACK_DATA = "offer_full_text"
OFFER_BACK_CALLBACK_DATA = "offer_back"


def _offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=OFFER_ACCEPT_BUTTON, callback_data=OFFER_ACCEPT_CALLBACK_DATA
                ),
                InlineKeyboardButton(
                    text=OFFER_FULL_TEXT_BUTTON, callback_data=OFFER_FULL_TEXT_CALLBACK_DATA
                ),
            ]
        ]
    )


def _offer_full_text_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=OFFER_ACCEPT_BUTTON, callback_data=OFFER_ACCEPT_CALLBACK_DATA
                )
            ],
            [InlineKeyboardButton(text=OFFER_BACK_BUTTON, callback_data=OFFER_BACK_CALLBACK_DATA)],
        ]
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message, settings: Settings, sheets: SheetsClient, state: FSMContext
) -> None:
    """/start: приветствие → оферта (если ещё не принята) → каталог.
    Одинаково для всех, независимо от того, зарегистрирован ли этот
    telegram_id ещё и как курьер — см. docstring модуля."""
    user_id = message.from_user.id if message.from_user else None
    logger.info("Получен /start от user_id=%s", user_id)
    await sheets.append_event(
        event_type="bot_start", actor_role="client", actor_id=str(user_id or "")
    )

    if settings.WELCOME_IMAGE_URL:
        await message.answer_photo(photo=settings.WELCOME_IMAGE_URL, caption=START_GREETING)
    else:
        await message.answer(START_GREETING)

    data = await state.get_data()
    if data.get(OFFER_ACCEPTED_KEY):
        await show_categories(message, sheets)
        return

    await message.answer(OFFER_TEXT, reply_markup=_offer_keyboard())


@router.callback_query(F.data == OFFER_ACCEPT_CALLBACK_DATA)
async def on_offer_accept(
    query: CallbackQuery, state: FSMContext, sheets: SheetsClient
) -> None:
    data = await state.get_data()
    already_accepted = bool(data.get(OFFER_ACCEPTED_KEY))
    if not already_accepted:
        await state.update_data(**{OFFER_ACCEPTED_KEY: True})
        user_id = query.from_user.id if query.from_user else None
        await sheets.append_event(
            event_type="offer_accepted", actor_role="client", actor_id=str(user_id or "")
        )

    await query.message.delete()
    await show_categories(query.message, sheets)
    await query.answer()


@router.callback_query(F.data == OFFER_FULL_TEXT_CALLBACK_DATA)
async def on_offer_full_text(query: CallbackQuery) -> None:
    await query.message.edit_text(OFFER_FULL_TEXT, reply_markup=_offer_full_text_keyboard())
    await query.answer()


@router.callback_query(F.data == OFFER_BACK_CALLBACK_DATA)
async def on_offer_back(query: CallbackQuery) -> None:
    await query.message.edit_text(OFFER_TEXT, reply_markup=_offer_keyboard())
    await query.answer()
