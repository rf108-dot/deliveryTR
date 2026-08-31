"""
/help — единая инструкция для обеих ролей (Day 5 hotfix, живой фидбэк) +
универсальный ответ на любое сообщение, не попавшее ни в один другой
хендлер (Day 4 hotfix, живой фидбэк).

Проблема (Day 4): после первого /start Telegram больше никогда не
покажет экран с кнопкой «Start» и описанием бота для этого пользователя
(это одноразовый экран платформы). Значок ≡ (меню команд) — единственная
постоянная подсказка, но не все пользователи его замечают. Раньше любое
сообщение вне активного флоу (например, не в процессе оформления
заказа) просто пропадало без ответа — та же категория багов, что уже
чинили в Day 3-4 (геолокация, телефон текстом).

Проблема (Day 5): даже с описанием бота и универсальным ответом было
неочевидно, что клиент и курьер входят в бота ПО-РАЗНОМУ (/start против
/shift_on) — добавлена явная команда /help с обоими путями.

ВАЖНО: этот роутер должен быть включён в bot.py ПОСЛЕДНИМ
(dp.include_router) — иначе он перехватит сообщения, предназначенные
другим, более специфичным хендлерам. aiogram пробует роутеры по
порядку включения; этот сработает, только если никто раньше не обработал
сообщение. /help — специфичный командный фильтр, поэтому он безопасен
даже в последнем роутере (никакой другой хендлер на эту команду не
подписан, конфликтов по порядку внутри роутера тоже нет — команда
зарегистрирована раньше общего F.text-перехватчика).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from services.sheets import SheetsClient
from texts.ru import FALLBACK_CATEGORIES_BUTTON, FALLBACK_HELP_MESSAGE, HELP_MESSAGE

router = Router(name=__name__)

FALLBACK_CATEGORIES_CALLBACK_DATA = "fallback_categories"


def _fallback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=FALLBACK_CATEGORIES_BUTTON,
                    callback_data=FALLBACK_CATEGORIES_CALLBACK_DATA,
                )
            ]
        ]
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_MESSAGE)


@router.message(F.text)
async def on_unhandled_message(message: Message) -> None:
    await message.answer(FALLBACK_HELP_MESSAGE, reply_markup=_fallback_keyboard())


@router.callback_query(F.data == FALLBACK_CATEGORIES_CALLBACK_DATA)
async def on_fallback_categories(query: CallbackQuery, sheets: SheetsClient) -> None:
    # Локальный импорт: catalog.py не зависит ни от чего, цикла не будет,
    # но откладываем импорт сюда, чтобы этот маленький модуль оставался
    # логически независимым от структуры каталога на уровне модуля.
    from handlers.catalog import show_categories

    await show_categories(query.message, sheets)
    await query.answer()
