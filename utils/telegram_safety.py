"""
Общая защита callback-хендлеров от непредвиденных сбоев (Day 5,
изначально только в handlers/courier.py; Day 6 — вынесено сюда для
переиспользования в handlers/order.py и любых будущих хендлерах).

Два независимых уровня защиты, оба нужны одновременно (одно не
заменяет другое):

1. safe_answer() — обёртка над query.answer(). Если бот долго не
   работал (ноут выключен/спал), Telegram может отдать очень старый
   callback_query после рестарта; попытка на него "ответить" кидает
   TelegramBadRequest ("query is too old and response timeout
   expired..."). К этому моменту вся реальная работа (запись в Sheets,
   уведомления) уже выполнена в хендлере — сбой здесь означает только
   то, что пользователь не увидит всплывающую подсказку, ничего
   критичного не теряется.

2. resilient() — декоратор для ВСЕГО хендлера целиком. Живой случай:
   ноутбук вышел из сна, сеть "выглядит" рабочей, но первый же запрос к
   Google Sheets API кидает низкоуровневый OSError ("Can't assign
   requested address") или SSLError — это НЕ Telegram и НЕ устаревший
   callback (safe_answer тут не срабатывает, исключение происходит
   раньше, до query.answer()). aiogram сам по себе не роняет бот целиком
   при необработанном исключении в хендлере, но конкретное действие
   пользователя просто повисает без ЛЮБОГО ответа — кнопка "крутится",
   непонятно, что происходит. Этот декоратор — последний рубеж: любое
   совсем неожиданное исключение, прорвавшееся мимо точечных try/except
   внутри хендлера, ловится здесь, и пользователь как минимум увидит
   понятный алерт вместо тишины. Уже выполненная к моменту сбоя работа
   не откатывается — это не транзакционность, а просто гарантия ответа.
"""

from __future__ import annotations

import functools
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def safe_answer(query: CallbackQuery, *args, **kwargs) -> None:
    try:
        await query.answer(*args, **kwargs)
    except TelegramBadRequest:
        logger.debug("query.answer() не удался — вероятно, устаревший callback_query")


def resilient(error_message: str):
    """
    Параметризованный декоратор — error_message показывается
    пользователю, если что-то непредвиденное прорвалось мимо всех
    точечных try/except внутри хендлера. Разный текст для разных ролей
    (курьер/клиент), логика защиты — одна и та же.

    Использование: @resilient(MY_TECHNICAL_ERROR_MESSAGE)
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(query: CallbackQuery, *args, **kwargs):
            try:
                await handler(query, *args, **kwargs)
            except Exception:  # noqa: BLE001 — намеренно широкий catch, это последний рубеж
                logger.exception("Необработанная ошибка в %s", handler.__name__)
                await safe_answer(query, error_message, show_alert=True)

        return wrapper

    return decorator
