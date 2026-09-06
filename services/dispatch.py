"""
Offer-based диспетчеризация заказов курьерам, атомарный захват заказа
(ТЗ §8.2).

Day 5: 1 круг рассылки — заказ пушится ОДНОВРЕМЕННО всем курьерам с
on_shift=TRUE, первый нажавший «Взять» забирает атомарно (Redis SET NX —
единственная гарантированно атомарная операция для этого на уровне
Redis, без нужды в Lua-скриптах или ручных блокировках). Проигравшим
сообщениям — правка на «уже принято».

ВАЖНО, что явно НЕ входит в этот день (см. ТЗ §17, план: Day 6 =
«тайм-ауты застрявших заказов + APScheduler»): автоматический перевод
в no_courier по истечении OFFER_TIMEOUT_SEC, если никто не принял, —
этот таймер требует персистентного планировщика (APScheduler), которого
пока нет (scheduler.py — каркас без задач, см. Day 0/README). Заказ
может повиснуть в статусе "offered" сколь угодно долго, если никто не
нажал «Взять» — до Day 6 это не автоматизировано.

Единственный СИНХРОННЫЙ случай, обработанный уже сейчас: если на смене
СЕЙЧАС вообще нет ни одного курьера — это известно немедленно, без
таймера, поэтому эскалация Админу происходит сразу, до всякой рассылки
(статус сразу "no_courier", минуя "offered" — рассылать было некому).
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis

from config import Settings
from services.notifications import notify_admins
from services.sheets import SheetsClient
from services.timeouts import schedule_offer_timeout
from texts.ru import (
    ADMIN_NO_COURIERS_ON_SHIFT_TEMPLATE,
    COURIER_OFFER_CUSTOM_TEMPLATE,
    COURIER_OFFER_EXPIRED_MESSAGE,
    COURIER_OFFER_P2P_TEMPLATE,
    COURIER_OFFER_TEMPLATE,
    COURIER_ORDER_ALREADY_TAKEN_MESSAGE,
    COURIER_TAKE_ORDER_BUTTON,
)
from utils.timefmt import now_local_str

logger = logging.getLogger(__name__)

_CAPTURE_KEY_TEMPLATE = "orders:capture:{order_id}"
_OFFER_MESSAGES_KEY_TEMPLATE = "orders:offer_msgs:{order_id}"
# 6 часов — с большим запасом на весь рабочий день; данные нужны только
# на время жизни одного раунда диспетчеризации, дальше это просто мусор
# в Redis, если не выставить TTL.
_OFFER_MESSAGES_TTL_SECONDS = 6 * 60 * 60

# Экспортируется для handlers/courier.py — фильтр callback_query по
# префиксу, без завязки на класс CallbackData (избегаем циклического
# импорта dispatch.py <-> courier.py: dispatch не может импортировать
# определения из courier.py, т.к. courier.py уже импортирует функции
# ИЗ dispatch.py для обработки "Взять").
TAKE_ORDER_CALLBACK_PREFIX = "courier_take:"


def take_order_callback_data(order_id: str) -> str:
    return f"{TAKE_ORDER_CALLBACK_PREFIX}{order_id}"


async def try_capture_order(redis: Redis, order_id: str, courier_id: str) -> bool:
    """
    Атомарный захват (ТЗ §8.2): SET ... NX — гарантированно только
    ПЕРВЫЙ вызов для данного order_id вернёт True, даже при гонке двух
    курьеров, нажавших «Взять» практически одновременно (это единственная
    атомарная на уровне Redis операция для паттерна "первый пришёл —
    забрал", без риска состояния гонки между "прочитать" и "записать").
    """
    key = _CAPTURE_KEY_TEMPLATE.format(order_id=order_id)
    was_set = await redis.set(key, courier_id, nx=True)
    return bool(was_set)


async def get_capturing_courier_id(redis: Redis, order_id: str) -> str | None:
    """
    Кто именно держит атомарный захват заказа (или None, если не
    захвачен никем). Живой фидбэк: при сетевой задержке курьер мог не
    увидеть мгновенного ответа на первый клик "Взять" и нажать её ещё
    раз — второй try_capture_order() корректно вернёт False (замок уже
    занят), но БЕЗ этой функции невозможно отличить "занял кто-то
    другой" от "это тот же курьер, просто повторный клик" — а разница
    принципиальна для того, какое сообщение показать (см.
    handlers/courier.py on_take_order).
    """
    key = _CAPTURE_KEY_TEMPLATE.format(order_id=order_id)
    value = await redis.get(key)
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


async def release_capture(redis: Redis, order_id: str) -> None:
    """
    Откат атомарного захвата — живой фидбэк: если ПОСЛЕ успешного
    try_capture_order дальнейшая обработка (чтение/запись заказа в
    Sheets, уведомления) упала с исключением — например, тот же
    сетевой сбой, что уже не раз ловили в этом проекте — Redis-замок
    оставался висеть НАВСЕГДА. Повторный клик того же курьера (по
    совету из сообщения об ошибке "попробуйте ещё раз") получал «заказ
    уже принят другим курьером» — хотя это была та же самая попытка,
    просто первая не успела полностью завершиться. handlers/courier.py
    вызывает это в except-блоке вокруг всей пост-захватной логики.
    """
    key = _CAPTURE_KEY_TEMPLATE.format(order_id=order_id)
    await redis.delete(key)


async def record_offer_message(
    redis: Redis, order_id: str, telegram_id: str, message_id: int
) -> None:
    key = _OFFER_MESSAGES_KEY_TEMPLATE.format(order_id=order_id)
    await redis.hset(key, telegram_id, str(message_id))
    await redis.expire(key, _OFFER_MESSAGES_TTL_SECONDS)


async def get_offer_message_ids(redis: Redis, order_id: str) -> dict[str, int]:
    key = _OFFER_MESSAGES_KEY_TEMPLATE.format(order_id=order_id)
    raw = await redis.hgetall(key)
    return {k.decode("utf-8"): int(v) for k, v in raw.items()}


async def notify_other_couriers_order_taken(
    bot: Bot, redis: Redis, order_id: str, winning_telegram_id: str
) -> None:
    """
    Убирает предложение (правит текст; клавиатура исчезает вместе с ним,
    т.к. edit_message_text без reply_markup убирает кнопки) у всех
    курьеров, кроме победившего — тот получает своё отдельное сообщение
    (см. handlers/courier.py, on_take_order).
    """
    message_ids = await get_offer_message_ids(redis, order_id)
    for telegram_id, message_id in message_ids.items():
        if telegram_id == winning_telegram_id:
            continue
        try:
            await bot.edit_message_text(
                chat_id=int(telegram_id),
                message_id=message_id,
                text=COURIER_ORDER_ALREADY_TAKEN_MESSAGE,
            )
        except TelegramBadRequest:
            # Курьер мог удалить чат/заблокировать бота — не критично,
            # это просто чистка UI, не влияет на корректность заказа.
            logger.debug(
                "Не удалось обновить сообщение курьеру %s (заказ %s)", telegram_id, order_id
            )


async def clear_expired_offer_for_all_couriers(bot: Bot, redis: Redis, order_id: str) -> None:
    """
    Зеркало notify_other_couriers_order_taken для случая, когда НИКТО
    не принял заказ (offer_timeout истёк, статус → no_courier, ТЗ §9) —
    живой баг из тестирования: раньше кнопка «Взять» оставалась
    визуально активной у всех курьеров и после истечения оффера
    (функционально безопасно — повторное нажатие корректно отклонялось
    как «кнопка устарела», см. handlers/courier.py::on_take_order, но
    вводило в заблуждение). Здесь, в отличие от
    notify_other_couriers_order_taken, победителя нет — убираем кнопку
    у ВСЕХ курьеров без исключения. Вызывается из
    services/timeouts.py::_check_offer_timeout.
    """
    message_ids = await get_offer_message_ids(redis, order_id)
    for telegram_id, message_id in message_ids.items():
        try:
            await bot.edit_message_text(
                chat_id=int(telegram_id),
                message_id=message_id,
                text=COURIER_OFFER_EXPIRED_MESSAGE,
            )
        except TelegramBadRequest:
            logger.debug(
                "Не удалось обновить сообщение курьеру %s (заказ %s, offer_timeout)",
                telegram_id,
                order_id,
            )


async def send_offer_to_couriers(
    bot: Bot,
    sheets: SheetsClient,
    redis: Redis,
    settings: Settings,
    scheduler: AsyncIOScheduler,
    *,
    order_id: str,
    merchant_name: str,
    merchant_description: str,
    delivery_address: str,
    total_try: str,
    custom_description: str | None = None,
    p2p_pickup_address: str | None = None,
    p2p_dropoff_address: str | None = None,
    p2p_description: str | None = None,
) -> None:
    """
    new → offered (ТЗ §8.2). Пушит предложение ВСЕМ курьерам с
    on_shift=TRUE одновременно. Если на смене никого — синхронная
    эскалация Админу вместо перехода в offered (см. docstring модуля).

    custom_description — заполняется только для order_kind="custom"
    (ТЗ §7.3.4, handlers/custom_order.py): у таких заказов нет
    каталожной суммы total_try (позиции нет в меню мерчанта), зато
    курьеру обязательно нужно видеть текст запроса — используется
    отдельная карточка COURIER_OFFER_CUSTOM_TEMPLATE вместо стандартной.

    p2p_pickup_address/p2p_dropoff_address/p2p_description — заполняются
    только для order_kind="p2p" (ТЗ §7.6.8, handlers/p2p.py) после
    одобрения Админом: своя карточка COURIER_OFFER_P2P_TEMPLATE без
    мерчанта вообще, с точками А/Б.
    """
    couriers = await sheets.get_couriers(force_refresh=True)
    on_shift = [c for c in couriers if c.is_available_for_dispatch]

    if not on_shift:
        await sheets.update_order_fields(order_id, {"status": "no_courier"})
        await sheets.append_event(
            event_type="stuck_order_escalated",
            actor_role="system",
            actor_id="dispatch",
            order_id=order_id,
            details="0 курьеров на смене",
        )
        await notify_admins(
            bot, settings, ADMIN_NO_COURIERS_ON_SHIFT_TEMPLATE.format(order_id=order_id)
        )
        return

    await sheets.update_order_fields(
        order_id, {"status": "offered", "timestamp_offered": now_local_str(settings)}
    )

    if p2p_pickup_address is not None:
        text = COURIER_OFFER_P2P_TEMPLATE.format(
            order_id=order_id,
            pickup_address=p2p_pickup_address,
            dropoff_address=p2p_dropoff_address or "",
            description=p2p_description or "",
            offer_timeout_sec=settings.OFFER_TIMEOUT_SEC,
        )
    elif custom_description:
        text = COURIER_OFFER_CUSTOM_TEMPLATE.format(
            order_id=order_id,
            merchant_name=merchant_name,
            merchant_description=merchant_description,
            custom_description=custom_description,
            delivery_address=delivery_address,
            offer_timeout_sec=settings.OFFER_TIMEOUT_SEC,
        )
    else:
        text = COURIER_OFFER_TEMPLATE.format(
            order_id=order_id,
            merchant_name=merchant_name,
            merchant_description=merchant_description,
            delivery_address=delivery_address,
            total_try=total_try,
            offer_timeout_sec=settings.OFFER_TIMEOUT_SEC,
        )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=COURIER_TAKE_ORDER_BUTTON,
                    callback_data=take_order_callback_data(order_id),
                )
            ]
        ]
    )

    sent_count = 0
    for courier in on_shift:
        try:
            sent = await bot.send_message(int(courier.telegram_id), text, reply_markup=keyboard)
        except TelegramBadRequest:
            logger.warning("Не удалось отправить предложение курьеру %s", courier.telegram_id)
            continue
        await record_offer_message(redis, order_id, courier.telegram_id, sent.message_id)
        sent_count += 1

    await sheets.append_event(
        event_type="courier_offer_sent",
        actor_role="system",
        actor_id="dispatch",
        order_id=order_id,
        details=f"couriers={sent_count}",
    )

    # Day 6 (ТЗ §9): если никто не нажмёт «Взять» за OFFER_TIMEOUT_SEC —
    # offered → no_courier, Админу «⚠️ Никто не взял #042». Отменяется в
    # handlers/courier.py on_take_order при своевременном захвате.
    schedule_offer_timeout(scheduler, order_id, settings.OFFER_TIMEOUT_SEC)
