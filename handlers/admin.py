"""
Все админ-команды (ТЗ §11 — полная таблица): заказы, мерчанты и меню,
курьеры, сервис. Нестандартные заказы и P2P (/confirm_custom_[id],
/reject_custom_[id], /approve_p2p_[id], /reject_p2p_[id]) — в
handlers/custom_order.py и handlers/p2p.py соответственно (команда-
специфичная логика тесно связана с их флоу, выносить сюда не имеет
смысла — тот же принцип самодостаточности модулей, что и у
handlers/courier.py). /reply_[user_id] — в handlers/support.py.

Day 4: /service_stop, /service_resume (уже были). Day 8: всё остальное.

Доступ только для Telegram ID из settings.admin_ids. Не-админам команды
отвечают молча (без ответа) — чтобы не раскрывать сам факт существования
админ-команд посторонним пользователям.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis

from config import Settings
from handlers.courier import _active_order_keyboard
from services.service_hours import set_manually_stopped
from services.sheets import Courier, SheetsClient
from services.timeouts import (
    cancel_delivery_timeout,
    cancel_offer_timeout,
    cancel_p2p_review_reminder,
    cancel_pickup_timeout,
    schedule_pickup_timeout,
)
from states.user_states import AdminStates
from texts.ru import (
    ADMIN_ADD_COURIER_DONE_TEMPLATE,
    ADMIN_ADD_COURIER_INVALID_TELEGRAM_ID,
    ADMIN_ADD_COURIER_NAME_PROMPT,
    ADMIN_ADD_COURIER_PHONE_PROMPT,
    ADMIN_ADD_COURIER_TELEGRAM_ID_PROMPT,
    ADMIN_ASSIGN_ACK_TEMPLATE,
    ADMIN_ASSIGN_COURIER_NOT_FOUND_TEMPLATE,
    ADMIN_BROADCAST_CANCEL_BUTTON,
    ADMIN_BROADCAST_CANCELLED_ACK,
    ADMIN_BROADCAST_CONFIRM_BUTTON,
    ADMIN_BROADCAST_CONFIRM_TEMPLATE,
    ADMIN_BROADCAST_DONE_TEMPLATE,
    ADMIN_BROADCAST_EMPTY_MESSAGE,
    ADMIN_CANCEL_ACK_TEMPLATE,
    ADMIN_COURIER_ACTIVE_LABEL,
    ADMIN_COURIER_INACTIVE_LABEL,
    ADMIN_COURIER_NOT_FOUND_TEMPLATE,
    ADMIN_COURIER_SHIFT_OFF_LABEL,
    ADMIN_COURIER_SHIFT_ON_LABEL,
    ADMIN_COURIER_TOGGLED_TEMPLATE,
    ADMIN_COURIERS_EMPTY,
    ADMIN_COURIERS_HEADER,
    ADMIN_COURIERS_ROW_TEMPLATE,
    ADMIN_DAY_START_CONFIRM_BUTTON,
    ADMIN_DAY_START_DISABLE_BUTTON,
    ADMIN_DAY_START_EMPTY,
    ADMIN_DAY_START_HEADER,
    ADMIN_DAY_START_MERCHANT_CONFIRMED_TEMPLATE,
    ADMIN_DAY_START_MERCHANT_DISABLED_TEMPLATE,
    ADMIN_DELIVER_ACK_TEMPLATE,
    ADMIN_ITEM_NOT_FOUND_TEMPLATE,
    ADMIN_ITEM_TOGGLED_TEMPLATE,
    ADMIN_MERCHANT_ENABLED_TEMPLATE,
    ADMIN_MERCHANT_NOT_FOUND_TEMPLATE,
    ADMIN_MERCHANT_TOGGLED_ACTIVE_TEMPLATE,
    ADMIN_ORDER_CARD_ADDRESS_LINE_TEMPLATE,
    ADMIN_ORDER_CARD_CLIENT_LINE_TEMPLATE,
    ADMIN_ORDER_CARD_COURIER_LINE_TEMPLATE,
    ADMIN_ORDER_CARD_HEADER_TEMPLATE,
    ADMIN_ORDER_CARD_ITEMS_LINE_TEMPLATE,
    ADMIN_ORDER_CARD_MERCHANT_LINE_TEMPLATE,
    ADMIN_ORDER_CARD_NO_COURIER_LINE,
    ADMIN_ORDER_CARD_P2P_LINE_TEMPLATE,
    ADMIN_ORDER_NOT_FOUND_TEMPLATE,
    ADMIN_ORDERS_EMPTY,
    ADMIN_ORDERS_HEADER,
    ADMIN_ORDERS_ROW_TEMPLATE,
    ADMIN_SERVICE_RESUMED_ACK,
    ADMIN_SERVICE_STOPPED_ACK,
    ADMIN_STATS_AVG_ASSIGN_LINE_TEMPLATE,
    ADMIN_STATS_AVG_DELIVERY_LINE_TEMPLATE,
    ADMIN_STATS_AVG_PICKUP_LINE_TEMPLATE,
    ADMIN_STATS_AVG_TIME_HEADER,
    ADMIN_STATS_CONVERSION_LINE_TEMPLATE,
    ADMIN_STATS_HEADER_TEMPLATE,
    ADMIN_STATS_NO_ORDERS,
    ADMIN_STATS_STATUS_LINE_TEMPLATE,
    CANCELLED_REASON_SUFFIX_TEMPLATE,
    CLIENT_BROADCAST_PREFIX,
    CLIENT_ORDER_ASSIGNED_TEMPLATE,
    CLIENT_ORDER_CANCELLED_TEMPLATE,
    CLIENT_ORDER_DELIVERED_TEMPLATE,
)
from texts.ru import COURIER_ACTIVE_ORDER_TEMPLATE
from utils.timefmt import now_local_str

logger = logging.getLogger(__name__)

router = Router(name=__name__)

_ACTIVE_ORDER_EXCLUDED_STATUSES = {"delivered", "cancelled"}
_TIME_FIELD_FORMAT = "%Y-%m-%d %H:%M:%S"

_BROADCAST_PENDING_KEY = "admin_broadcast_pending_text"
_BROADCAST_CONFIRM_CALLBACK_DATA = "admin_broadcast_confirm"
_BROADCAST_CANCEL_CALLBACK_DATA = "admin_broadcast_cancel"

_COURIER_ID_SEQUENCE_REDIS_KEY = "couriers:id_seq"


def _is_admin(message: Message, settings: Settings) -> bool:
    return bool(message.from_user) and message.from_user.id in settings.admin_ids


class DayStartCallback(CallbackData, prefix="day_start"):
    merchant_id: str
    action: str  # "confirm" | "disable"


# ---------------------------------------------------------------------- #
# Заказы
# ---------------------------------------------------------------------- #


def _order_summary_short(order: dict[str, str]) -> str:
    kind = order.get("order_kind", "standard")
    if kind == "p2p":
        return f"{order.get('pickup_address', '')} → {order.get('dropoff_address', '')}"
    if kind == "custom":
        return (order.get("custom_description", "") or "")[:40]
    return order.get("merchant_name", "")


@router.message(Command("orders"))
async def cmd_orders(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    orders = await sheets.get_all_orders()
    active = [o for o in orders if o.get("status") not in _ACTIVE_ORDER_EXCLUDED_STATUSES]
    if not active:
        await message.answer(ADMIN_ORDERS_EMPTY)
        return

    lines = [ADMIN_ORDERS_HEADER, ""]
    for order in active:
        lines.append(
            ADMIN_ORDERS_ROW_TEMPLATE.format(
                order_id=order.get("order_id", ""),
                order_kind=order.get("order_kind", "standard"),
                status=order.get("status", ""),
                summary=_order_summary_short(order),
            )
        )
    await message.answer("\n".join(lines))


@router.message(F.text.startswith("/order_"))
async def cmd_order_card(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    order_id = (message.text or "")[len("/order_") :].strip().split()[0:1]
    if not order_id:
        return
    order_id = order_id[0]

    order = await sheets.get_order(order_id)
    if order is None:
        await message.answer(ADMIN_ORDER_NOT_FOUND_TEMPLATE.format(order_id=order_id))
        return

    lines = [
        ADMIN_ORDER_CARD_HEADER_TEMPLATE.format(
            order_id=order_id,
            order_kind=order.get("order_kind", "standard"),
            status=order.get("status", ""),
            timestamp_created=order.get("timestamp_created", ""),
        )
    ]
    username = order.get("username", "")
    phone = order.get("phone", "")
    contact = f"@{username}" if username else (phone or "—")
    lines.append(ADMIN_ORDER_CARD_CLIENT_LINE_TEMPLATE.format(contact=contact))

    kind = order.get("order_kind", "standard")
    if kind == "p2p":
        lines.append(
            ADMIN_ORDER_CARD_P2P_LINE_TEMPLATE.format(
                pickup_address=order.get("pickup_address", ""),
                dropoff_address=order.get("dropoff_address", ""),
            )
        )
        lines.append(ADMIN_ORDER_CARD_ITEMS_LINE_TEMPLATE.format(admin_notes=order.get("p2p_description", "")))
    else:
        lines.append(ADMIN_ORDER_CARD_MERCHANT_LINE_TEMPLATE.format(merchant_name=order.get("merchant_name", "")))
        lines.append(ADMIN_ORDER_CARD_ITEMS_LINE_TEMPLATE.format(admin_notes=order.get("admin_notes", "")))
        lines.append(ADMIN_ORDER_CARD_ADDRESS_LINE_TEMPLATE.format(delivery_address=order.get("delivery_address", "")))

    if order.get("courier_id"):
        lines.append(
            ADMIN_ORDER_CARD_COURIER_LINE_TEMPLATE.format(
                courier_name=order.get("courier_name", ""), courier_phone=order.get("courier_phone", "")
            )
        )
    else:
        lines.append(ADMIN_ORDER_CARD_NO_COURIER_LINE)

    await message.answer("\n".join(lines))


@router.message(F.text.startswith("/assign_"))
async def cmd_assign_order(
    message: Message,
    settings: Settings,
    sheets: SheetsClient,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
) -> None:
    """/assign_042_[courier_id] — ручное назначение (ТЗ §11, для
    no_courier — "решение внесистемно, затем фиксация"). Не блокируем по
    текущему статусу заказа: Админ мог решить назначить курьера и в
    других ситуациях (не только no_courier) — это его осознанный выбор,
    ручная команда существует именно для нестандартных вмешательств."""
    if not _is_admin(message, settings):
        return
    rest = (message.text or "")[len("/assign_") :]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        return
    order_id, courier_id = parts[0].strip(), parts[1].strip()

    order = await sheets.get_order(order_id)
    if order is None:
        await message.answer(ADMIN_ORDER_NOT_FOUND_TEMPLATE.format(order_id=order_id))
        return

    couriers = await sheets.get_couriers()
    courier: Courier | None = next((c for c in couriers if c.courier_id == courier_id), None)
    if courier is None:
        await message.answer(ADMIN_ASSIGN_COURIER_NOT_FOUND_TEMPLATE.format(courier_id=courier_id))
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
        "order_manually_assigned: order_id=%s courier_id=%s admin_id=%s",
        order_id, courier_id, message.from_user.id,
    )
    await sheets.append_event(
        event_type="order_manually_assigned",
        actor_role="admin",
        actor_id=str(message.from_user.id),
        order_id=order_id,
        details=f"courier_id={courier_id}",
    )

    cancel_offer_timeout(scheduler, order_id)
    schedule_pickup_timeout(scheduler, order_id, settings.PICKUP_TIMEOUT_MIN)

    try:
        await bot.send_message(
            int(courier.telegram_id),
            CLIENT_ORDER_ASSIGNED_TEMPLATE.format(  # переиспользуем как заголовок — курьеру ниже шлём полноценную карточку
                courier_name=courier.name, items=order.get("admin_notes", ""), merchant_name=order.get("merchant_name", "")
            ),
        )

        await bot.send_message(
            int(courier.telegram_id),
            COURIER_ACTIVE_ORDER_TEMPLATE.format(
                order_id=order_id,
                merchant_name=order.get("merchant_name", ""),
                delivery_address=order.get("delivery_address", ""),
            ),
            reply_markup=_active_order_keyboard(order_id),
        )
    except (TelegramAPIError, ValueError) as exc:
        logger.warning("Не удалось уведомить курьера %s о назначении: %s", courier.telegram_id, exc)

    client_user_id = order.get("user_id", "")
    if client_user_id:
        try:
            await bot.send_message(
                int(client_user_id),
                CLIENT_ORDER_ASSIGNED_TEMPLATE.format(
                    courier_name=courier.name,
                    items=order.get("admin_notes", ""),
                    merchant_name=order.get("merchant_name", ""),
                ),
            )
        except (TelegramAPIError, ValueError) as exc:
            logger.warning("Не удалось уведомить клиента %s о назначении: %s", client_user_id, exc)

    await message.answer(ADMIN_ASSIGN_ACK_TEMPLATE.format(order_id=order_id, courier_name=courier.name))


@router.message(F.text.startswith("/cancel_"))
async def cmd_cancel_order(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot, scheduler: AsyncIOScheduler
) -> None:
    if not _is_admin(message, settings):
        return
    rest = (message.text or "")[len("/cancel_") :]
    parts = rest.split(maxsplit=1)
    if not parts:
        return
    order_id = parts[0].strip()
    reason = parts[1].strip() if len(parts) > 1 else ""

    order = await sheets.get_order(order_id)
    if order is None:
        await message.answer(ADMIN_ORDER_NOT_FOUND_TEMPLATE.format(order_id=order_id))
        return

    await sheets.update_order_fields(
        order_id, {"status": "cancelled", "cancel_reason": reason}
    )
    logger.info(
        "order_cancelled_by_admin: order_id=%s admin_id=%s reason=%r",
        order_id, message.from_user.id, reason,
    )
    await sheets.append_event(
        event_type="order_cancelled",
        actor_role="admin",
        actor_id=str(message.from_user.id),
        order_id=order_id,
        details=reason,
    )

    cancel_offer_timeout(scheduler, order_id)
    cancel_pickup_timeout(scheduler, order_id)
    cancel_delivery_timeout(scheduler, order_id)
    cancel_p2p_review_reminder(scheduler, order_id)

    client_user_id = order.get("user_id", "")
    reason_suffix = CANCELLED_REASON_SUFFIX_TEMPLATE.format(reason=reason) if reason else ""
    if client_user_id:
        try:
            await bot.send_message(
                int(client_user_id),
                CLIENT_ORDER_CANCELLED_TEMPLATE.format(order_id=order_id, reason_suffix=reason_suffix),
            )
        except (TelegramAPIError, ValueError) as exc:
            logger.warning("Не удалось уведомить клиента %s об отмене: %s", client_user_id, exc)

    await message.answer(ADMIN_CANCEL_ACK_TEMPLATE.format(order_id=order_id))


@router.message(F.text.startswith("/deliver_"))
async def cmd_force_deliver(
    message: Message, settings: Settings, sheets: SheetsClient, bot: Bot, scheduler: AsyncIOScheduler
) -> None:
    if not _is_admin(message, settings):
        return
    order_id = (message.text or "")[len("/deliver_") :].strip().split()[0:1]
    if not order_id:
        return
    order_id = order_id[0]

    order = await sheets.get_order(order_id)
    if order is None:
        await message.answer(ADMIN_ORDER_NOT_FOUND_TEMPLATE.format(order_id=order_id))
        return


    await sheets.update_order_fields(
        order_id, {"status": "delivered", "timestamp_delivered": now_local_str(settings)}
    )
    logger.info("order_force_delivered: order_id=%s admin_id=%s", order_id, message.from_user.id)
    await sheets.append_event(
        event_type="order_force_delivered",
        actor_role="admin",
        actor_id=str(message.from_user.id),
        order_id=order_id,
    )

    cancel_delivery_timeout(scheduler, order_id)

    client_user_id = order.get("user_id", "")
    if client_user_id:
        try:
            await bot.send_message(int(client_user_id), CLIENT_ORDER_DELIVERED_TEMPLATE)
        except (TelegramAPIError, ValueError) as exc:
            logger.warning("Не удалось уведомить клиента %s о принудительной доставке: %s", client_user_id, exc)

    await message.answer(ADMIN_DELIVER_ACK_TEMPLATE.format(order_id=order_id))


# ---------------------------------------------------------------------- #
# Мерчанты и меню
# ---------------------------------------------------------------------- #


def _day_start_keyboard(merchant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ADMIN_DAY_START_CONFIRM_BUTTON,
                    callback_data=DayStartCallback(merchant_id=merchant_id, action="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text=ADMIN_DAY_START_DISABLE_BUTTON,
                    callback_data=DayStartCallback(merchant_id=merchant_id, action="disable").pack(),
                ),
            ]
        ]
    )


@router.message(Command("day_start"))
async def cmd_day_start(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    merchants = await sheets.get_merchants(force_refresh=True)
    active = [m for m in merchants if m.is_active]
    if not active:
        await message.answer(ADMIN_DAY_START_EMPTY)
        return

    await message.answer(ADMIN_DAY_START_HEADER)
    for merchant in active:
        await message.answer(merchant.name, reply_markup=_day_start_keyboard(merchant.merchant_id))


@router.callback_query(DayStartCallback.filter())
async def on_day_start_action(
    query: CallbackQuery, callback_data: DayStartCallback, settings: Settings, sheets: SheetsClient
) -> None:
    if not query.from_user or query.from_user.id not in settings.admin_ids:
        await query.answer()
        return

    merchants = await sheets.get_merchants(force_refresh=True)
    merchant = next((m for m in merchants if m.merchant_id == callback_data.merchant_id), None)
    name = merchant.name if merchant else callback_data.merchant_id

    confirmed = callback_data.action == "confirm"
    await sheets.update_merchant_fields(
        callback_data.merchant_id, {"today_confirmed": "TRUE" if confirmed else "FALSE"}
    )
    logger.info(
        "merchant_day_start: merchant_id=%s confirmed=%s admin_id=%s",
        callback_data.merchant_id, confirmed, query.from_user.id,
    )
    await sheets.append_event(
        event_type="merchant_day_start",
        actor_role="admin",
        actor_id=str(query.from_user.id),
        details=f"merchant_id={callback_data.merchant_id} confirmed={confirmed}",
    )

    text = (
        ADMIN_DAY_START_MERCHANT_CONFIRMED_TEMPLATE if confirmed else ADMIN_DAY_START_MERCHANT_DISABLED_TEMPLATE
    ).format(name=name)
    await query.message.edit_text(text)
    await query.answer()


@router.message(F.text.startswith("/enable_merchant_"))
async def cmd_enable_merchant(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    merchant_id = (message.text or "")[len("/enable_merchant_") :].strip().split()[0:1]
    if not merchant_id:
        return
    merchant_id = merchant_id[0]

    merchants = await sheets.get_merchants(force_refresh=True)
    merchant = next((m for m in merchants if m.merchant_id == merchant_id), None)
    if merchant is None:
        await message.answer(ADMIN_MERCHANT_NOT_FOUND_TEMPLATE.format(merchant_id=merchant_id))
        return

    await sheets.update_merchant_fields(merchant_id, {"today_confirmed": "TRUE"})
    logger.info("merchant_enabled: merchant_id=%s admin_id=%s", merchant_id, message.from_user.id)
    await sheets.append_event(
        event_type="merchant_enabled", actor_role="admin", actor_id=str(message.from_user.id),
        details=f"merchant_id={merchant_id}",
    )
    await message.answer(ADMIN_MERCHANT_ENABLED_TEMPLATE.format(name=merchant.name))


@router.message(F.text.startswith("/toggle_merchant_"))
async def cmd_toggle_merchant(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    merchant_id = (message.text or "")[len("/toggle_merchant_") :].strip().split()[0:1]
    if not merchant_id:
        return
    merchant_id = merchant_id[0]

    merchants = await sheets.get_merchants(force_refresh=True)
    merchant = next((m for m in merchants if m.merchant_id == merchant_id), None)
    if merchant is None:
        await message.answer(ADMIN_MERCHANT_NOT_FOUND_TEMPLATE.format(merchant_id=merchant_id))
        return

    new_value = not merchant.is_active
    await sheets.update_merchant_fields(merchant_id, {"is_active": "TRUE" if new_value else "FALSE"})
    logger.info(
        "merchant_toggled: merchant_id=%s is_active=%s admin_id=%s",
        merchant_id, new_value, message.from_user.id,
    )
    await sheets.append_event(
        event_type="merchant_toggled", actor_role="admin", actor_id=str(message.from_user.id),
        details=f"merchant_id={merchant_id} is_active={new_value}",
    )
    await message.answer(
        ADMIN_MERCHANT_TOGGLED_ACTIVE_TEMPLATE.format(name=merchant.name, value="TRUE" if new_value else "FALSE")
    )


@router.message(F.text.startswith("/toggle_item_"))
async def cmd_toggle_item(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    item_id = (message.text or "")[len("/toggle_item_") :].strip().split()[0:1]
    if not item_id:
        return
    item_id = item_id[0]

    items = await sheets.get_items(force_refresh=True)
    item = next((i for i in items if i.item_id == item_id), None)
    if item is None:
        await message.answer(ADMIN_ITEM_NOT_FOUND_TEMPLATE.format(item_id=item_id))
        return

    new_value = not item.is_available
    await sheets.update_item_fields(item_id, {"is_available": "TRUE" if new_value else "FALSE"})
    logger.info(
        "item_toggled: item_id=%s is_available=%s admin_id=%s", item_id, new_value, message.from_user.id
    )
    await sheets.append_event(
        event_type="item_toggled", actor_role="admin", actor_id=str(message.from_user.id),
        details=f"item_id={item_id} is_available={new_value}",
    )
    await message.answer(
        ADMIN_ITEM_TOGGLED_TEMPLATE.format(name=item.name, value="TRUE" if new_value else "FALSE")
    )


# ---------------------------------------------------------------------- #
# Курьеры
# ---------------------------------------------------------------------- #


@router.message(Command("couriers"))
async def cmd_couriers(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    couriers = await sheets.get_couriers(force_refresh=True)
    if not couriers:
        await message.answer(ADMIN_COURIERS_EMPTY)
        return

    lines = [ADMIN_COURIERS_HEADER, ""]
    for courier in couriers:
        lines.append(
            ADMIN_COURIERS_ROW_TEMPLATE.format(
                courier_id=courier.courier_id,
                name=courier.name,
                shift_label=ADMIN_COURIER_SHIFT_ON_LABEL if courier.on_shift else ADMIN_COURIER_SHIFT_OFF_LABEL,
                active_label=ADMIN_COURIER_ACTIVE_LABEL if courier.is_active else ADMIN_COURIER_INACTIVE_LABEL,
            )
        )
    await message.answer("\n".join(lines))


@router.message(Command("add_courier"))
async def cmd_add_courier_start(message: Message, settings: Settings, state: FSMContext) -> None:
    if not _is_admin(message, settings):
        return
    await state.set_state(AdminStates.adding_courier_name)
    await message.answer(ADMIN_ADD_COURIER_NAME_PROMPT)


@router.message(StateFilter(AdminStates.adding_courier_name), F.text)
async def on_add_courier_name(message: Message, state: FSMContext) -> None:
    await state.update_data(admin_new_courier_name=message.text.strip())
    await state.set_state(AdminStates.adding_courier_phone)
    await message.answer(ADMIN_ADD_COURIER_PHONE_PROMPT)


@router.message(StateFilter(AdminStates.adding_courier_phone), F.text)
async def on_add_courier_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(admin_new_courier_phone=message.text.strip())
    await state.set_state(AdminStates.adding_courier_telegram_id)
    await message.answer(ADMIN_ADD_COURIER_TELEGRAM_ID_PROMPT)


@router.message(StateFilter(AdminStates.adding_courier_telegram_id), F.text)
async def on_add_courier_telegram_id(
    message: Message, state: FSMContext, sheets: SheetsClient, redis: Redis
) -> None:
    telegram_id = message.text.strip()
    if not telegram_id.isdigit():
        await message.answer(ADMIN_ADD_COURIER_INVALID_TELEGRAM_ID)
        return

    data = await state.get_data()
    name = data.get("admin_new_courier_name", "")
    phone = data.get("admin_new_courier_phone", "")

    seq = await redis.incr(_COURIER_ID_SEQUENCE_REDIS_KEY)
    courier_id = f"cur_{seq:03d}"

    await sheets.add_courier(
        courier_id=courier_id,
        name=name,
        phone=phone,
        telegram_id=telegram_id,
        is_registered_legal=False,
        on_shift=False,
        is_active=True,
    )
    logger.info(
        "courier_added: courier_id=%s admin_id=%s", courier_id, message.from_user.id if message.from_user else ""
    )
    await sheets.append_event(
        event_type="courier_added",
        actor_role="admin",
        actor_id=str(message.from_user.id if message.from_user else ""),
        details=f"courier_id={courier_id}",
    )

    await state.set_state(None)
    await state.update_data(admin_new_courier_name=None, admin_new_courier_phone=None)
    await message.answer(ADMIN_ADD_COURIER_DONE_TEMPLATE.format(name=name, courier_id=courier_id))


@router.message(F.text.startswith("/toggle_courier_"))
async def cmd_toggle_courier(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    courier_id = (message.text or "")[len("/toggle_courier_") :].strip().split()[0:1]
    if not courier_id:
        return
    courier_id = courier_id[0]

    couriers = await sheets.get_couriers(force_refresh=True)
    courier = next((c for c in couriers if c.courier_id == courier_id), None)
    if courier is None:
        await message.answer(ADMIN_COURIER_NOT_FOUND_TEMPLATE.format(courier_id=courier_id))
        return

    new_value = not courier.is_active
    await sheets.update_courier_fields(courier_id, {"is_active": "TRUE" if new_value else "FALSE"})
    logger.info(
        "courier_toggled: courier_id=%s is_active=%s admin_id=%s",
        courier_id, new_value, message.from_user.id,
    )
    await sheets.append_event(
        event_type="courier_toggled", actor_role="admin", actor_id=str(message.from_user.id),
        details=f"courier_id={courier_id} is_active={new_value}",
    )
    await message.answer(
        ADMIN_COURIER_TOGGLED_TEMPLATE.format(name=courier.name, value="TRUE" if new_value else "FALSE")
    )


# ---------------------------------------------------------------------- #
# Сервис
# ---------------------------------------------------------------------- #


@router.message(Command("service_stop"))
async def cmd_service_stop(
    message: Message, settings: Settings, redis: Redis, sheets: SheetsClient
) -> None:
    if not _is_admin(message, settings):
        return
    await set_manually_stopped(redis, True)
    logger.info("service_stopped: admin_id=%s", message.from_user.id)
    await sheets.append_event(
        event_type="service_stopped", actor_role="admin", actor_id=str(message.from_user.id)
    )
    await message.answer(ADMIN_SERVICE_STOPPED_ACK)


@router.message(Command("service_resume"))
async def cmd_service_resume(
    message: Message, settings: Settings, redis: Redis, sheets: SheetsClient
) -> None:
    if not _is_admin(message, settings):
        return
    await set_manually_stopped(redis, False)
    logger.info("service_resumed: admin_id=%s", message.from_user.id)
    await sheets.append_event(
        event_type="service_resumed", actor_role="admin", actor_id=str(message.from_user.id)
    )
    await message.answer(ADMIN_SERVICE_RESUMED_ACK)


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TIME_FIELD_FORMAT)
    except ValueError:
        return None


def _avg_minutes(orders: list[dict[str, str]], start_field: str, end_field: str) -> float | None:
    deltas: list[float] = []
    for order in orders:
        start = _parse_timestamp(order.get(start_field, ""))
        end = _parse_timestamp(order.get(end_field, ""))
        if start is not None and end is not None:
            deltas.append((end - start).total_seconds() / 60)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


@router.message(Command("stats"))
async def cmd_stats(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not _is_admin(message, settings):
        return
    today_str = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
    all_orders = await sheets.get_all_orders()
    today_orders = [o for o in all_orders if o.get("timestamp_created", "").startswith(today_str)]

    if not today_orders:
        await message.answer(ADMIN_STATS_NO_ORDERS)
        return

    status_counts: dict[str, int] = {}
    for order in today_orders:
        status = order.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    lines = [ADMIN_STATS_HEADER_TEMPLATE.format(date=today_str), ""]
    for status, count in sorted(status_counts.items()):
        lines.append(ADMIN_STATS_STATUS_LINE_TEMPLATE.format(status=status, count=count))

    avg_assign = _avg_minutes(today_orders, "timestamp_created", "timestamp_assigned")
    avg_pickup = _avg_minutes(today_orders, "timestamp_assigned", "timestamp_picked_up")
    avg_delivery = _avg_minutes(today_orders, "timestamp_picked_up", "timestamp_delivered")
    if avg_assign is not None or avg_pickup is not None or avg_delivery is not None:
        lines.append(ADMIN_STATS_AVG_TIME_HEADER)
        if avg_assign is not None:
            lines.append(ADMIN_STATS_AVG_ASSIGN_LINE_TEMPLATE.format(minutes=avg_assign))
        if avg_pickup is not None:
            lines.append(ADMIN_STATS_AVG_PICKUP_LINE_TEMPLATE.format(minutes=avg_pickup))
        if avg_delivery is not None:
            lines.append(ADMIN_STATS_AVG_DELIVERY_LINE_TEMPLATE.format(minutes=avg_delivery))

    delivered_count = status_counts.get("delivered", 0)
    conversion = (delivered_count / len(today_orders)) * 100 if today_orders else 0.0
    lines.append(ADMIN_STATS_CONVERSION_LINE_TEMPLATE.format(percent=conversion))

    await message.answer("\n".join(lines))


@router.message(Command("broadcast"))
async def cmd_broadcast_start(
    message: Message, settings: Settings, sheets: SheetsClient, command: CommandObject, state: FSMContext
) -> None:
    if not _is_admin(message, settings):
        return
    text = (command.args or "").strip()
    if not text:
        await message.answer(ADMIN_BROADCAST_EMPTY_MESSAGE)
        return

    events = await sheets.get_all_events()
    client_ids = {
        e.get("actor_id", "")
        for e in events
        if e.get("actor_role") == "client" and e.get("event_type") == "bot_start" and e.get("actor_id", "").strip()
    }

    await state.update_data(**{_BROADCAST_PENDING_KEY: text})
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ADMIN_BROADCAST_CONFIRM_BUTTON, callback_data=_BROADCAST_CONFIRM_CALLBACK_DATA),
                InlineKeyboardButton(text=ADMIN_BROADCAST_CANCEL_BUTTON, callback_data=_BROADCAST_CANCEL_CALLBACK_DATA),
            ]
        ]
    )
    await message.answer(
        ADMIN_BROADCAST_CONFIRM_TEMPLATE.format(count=len(client_ids), text=text), reply_markup=keyboard
    )


@router.callback_query(F.data == _BROADCAST_CANCEL_CALLBACK_DATA)
async def on_broadcast_cancel(query: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    if not query.from_user or query.from_user.id not in settings.admin_ids:
        await query.answer()
        return
    await state.update_data(**{_BROADCAST_PENDING_KEY: None})
    await query.message.edit_text(ADMIN_BROADCAST_CANCELLED_ACK)
    await query.answer()


@router.callback_query(F.data == _BROADCAST_CONFIRM_CALLBACK_DATA)
async def on_broadcast_confirm(
    query: CallbackQuery, settings: Settings, sheets: SheetsClient, state: FSMContext, bot: Bot
) -> None:
    if not query.from_user or query.from_user.id not in settings.admin_ids:
        await query.answer()
        return

    data = await state.get_data()
    text = data.get(_BROADCAST_PENDING_KEY)
    if not text:
        await query.answer()
        return
    await state.update_data(**{_BROADCAST_PENDING_KEY: None})

    events = await sheets.get_all_events()
    client_ids = {
        e.get("actor_id", "")
        for e in events
        if e.get("actor_role") == "client" and e.get("event_type") == "bot_start" and e.get("actor_id", "").strip()
    }

    sent, failed = 0, 0
    for client_id in client_ids:
        try:
            await bot.send_message(int(client_id), f"{CLIENT_BROADCAST_PREFIX}{text}")
            sent += 1
        except (TelegramAPIError, ValueError):
            failed += 1

    logger.info("broadcast_sent: admin_id=%s sent=%d failed=%d", query.from_user.id, sent, failed)
    await sheets.append_event(
        event_type="broadcast_sent", actor_role="admin", actor_id=str(query.from_user.id),
        details=f"sent={sent} failed={failed}",
    )

    await query.message.edit_text(ADMIN_BROADCAST_DONE_TEMPLATE.format(sent=sent, failed=failed))
    await query.answer()
