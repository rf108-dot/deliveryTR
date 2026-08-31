"""
Курьерский модуль: статус смены (on/off), приём предложений заказов,
подтверждение этапов (забрал/доставил/проблема на точке).

Day 5 (ТЗ §8):
  §8.1 /shift_on, /shift_off — команды И персистентная reply-кнопка
       (тапнуть — то же самое, что напечатать команду).
  §8.2 «✅ Взять» — атомарный захват через services/dispatch.py
       (try_capture_order, Redis SET NX). Проигравшим — правка сообщения
       на «уже принято», победителю — карточка активного заказа с
       кнопками этапов.
  §8.3 «📦 Забрал заказ» / «✅ Доставил» / «⚠️ Проблема на точке» —
       переходы статуса, уведомления клиенту/админу.

Роль определяется по ID (ТЗ §2): все хендлеры здесь проверяют, что
вызывающий — зарегистрированный, активный курьер (лист «Курьеры»,
документ «Заказы»); прочим — вежливый отказ с подсказкой обратиться к
администратору. Это ОТЛИЧАЕТСЯ от handlers/admin.py (там не-админам —
полное молчание): быть курьером не настолько чувствительная роль, чтобы
скрывать сам факт существования команд, а отказ полезнее тишины — он
подсказывает человеку, что делать дальше.

FSM-состояния не понадобились (states/courier_states.py остался
пустым каркасом) — все действия здесь однократные (команда/нажатие
кнопки с order_id, зашитым в callback_data через префикс), не
многошаговые диалоги, в отличие от чекаута клиента (Day 3).

ВАЖНАЯ ЗАЩИТА, которой здесь НЕТ (осознанное упрощение Day 5): после
атомарного захвата не делается повторной проверки «а точно ли курьер,
нажимающий «Забрал»/«Доставил», — тот самый, что взял заказ». Это
опирается на то, что карточку активного заказа с этими кнопками
получает ТОЛЬКО победивший курьер (Telegram-сообщения приватны для
чата, кнопки в принципе недоступны никому другому) — «безопасность
через архитектуру доставки сообщений», а не через дополнительную
сверку. Если понадобится дополнительная строгость — легко добавить
сверку courier_id в get_order() перед переходом.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from redis.asyncio import Redis

from config import Settings
from handlers.order import ORDER_AGAIN_CALLBACK_DATA
from services.dispatch import (
    TAKE_ORDER_CALLBACK_PREFIX,
    get_capturing_courier_id,
    notify_other_couriers_order_taken,
    release_capture,
    try_capture_order,
)
from services.notifications import notify_admins
from services.sheets import Courier, SheetsClient, find_courier_by_telegram_id
from services.timeouts import (
    cancel_delivery_timeout,
    cancel_offer_timeout,
    cancel_pickup_timeout,
    schedule_delivery_timeout,
    schedule_pickup_timeout,
)
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
    COURIER_ACTIVE_ORDER_TEMPLATE,
    COURIER_DELIVERED_ACK,
    COURIER_DELIVERED_BUTTON,
    COURIER_DELIVERY_THANKS_MESSAGE,
    COURIER_MUST_PICK_UP_FIRST_MESSAGE,
    COURIER_NOT_REGISTERED_MESSAGE,
    COURIER_OFFER_TEXT,
    COURIER_ORDER_ALREADY_TAKEN_MESSAGE,
    COURIER_ORDER_NOT_FOUND_MESSAGE,
    COURIER_ORDER_PAUSED_MESSAGE,
    COURIER_ORDER_TAKEN_BY_YOU_TEMPLATE,
    COURIER_PICKED_UP_ACK,
    COURIER_PICKED_UP_BUTTON,
    COURIER_PROBLEM_ACK,
    COURIER_PROBLEM_BUTTON,
    COURIER_SHIFT_OFF_ACK,
    COURIER_SHIFT_OFF_BUTTON,
    COURIER_SHIFT_ON_ACK,
    COURIER_TECHNICAL_ERROR_MESSAGE,
    COURIER_SHIFT_ON_BUTTON,
    OFFER_ACCEPT_BUTTON,
    ORDER_AGAIN_BUTTON,
)
from utils.timefmt import now_local_str

logger = logging.getLogger(__name__)

router = Router(name=__name__)

PICKED_UP_CALLBACK_PREFIX = "courier_picked_up:"
DELIVERED_CALLBACK_PREFIX = "courier_delivered:"
PROBLEM_CALLBACK_PREFIX = "courier_problem:"


# ---------------------------------------------------------------------- #
# Помощники
# ---------------------------------------------------------------------- #


def build_shift_keyboard(on_shift: bool) -> ReplyKeyboardMarkup:
    """Персистентная кнопка-переключатель (ТЗ §8.1) — показывает
    ПРОТИВОПОЛОЖНОЕ текущему состоянию действие: если сейчас на смене,
    кнопка предлагает завершить её, и наоборот."""
    label = COURIER_SHIFT_OFF_BUTTON if on_shift else COURIER_SHIFT_ON_BUTTON
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=label)]], resize_keyboard=True)


def _active_order_keyboard(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=COURIER_PICKED_UP_BUTTON,
                    callback_data=f"{PICKED_UP_CALLBACK_PREFIX}{order_id}",
                ),
                InlineKeyboardButton(
                    text=COURIER_DELIVERED_BUTTON,
                    callback_data=f"{DELIVERED_CALLBACK_PREFIX}{order_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=COURIER_PROBLEM_BUTTON,
                    callback_data=f"{PROBLEM_CALLBACK_PREFIX}{order_id}",
                )
            ],
        ]
    )


from utils.telegram_safety import resilient
from utils.telegram_safety import safe_answer as _safe_answer

_resilient = lambda handler: resilient(COURIER_TECHNICAL_ERROR_MESSAGE)(handler)  # noqa: E731


async def _get_courier(sheets: SheetsClient, telegram_id: int) -> Courier | None:
    """Зарегистрированный И активный курьер, либо None. is_registered_legal
    сознательно не проверяется здесь — см. Courier.is_available_for_dispatch
    в services/sheets.py.

    force_refresh=True: живой фидбэк — админ регистрирует курьера в
    таблице и сразу же просит его попробовать /shift_on; без этого
    5-минутный кеш мог бы вернуть «курьер не найден» ещё какое-то время
    после того, как запись уже добавлена. Это не hot path (в отличие от
    просмотра меню), лишние запросы к Sheets не страшны."""
    couriers = await sheets.get_couriers(force_refresh=True)
    courier = find_courier_by_telegram_id(couriers, str(telegram_id))
    if courier is None or not courier.is_active:
        return None
    return courier


# ---------------------------------------------------------------------- #
# §8.1: смена (оферта курьера — гейт перед ПЕРВЫМ включением смены;
# живой фидбэк Day 5: раньше гейт стоял на /start и уводил курьера от
# обычного клиентского флоу принудительно — курьер тоже может просто
# хотеть что-то заказать, /start не должен решать это за него)
# ---------------------------------------------------------------------- #

COURIER_OFFER_ACCEPTED_KEY = "courier_offer_accepted"
COURIER_OFFER_ACCEPT_CALLBACK_DATA = "courier_offer_accept"


def _courier_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=OFFER_ACCEPT_BUTTON, callback_data=COURIER_OFFER_ACCEPT_CALLBACK_DATA
                )
            ]
        ]
    )


async def _activate_shift(
    message: Message, courier: Courier, settings: Settings, sheets: SheetsClient, bot: Bot
) -> None:
    """Собственно включение смены — общий код для прямого /shift_on
    (оферта уже принята раньше) и для пути «принял оферту → сразу
    включилась смена» (on_courier_offer_accept)."""
    await sheets.update_courier_on_shift(courier.telegram_id, True)
    logger.info("courier_shift: courier_id=%s on_shift=True", courier.courier_id)
    await message.answer(COURIER_SHIFT_ON_ACK, reply_markup=build_shift_keyboard(True))
    await notify_admins(bot, settings, ADMIN_COURIER_ON_SHIFT_TEMPLATE.format(name=courier.name))


async def _handle_shift_on(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot, state: FSMContext
) -> None:
    if not message.from_user:
        return
    courier = await _get_courier(sheets, message.from_user.id)
    if courier is None:
        await message.answer(
            COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=message.from_user.id)
        )
        return

    data = await state.get_data()
    if not data.get(COURIER_OFFER_ACCEPTED_KEY):
        await message.answer(COURIER_OFFER_TEXT, reply_markup=_courier_offer_keyboard())
        return

    await _activate_shift(message, courier, settings, sheets, bot)


async def _handle_shift_off(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot
) -> None:
    """Выключение смены не гейтится офертой — выключить можно всегда,
    без выяснения, была ли оферта уже принята (нет сценария, где
    выключение смены нужно было бы дополнительно защищать)."""
    if not message.from_user:
        return
    courier = await _get_courier(sheets, message.from_user.id)
    if courier is None:
        await message.answer(
            COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=message.from_user.id)
        )
        return

    await sheets.update_courier_on_shift(courier.telegram_id, False)
    logger.info("courier_shift: courier_id=%s on_shift=False", courier.courier_id)
    await message.answer(COURIER_SHIFT_OFF_ACK, reply_markup=build_shift_keyboard(False))
    await notify_admins(bot, settings, ADMIN_COURIER_OFF_SHIFT_TEMPLATE.format(name=courier.name))


@router.message(Command("shift_on"))
async def cmd_shift_on(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot, state: FSMContext
) -> None:
    await _handle_shift_on(message, settings, sheets, bot, state)


@router.message(Command("shift_off"))
async def cmd_shift_off(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot
) -> None:
    await _handle_shift_off(message, settings, sheets, bot)


@router.message(F.text == COURIER_SHIFT_ON_BUTTON)
async def on_shift_on_button(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot, state: FSMContext
) -> None:
    await _handle_shift_on(message, settings, sheets, bot, state)


@router.message(F.text == COURIER_SHIFT_OFF_BUTTON)
async def on_shift_off_button(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot
) -> None:
    await _handle_shift_off(message, settings, sheets, bot)


@router.callback_query(F.data == COURIER_OFFER_ACCEPT_CALLBACK_DATA)
@_resilient
async def on_courier_offer_accept(
    query: CallbackQuery, state: FSMContext, sheets: SheetsClient, settings: Settings, bot: Bot
) -> None:
    if not query.from_user:
        await _safe_answer(query)
        return
    courier = await _get_courier(sheets, query.from_user.id)
    if courier is None:
        await _safe_answer(query, 
            COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=query.from_user.id),
            show_alert=True,
        )
        return

    data = await state.get_data()
    if not data.get(COURIER_OFFER_ACCEPTED_KEY):
        await state.update_data(**{COURIER_OFFER_ACCEPTED_KEY: True})
        await sheets.append_event(
            event_type="courier_offer_accepted",
            actor_role="courier",
            actor_id=courier.courier_id,
        )

    await query.message.delete()
    await _activate_shift(query.message, courier, settings, sheets, bot)
    await _safe_answer(query)


# ---------------------------------------------------------------------- #
# §8.2: приём предложения ("Взять")
# ---------------------------------------------------------------------- #


@router.callback_query(F.data.startswith(TAKE_ORDER_CALLBACK_PREFIX))
@_resilient
async def on_take_order(
    query: CallbackQuery,
    sheets: SheetsClient,
    redis: Redis,
    settings: Settings,
    bot: Bot,
    scheduler: AsyncIOScheduler,
) -> None:
    order_id = query.data.removeprefix(TAKE_ORDER_CALLBACK_PREFIX)

    if not query.from_user:
        await _safe_answer(query)
        return
    courier = await _get_courier(sheets, query.from_user.id)
    if courier is None:
        await _safe_answer(query, 
            COURIER_NOT_REGISTERED_MESSAGE.format(telegram_id=query.from_user.id),
            show_alert=True,
        )
        return

    won = await try_capture_order(redis, order_id, courier.courier_id)
    if not won:
        existing_courier_id = await get_capturing_courier_id(redis, order_id)
        if existing_courier_id == courier.courier_id:
            # Живой фидбэк: тот же курьер повторно нажал "Взять" —
            # скорее всего, из-за сетевой задержки на первом клике не
            # увидел мгновенного ответа. Это НЕ "занято другим" — просто
            # повторно показываем текущее состояние активного заказа,
            # не пугая ложным "уже принят другим курьером".
            order = await sheets.get_order(order_id)
            merchant_name = order.get("merchant_name", "") if order else ""
            delivery_address = order.get("delivery_address", "") if order else ""
            await _safe_answer(query)
            await query.message.answer(
                COURIER_ACTIVE_ORDER_TEMPLATE.format(
                    order_id=order_id,
                    merchant_name=merchant_name,
                    delivery_address=delivery_address,
                ),
                reply_markup=_active_order_keyboard(order_id),
            )
            return

        await _safe_answer(query, COURIER_ORDER_ALREADY_TAKEN_MESSAGE, show_alert=True)
        # Защитная правка своего же сообщения на случай, если рассылка от
        # победителя (notify_other_couriers_order_taken) ещё не долетела.
        try:
            await query.message.edit_text(COURIER_ORDER_ALREADY_TAKEN_MESSAGE)
        except TelegramBadRequest:
            pass
        return

    # Всё, что ниже, обёрнуто в try/except с откатом Redis-замка при
    # любом сбое (см. release_capture в services/dispatch.py) — живой
    # фидбэк: без этого сбой ПОСЛЕ успешного захвата (например, тот же
    # сетевой сбой, что уже не раз ловили в этом проекте) оставлял
    # Redis-замок висеть навсегда — повторный клик того же курьера
    # получал "заказ уже принят другим курьером", хотя это была та же
    # самая попытка, просто первая не успела полностью завершиться.
    try:
        # Живой фидбэк: атомарный захват в Redis сам по себе НЕ проверяет
        # актуальность статуса в Sheets — заказ мог уже эскалироваться в
        # no_courier (истёк OFFER_TIMEOUT_SEC) ровно между тем, как курьер
        # увидел кнопку "Взять", и моментом клика. Без этой проверки заказ
        # молча "воскресал" обратно в assigned поверх уже случившейся
        # эскалации "Никто не взял" — админ получал противоречивую картину.
        order = await sheets.get_order(order_id)
        if order is None or order.get("status") != "offered":
            await _safe_answer(query, COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)
            try:
                await query.message.edit_text(COURIER_ORDER_NOT_FOUND_MESSAGE)
            except TelegramBadRequest:
                pass
            await release_capture(redis, order_id)
            return

        await sheets.update_order_fields(
            order_id,
            {
                "status": "assigned",
                "courier_id": courier.courier_id,
                "courier_name": courier.name,
                "courier_phone": courier.phone,
                "timestamp_assigned": now_local_str(settings),
            },
        )
        logger.info(
            "courier_accepted: order_id=%s courier_id=%s", order_id, courier.courier_id
        )
        await sheets.append_event(
            event_type="courier_accepted",
            actor_role="courier",
            actor_id=courier.courier_id,
            order_id=order_id,
        )

        # Day 6 (ТЗ §9): вовремя взяли — offer_timeout больше не нужен;
        # теперь считаем pickup_timeout ("взял, но не забрал").
        cancel_offer_timeout(scheduler, order_id)
        schedule_pickup_timeout(scheduler, order_id, settings.PICKUP_TIMEOUT_MIN)

        await notify_other_couriers_order_taken(bot, redis, order_id, courier.telegram_id)

        try:
            await query.message.edit_text(
                COURIER_ORDER_TAKEN_BY_YOU_TEMPLATE.format(order_id=order_id)
            )
        except TelegramBadRequest:
            pass

        order = await sheets.get_order(order_id)
        merchant_name = order.get("merchant_name", "") if order else ""
        delivery_address = order.get("delivery_address", "") if order else ""
        client_user_id = order.get("user_id", "") if order else ""
        await query.message.answer(
            COURIER_ACTIVE_ORDER_TEMPLATE.format(
                order_id=order_id, merchant_name=merchant_name, delivery_address=delivery_address
            ),
            reply_markup=_active_order_keyboard(order_id),
        )
    except Exception:
        await release_capture(redis, order_id)
        raise

    # Живой фидбэк: клиент раньше не получал вообще ничего на этапе
    # "курьер принял заказ" — только на "забрал"/"доставил". items_text
    # берём из admin_notes (уже читаемая строка, см. Day 4 hotfix), а не
    # заново разбираем технический items_json.
    if client_user_id:
        items_text = order.get("admin_notes", "")
        try:
            await bot.send_message(
                int(client_user_id),
                CLIENT_ORDER_ASSIGNED_TEMPLATE.format(
                    courier_name=courier.name, items=items_text, merchant_name=merchant_name
                ),
            )
        except (TelegramBadRequest, ValueError):
            logger.warning(
                "Не удалось уведомить клиента %s о назначении курьера (заказ %s)",
                client_user_id,
                order_id,
            )

    await notify_admins(
        bot,
        settings,
        ADMIN_COURIER_ASSIGNED_TEMPLATE.format(order_id=order_id, courier_name=courier.name),
        exclude_user_id=client_user_id or None,
    )
    await _safe_answer(query)


# ---------------------------------------------------------------------- #
# §8.3: этапы активного заказа
# ---------------------------------------------------------------------- #


@router.callback_query(F.data.startswith(PICKED_UP_CALLBACK_PREFIX))
@_resilient
async def on_picked_up(
    query: CallbackQuery, sheets: SheetsClient, settings: Settings, bot: Bot, scheduler: AsyncIOScheduler
) -> None:
    order_id = query.data.removeprefix(PICKED_UP_CALLBACK_PREFIX)
    order = await sheets.get_order(order_id)
    if order is None:
        await _safe_answer(query, COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)
        return

    await sheets.update_order_fields(
        order_id, {"status": "picked_up", "timestamp_picked_up": now_local_str(settings)}
    )
    logger.info("courier_picked_up: order_id=%s", order_id)
    await sheets.append_event(
        event_type="courier_picked_up",
        actor_role="courier",
        actor_id=order.get("courier_id", ""),
        order_id=order_id,
    )

    # Day 6 (ТЗ §9): вовремя забрал — pickup_timeout больше не нужен;
    # теперь считаем delivery_timeout ("забрал, но не доставил").
    cancel_pickup_timeout(scheduler, order_id)
    schedule_delivery_timeout(scheduler, order_id, settings.DELIVERY_TIMEOUT_MIN)

    user_id = order.get("user_id", "")
    await notify_admins(
        bot,
        settings,
        ADMIN_ORDER_PICKED_UP_TEMPLATE.format(order_id=order_id),
        exclude_user_id=user_id or None,
    )

    if user_id:
        try:
            await bot.send_message(
                int(user_id),
                CLIENT_ORDER_PICKED_UP_TEMPLATE.format(
                    courier_name=order.get("courier_name", ""),
                    courier_phone=order.get("courier_phone", ""),
                ),
            )
        except (TelegramBadRequest, ValueError):
            logger.warning("Не удалось уведомить клиента %s о заказе %s", user_id, order_id)

    await _safe_answer(query, COURIER_PICKED_UP_ACK)


@router.callback_query(F.data.startswith(DELIVERED_CALLBACK_PREFIX))
@_resilient
async def on_delivered(
    query: CallbackQuery, sheets: SheetsClient, settings: Settings, bot: Bot, scheduler: AsyncIOScheduler
) -> None:
    order_id = query.data.removeprefix(DELIVERED_CALLBACK_PREFIX)
    order = await sheets.get_order(order_id)
    if order is None:
        await _safe_answer(query, COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)
        return
    if order.get("status") != "picked_up":
        # По запросу: строгая последовательность — "Доставил" доступен
        # только ПОСЛЕ "Забрал заказ" (в отличие от "Проблема на точке",
        # которая по отдельному запросу разрешена и до "Забрал" — см.
        # on_problem ниже, там такого гейта уже нет).
        await _safe_answer(query, COURIER_MUST_PICK_UP_FIRST_MESSAGE, show_alert=True)
        return

    await sheets.update_order_fields(
        order_id, {"status": "delivered", "timestamp_delivered": now_local_str(settings)}
    )
    logger.info("courier_delivered: order_id=%s", order_id)
    await sheets.append_event(
        event_type="courier_delivered",
        actor_role="courier",
        actor_id=order.get("courier_id", ""),
        order_id=order_id,
    )
    cancel_delivery_timeout(scheduler, order_id)  # Day 6 (ТЗ §9): доставлено вовремя

    await notify_admins(
        bot,
        settings,
        ADMIN_ORDER_DELIVERED_TEMPLATE.format(order_id=order_id),
        exclude_user_id=order.get("user_id", "") or None,
    )

    user_id = order.get("user_id", "")
    if user_id:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=ORDER_AGAIN_BUTTON, callback_data=ORDER_AGAIN_CALLBACK_DATA
                    )
                ]
            ]
        )
        try:
            await bot.send_message(
                int(user_id), CLIENT_ORDER_DELIVERED_TEMPLATE, reply_markup=keyboard
            )
        except (TelegramBadRequest, ValueError):
            logger.warning(
                "Не удалось уведомить клиента %s о доставке заказа %s", user_id, order_id
            )

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    # По запросу: после "Доставил" курьер должен явно увидеть, что он
    # всё ещё на смене и готов брать новые заказы — та же карточка
    # статуса, что и при /shift_on, с той же кнопкой "Уйти со смены".
    # on_shift в Sheets не менялся этим переходом (только статус
    # ЗАКАЗА, не курьера) — курьер и был, и остаётся на смене.
    if query.from_user:
        await query.message.answer(
            f"{COURIER_DELIVERY_THANKS_MESSAGE}\n\n{COURIER_SHIFT_ON_ACK}",
            reply_markup=build_shift_keyboard(True),
        )

    await _safe_answer(query, COURIER_DELIVERED_ACK)


@router.callback_query(F.data.startswith(PROBLEM_CALLBACK_PREFIX))
@_resilient
async def on_problem(
    query: CallbackQuery, sheets: SheetsClient, settings: Settings, bot: Bot, scheduler: AsyncIOScheduler
) -> None:
    order_id = query.data.removeprefix(PROBLEM_CALLBACK_PREFIX)
    order = await sheets.get_order(order_id)
    if order is None:
        await _safe_answer(query, COURIER_ORDER_NOT_FOUND_MESSAGE, show_alert=True)
        return
    # По запросу: "Проблема на точке" доступна и ДО "Забрал заказ" —
    # проблема (заведение закрыто, нет позиции, не может найти точку и
    # т.п.) вполне может возникнуть ещё до фактического получения
    # заказа. Гейт по статусу picked_up остаётся ТОЛЬКО у "Доставил" —
    # см. on_delivered ниже.

    await sheets.update_order_fields(order_id, {"status": "merchant_problem"})
    logger.info("merchant_problem: order_id=%s", order_id)
    await sheets.append_event(
        event_type="merchant_problem",
        actor_role="courier",
        actor_id=order.get("courier_id", ""),
        order_id=order_id,
    )
    # Day 6 (ТЗ §9): заказ поставлен на паузу — отменяем ОБА возможных
    # таймера (pickup/delivery), неважно, какой из них реально был
    # активен на момент нажатия "Проблема" (до или после "Забрал");
    # отмена несуществующего задания безопасна (см. services/timeouts.py
    # _cancel — тихо игнорирует отсутствие).
    cancel_pickup_timeout(scheduler, order_id)
    cancel_delivery_timeout(scheduler, order_id)

    client_user_id = order.get("user_id", "")
    await notify_admins(
        bot,
        settings,
        ADMIN_MERCHANT_PROBLEM_TEMPLATE.format(
            order_id=order_id,
            merchant_name=order.get("merchant_name", ""),
            courier_name=order.get("courier_name", ""),
            courier_phone=order.get("courier_phone", ""),
        ),
        exclude_user_id=client_user_id or None,
    )

    if client_user_id:
        try:
            await bot.send_message(int(client_user_id), CLIENT_ORDER_PROBLEM_TEMPLATE)
        except (TelegramBadRequest, ValueError):
            logger.warning(
                "Не удалось уведомить клиента %s о проблеме с заказом %s",
                client_user_id,
                order_id,
            )

    try:
        # reply_markup=None — живой фидбэк: раньше кнопки оставались
        # кликабельными на "поставленном на паузу" заказе (edit_text без
        # явного reply_markup сохраняет старую клавиатуру), что вело к
        # запутывающим повторным кликам (например, "Доставил" уже после
        # "Проблема" давало "заказ не найден" на самом деле из-за более
        # глубокой путаницы, но лишняя активная клавиатура точно не
        # помогала). Как только заказ на паузе — дальше решает админ,
        # курьеру больше не с чем взаимодействовать на этой карточке.
        await query.message.edit_text(COURIER_ORDER_PAUSED_MESSAGE, reply_markup=None)
    except TelegramBadRequest:
        pass
    await _safe_answer(query, COURIER_PROBLEM_ACK)
