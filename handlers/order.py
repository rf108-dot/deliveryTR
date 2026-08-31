"""
Оформление заказа: геолокация, reverse geocoding, контакт, перепроверка
наличия, итог, проверка геозоны.

Day 3 (ТЗ §7.4, §3A). Флоу:
  1. Геолокация ИЛИ ручной ввод адреса (7.4.1)
     - геолокация → проверка зоны → reverse geocode → [Верно]/[Изменить]
     - ручной адрес → forward geocode (нужно для проверки зоны — см.
       пояснение в services/geocoding.py, это решение архитектурного
       пробела в ТЗ, а не буквальное требование) → проверка зоны
  2. Контакт для курьера (7.4.2): @username автоматически, иначе запрос
     номера телефона
  3. Перепроверка наличия позиций + итоговое подтверждение (7.4.3)
  4. [Подтвердить] → «заказ создан» / [Изменить] → назад к корзине

Day 4 (ТЗ §6, §5, §4.2): проверка часов работы на входе в чекаут и
повторно на «Подтвердить» (защита от «начал в 20:14, подтвердил в
20:16»); заглушка оплаты (PAYMENT_ENABLED); реальная запись заказа в
лист «Заказы» с генерацией order_id через Redis (атомарный счётчик,
глобальный — не per-user FSM-данные).

Хранение черновика заказа — в FSMContext.data под ключом ORDER_DRAFT_KEY,
отдельно от корзины (CART_STATE_KEY, см. handlers/cart.py), которая
остаётся источником правды по составу заказа до самого подтверждения.
"""

from __future__ import annotations

import json
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from redis.asyncio import Redis

from config import Settings
from handlers.cart import CART_CHECKOUT_CALLBACK_DATA, CART_STATE_KEY, render_cart_view
from handlers.catalog import show_categories
from services.dispatch import send_offer_to_couriers
from services.geocoding import GeocodingAdapter
from services.service_hours import ServiceStatus, get_service_status
from services.sheets import SheetsClient, SheetsWriteError, items_for_merchant
from services.zone import is_point_in_zone
from states.user_states import OrderStates
from texts.ru import (
    CART_EMPTY_ALERT,
    ORDER_ACCEPTED_DEFAULT,
    ORDER_ACCEPTED_MANUAL_ADDRESS_TEMPLATE,
    ORDER_ADDRESS_CONFIRM_BUTTON,
    ORDER_ADDRESS_CONFIRM_TEMPLATE,
    ORDER_ADDRESS_EDIT_BUTTON,
    ORDER_ADDRESS_EDIT_PROMPT_TEMPLATE,
    ORDER_ADDRESS_NOT_FOUND,
    ORDER_ADDRESS_PARTIAL_MATCH_WARNING,
    ORDER_AGAIN_BUTTON,
    ORDER_CONFIRM_BUTTON,
    ORDER_CONTACT_BUTTON,
    ORDER_CONTACT_INVALID_FORMAT_MESSAGE,
    ORDER_CONTACT_PROMPT,
    ORDER_CONTACT_RECEIVED_ACK,
    ORDER_EDIT_BUTTON,
    ORDER_ITEMS_UNAVAILABLE,
    ORDER_LOCATION_BUTTON,
    ORDER_LOCATION_PROMPT,
    ORDER_LOCATION_RECEIVED_ACK,
    ORDER_MANUAL_ADDRESS_BUTTON,
    ORDER_MANUAL_ADDRESS_PROMPT,
    ORDER_PHONE_CONFIRM_TEMPLATE,
    ORDER_SAVE_FAILED_MESSAGE,
    ORDER_SUMMARY_ADDRESS_LINE,
    ORDER_SUMMARY_CONTACT_PHONE_LINE,
    ORDER_SUMMARY_CONTACT_USERNAME_LINE,
    ORDER_SUMMARY_CUSTOM_DESCRIPTION_LINE,
    ORDER_SUMMARY_HEADER,
    ORDER_SUMMARY_MERCHANT_LINE,
    ORDER_SUMMARY_PAYMENT_NOTE,
    ORDER_UNEXPECTED_INPUT_IN_LOCATION_STEP,
    ORDER_ZONE_OUTSIDE_MESSAGE,
    ORDER_ZONE_OUTSIDE_UNCERTAIN_MESSAGE,
    PAYMENT_NOT_IMPLEMENTED_MESSAGE,
    SERVICE_CLOSED_MESSAGE,
    SERVICE_LAST_ORDER_PASSED_MESSAGE,
    SERVICE_MANUALLY_STOPPED_MESSAGE,
)
from utils.formatters import format_order_items_text, format_order_summary
from utils.telegram_safety import resilient
from utils.timefmt import now_local_str

logger = logging.getLogger(__name__)

router = Router(name=__name__)

# Отдельный ключ в FSMContext.data — не пересекается с CART_STATE_KEY.
ORDER_DRAFT_KEY = "order_draft"

ADDRESS_CONFIRM_CALLBACK_DATA = "order_address_confirm"
ADDRESS_EDIT_CALLBACK_DATA = "order_address_edit"
ORDER_CONFIRM_CALLBACK_DATA = "order_confirm"
ORDER_BACK_TO_CART_CALLBACK_DATA = "order_back_to_cart"
PHONE_CONFIRM_CALLBACK_DATA = "order_phone_confirm"
PHONE_EDIT_CALLBACK_DATA = "order_phone_edit"
ORDER_AGAIN_CALLBACK_DATA = "order_again"

# Redis-ключ атомарного счётчика order_id — глобальный, не per-user.
ORDER_ID_SEQUENCE_REDIS_KEY = "orders:id_seq"

# Идемпотентность подтверждения заказа (защита от двойного тапа/ретрая
# Telegram-апдейта на "Подтвердить" — см. on_order_confirmed). TTL взят
# с запасом относительно реальной длительности хендлера (наблюдалось
# 4-6 сек в проде) — на случай, если процесс упадёт до выполнения finally.
ORDER_CONFIRM_LOCK_KEY_TEMPLATE = "order_confirm_lock:{user_id}"
ORDER_CONFIRM_LOCK_TTL_SECONDS = 30

# Базовая проверка формата телефона (E.164-подобная: 7-15 цифр, опц. "+").
# НЕ строгая валидация конкретной страны (для этого нужна отдельная
# библиотека вроде phonenumbers/libphonenumber) — только отсечение явного
# мусора (буквы, слишком короткое/длинное). Разрешаем пробелы/дефисы/
# скобки во ВВОДЕ (люди так печатают), но не в итоговом сохранённом номере.
_PHONE_DIGITS_PATTERN = re.compile(r"^\+?\d{7,15}$")


def _normalize_phone_candidate(text: str) -> str | None:
    cleaned = re.sub(r"[\s\-()]", "", text.strip())
    return cleaned if _PHONE_DIGITS_PATTERN.match(cleaned) else None


def _service_status_message(status: str, settings: Settings) -> str:
    if status == ServiceStatus.MANUALLY_STOPPED:
        return SERVICE_MANUALLY_STOPPED_MESSAGE
    if status == ServiceStatus.LAST_ORDER_PASSED:
        return SERVICE_LAST_ORDER_PASSED_MESSAGE.format(last_order=settings.LAST_ORDER)
    if status == ServiceStatus.CLOSED:
        return SERVICE_CLOSED_MESSAGE.format(open=settings.SERVICE_OPEN, close=settings.SERVICE_CLOSE)
    return ""


async def _generate_order_id(redis: Redis) -> str:
    seq = await redis.incr(ORDER_ID_SEQUENCE_REDIS_KEY)
    return f"{seq:03d}"


# ---------------------------------------------------------------------- #
# Клавиатуры
# ---------------------------------------------------------------------- #


def _location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ORDER_LOCATION_BUTTON, request_location=True)],
            [KeyboardButton(text=ORDER_MANUAL_ADDRESS_BUTTON)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=ORDER_CONTACT_BUTTON, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _address_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ORDER_ADDRESS_CONFIRM_BUTTON, callback_data=ADDRESS_CONFIRM_CALLBACK_DATA
                ),
                InlineKeyboardButton(
                    text=ORDER_ADDRESS_EDIT_BUTTON, callback_data=ADDRESS_EDIT_CALLBACK_DATA
                ),
            ]
        ]
    )


def _phone_confirm_keyboard() -> InlineKeyboardMarkup:
    """Те же подписи кнопок «Верно»/«Изменить», что и у адреса — семантика
    одинаковая (подтвердить распознанное значение или исправить его)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ORDER_ADDRESS_CONFIRM_BUTTON, callback_data=PHONE_CONFIRM_CALLBACK_DATA
                ),
                InlineKeyboardButton(
                    text=ORDER_ADDRESS_EDIT_BUTTON, callback_data=PHONE_EDIT_CALLBACK_DATA
                ),
            ]
        ]
    )


def _final_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ORDER_CONFIRM_BUTTON, callback_data=ORDER_CONFIRM_CALLBACK_DATA)],
            [InlineKeyboardButton(text=ORDER_EDIT_BUTTON, callback_data=ORDER_BACK_TO_CART_CALLBACK_DATA)],
        ]
    )


async def _update_order_draft(state: FSMContext, **fields: object) -> None:
    data = await state.get_data()
    draft = dict(data.get(ORDER_DRAFT_KEY) or {})
    draft.update(fields)
    await state.update_data(**{ORDER_DRAFT_KEY: draft})


# ---------------------------------------------------------------------- #
# Точка входа
# ---------------------------------------------------------------------- #


def _build_checkout_draft(
    *,
    order_kind: str = "standard",
    merchant_id: str | None = None,
    merchant_name: str | None = None,
    custom_description: str | None = None,
    custom_photo_file_id: str | None = None,
) -> dict[str, object]:
    draft: dict[str, object] = {"order_kind": order_kind}
    if merchant_id is not None:
        draft["merchant_id"] = merchant_id
    if merchant_name is not None:
        draft["merchant_name"] = merchant_name
    if custom_description is not None:
        draft["custom_description"] = custom_description
    if custom_photo_file_id is not None:
        draft["custom_photo_file_id"] = custom_photo_file_id
    return draft


async def start_checkout_for_user(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    *,
    order_kind: str = "standard",
    merchant_id: str | None = None,
    merchant_name: str | None = None,
    custom_description: str | None = None,
    custom_photo_file_id: str | None = None,
) -> None:
    """
    Вариант start_checkout() для случаев, когда нет объекта Message
    целевого пользователя — например, чекаут запускается из АДМИНСКОЙ
    команды после подтверждения нестандартного заказа (ТЗ §7.3.4, см.
    handlers/custom_order.py: /confirm_custom_[id] обрабатывает апдейт
    от Админа, а перевести в чекаут нужно КЛИЕНТА). Логика идентична
    start_checkout(), просто отправка через bot.send_message(chat_id=...)
    вместо message.answer().

    order_kind="custom" плюс merchant_id/merchant_name/custom_description/
    custom_photo_file_id — прокидываются через ORDER_DRAFT_KEY и читаются
    веткой order_kind=="custom" в _show_final_confirmation/
    on_order_confirmed ниже. Для order_kind="standard" (обычный чекаут из
    корзины) эти поля не нужны — merchant_id/items берутся из
    CART_STATE_KEY как раньше.
    """
    draft = _build_checkout_draft(
        order_kind=order_kind,
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        custom_description=custom_description,
        custom_photo_file_id=custom_photo_file_id,
    )
    await state.update_data(**{ORDER_DRAFT_KEY: draft})
    await state.set_state(OrderStates.waiting_for_location)
    await bot.send_message(chat_id, ORDER_LOCATION_PROMPT, reply_markup=_location_keyboard())


async def start_checkout(message: Message, state: FSMContext) -> None:
    await state.update_data(**{ORDER_DRAFT_KEY: _build_checkout_draft()})
    await state.set_state(OrderStates.waiting_for_location)
    await message.answer(ORDER_LOCATION_PROMPT, reply_markup=_location_keyboard())


@router.callback_query(F.data == CART_CHECKOUT_CALLBACK_DATA)
async def on_checkout_start(
    query: CallbackQuery, state: FSMContext, settings: Settings, redis: Redis
) -> None:
    data = await state.get_data()
    cart = data.get(CART_STATE_KEY)
    if not cart or not cart.get("items"):
        await query.answer(CART_EMPTY_ALERT, show_alert=True)
        return

    status = await get_service_status(settings, redis)
    if status != ServiceStatus.OPEN:
        await query.answer(_service_status_message(status, settings), show_alert=True)
        return

    await start_checkout(query.message, state)
    await query.answer()


# ---------------------------------------------------------------------- #
# Шаг 1: геолокация / ручной адрес (7.4.1, §3A)
# ---------------------------------------------------------------------- #


@router.message(StateFilter(OrderStates.waiting_for_location), F.location)
async def on_location_received(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter, settings: Settings
) -> None:
    lat, lon = message.location.latitude, message.location.longitude

    if not is_point_in_zone(
        lat, lon, settings.ZONE_CENTER_LAT, settings.ZONE_CENTER_LON, settings.ZONE_RADIUS_KM
    ):
        # ВАЖНО: не сбрасываем состояние в None — иначе следующая попытка
        # пользователя (другая геолокация/адрес) просто не попадёт ни в
        # один хендлер и останется без ответа (реальный баг из живого
        # тестирования, см. CHECKLIST.md). Остаёмся в waiting_for_location.
        await message.answer(ORDER_ZONE_OUTSIDE_MESSAGE)
        await message.answer(ORDER_LOCATION_PROMPT, reply_markup=_location_keyboard())
        return

    await message.answer(ORDER_LOCATION_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())

    address = await geocoding.reverse_geocode(lat, lon)
    if address is None:
        # Reverse geocoding не смог определить адрес — просим ввести текстом,
        # координаты уже прошли проверку зоны, сохраняем их.
        await _update_order_draft(state, lat=lat, lon=lon)
        await state.set_state(OrderStates.waiting_for_manual_address)
        await message.answer(ORDER_MANUAL_ADDRESS_PROMPT)
        return

    await _update_order_draft(state, lat=lat, lon=lon, address=address, address_manually_edited=False)
    await state.set_state(OrderStates.confirming_address)
    await message.answer(
        ORDER_ADDRESS_CONFIRM_TEMPLATE.format(address=address),
        reply_markup=_address_confirm_keyboard(),
    )


@router.message(
    StateFilter(OrderStates.waiting_for_location), F.text == ORDER_MANUAL_ADDRESS_BUTTON
)
async def on_manual_address_chosen(message: Message, state: FSMContext) -> None:
    await state.set_state(OrderStates.waiting_for_manual_address)
    await message.answer(ORDER_MANUAL_ADDRESS_PROMPT, reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(OrderStates.waiting_for_location))
async def on_unexpected_input_in_location_step(message: Message) -> None:
    await message.answer(ORDER_UNEXPECTED_INPUT_IN_LOCATION_STEP)


@router.callback_query(
    StateFilter(OrderStates.confirming_address), F.data == ADDRESS_EDIT_CALLBACK_DATA
)
async def on_address_edit(query: CallbackQuery, state: FSMContext) -> None:
    """
    Показывает текущий (распознанный) адрес прямо в сообщении, чтобы
    пользователь мог скопировать его и поправить только нужную часть
    (например, номер дома), а не перепечатывать целиком. Telegram Bot
    API не умеет предзаполнять поле ввода текстом — это не ограничение
    нашего кода, а то, чего в принципе нет в API; показ текста в
    сообщении для копирования — рабочий компромисс.
    """
    data = await state.get_data()
    order_draft = data.get(ORDER_DRAFT_KEY) or {}
    current_address = order_draft.get("address", "")

    await state.set_state(OrderStates.waiting_for_manual_address)
    if current_address:
        await query.message.edit_text(
            ORDER_ADDRESS_EDIT_PROMPT_TEMPLATE.format(address=current_address)
        )
    else:
        await query.message.edit_text(ORDER_MANUAL_ADDRESS_PROMPT)
    await query.answer()


@router.message(StateFilter(OrderStates.waiting_for_manual_address), F.text)
async def on_manual_address_received(
    message: Message,
    state: FSMContext,
    geocoding: GeocodingAdapter,
    settings: Settings,
    sheets: SheetsClient,
) -> None:
    address_text = message.text.strip()
    geocode_result = await geocoding.geocode(address_text)
    if geocode_result is None:
        await message.answer(ORDER_ADDRESS_NOT_FOUND)
        return  # остаёмся в том же состоянии — просим ввести ещё раз

    if not is_point_in_zone(
        geocode_result.lat,
        geocode_result.lon,
        settings.ZONE_CENTER_LAT,
        settings.ZONE_CENTER_LON,
        settings.ZONE_RADIUS_KM,
    ):
        # См. пояснение в on_location_received — не сбрасываем состояние,
        # остаёмся в waiting_for_manual_address, разрешаем ввести другой адрес.
        # Если геокодирование само по себе было неточным (partial_match) —
        # объясняем это явно, а не просто говорим "вне зоны" (реальный
        # фидбэк из живого тестирования: пользователь не понимал, почему
        # выдуманный адрес не дал "не удалось распознать").
        if geocode_result.partial_match:
            await message.answer(ORDER_ZONE_OUTSIDE_UNCERTAIN_MESSAGE)
        else:
            await message.answer(ORDER_ZONE_OUTSIDE_MESSAGE)
        await message.answer(ORDER_MANUAL_ADDRESS_PROMPT)
        return

    if geocode_result.partial_match:
        # Google не нашёл адрес точно и "откатился" на ближайший
        # распознанный уровень (например, несуществующая улица →
        # реальный район) — предупреждаем, но НЕ блокируем заказ (точность
        # адреса и так не проверяется строго по ТЗ §7.4.1, курьер уточнит
        # детали при контакте с клиентом).
        await message.answer(ORDER_ADDRESS_PARTIAL_MATCH_WARNING)

    await _update_order_draft(
        state,
        lat=geocode_result.lat,
        lon=geocode_result.lon,
        address=address_text,
        address_manually_edited=True,
        address_partial_match=geocode_result.partial_match,
    )
    await _request_contact(message, state, from_user=message.from_user)


@router.callback_query(
    StateFilter(OrderStates.confirming_address), F.data == ADDRESS_CONFIRM_CALLBACK_DATA
)
async def on_address_confirmed(
    query: CallbackQuery, state: FSMContext, sheets: SheetsClient
) -> None:
    await _request_contact(query.message, state, from_user=query.from_user)
    await query.answer()


# ---------------------------------------------------------------------- #
# Шаг 2: контакт для курьера (7.4.2)
# ---------------------------------------------------------------------- #


async def _request_contact(message: Message, state: FSMContext, *, from_user) -> None:
    """
    По просьбе продукта (после живого тестирования): телефон теперь
    запрашивается ВСЕГДА, а не только при отсутствии @username — так
    администратору проще найти пользователя при необходимости. Если
    @username есть — сохраняем его сразу, но всё равно просим телефон
    следующим шагом (оба значения хранятся параллельно, не взаимоисключающе).
    """
    username = from_user.username if from_user else None
    if username:
        await _update_order_draft(state, contact_username=username)

    await state.set_state(OrderStates.waiting_for_contact)
    await message.answer(ORDER_CONTACT_PROMPT, reply_markup=_contact_keyboard())


@router.message(StateFilter(OrderStates.waiting_for_contact), F.contact)
async def on_contact_received(
    message: Message, state: FSMContext, sheets: SheetsClient
) -> None:
    contact = message.contact
    if message.from_user and contact.user_id and contact.user_id != message.from_user.id:
        # Переслали чужой контакт вместо своего — просим ещё раз, тем же способом.
        await message.answer(ORDER_CONTACT_PROMPT, reply_markup=_contact_keyboard())
        return

    # Не затираем contact_username, если он уже был сохранён шагом раньше —
    # username и телефон теперь хранятся оба, не взаимоисключающе.
    await _update_order_draft(state, contact_phone=contact.phone_number)
    await message.answer(ORDER_CONTACT_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())
    await _after_phone_received(message, state, sheets)


@router.message(StateFilter(OrderStates.waiting_for_contact), F.text)
async def on_contact_typed_as_text(
    message: Message, state: FSMContext, sheets: SheetsClient
) -> None:
    """
    Фоллбэк: пользователь напечатал номер текстом вместо нажатия кнопки
    request_contact — та же история, что и с геолокацией на Day 3
    (Telegram Desktop может не поддерживать кнопку так, как ожидается —
    см. CHECKLIST.md). Раньше такое сообщение просто не попадало ни в
    один хендлер (реальный баг из живого тестирования — полная тишина).
    Базовая проверка формата (не строгая валидация конкретной страны).
    """
    phone = _normalize_phone_candidate(message.text)
    if phone is None:
        await message.answer(ORDER_CONTACT_INVALID_FORMAT_MESSAGE)
        return  # остаёмся в том же состоянии — просим ввести ещё раз

    await _update_order_draft(state, contact_phone=phone)
    await message.answer(ORDER_CONTACT_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())
    await _after_phone_received(message, state, sheets)


async def _after_phone_received(message: Message, state: FSMContext, sheets: SheetsClient) -> None:
    """
    Живой фидбэк: опечатка в номере (лишняя/недостающая цифра) замечена
    только на итоговом подтверждении — пользователю пришлось заново
    проходить весь чекаут, что рискует задвоением заказа, если он успел
    подтвердить оба раза. Теперь — отдельное подтверждение номера сразу
    после ввода, «Изменить» здесь меняет ТОЛЬКО номер (не весь чекаут).

    Если @username уже есть — этот шаг пропускается: админ/курьер и так
    смогут найти пользователя по юзернейму, даже если номер окажется
    неверным/недоступным (продуктовое решение по вашему запросу).
    """
    data = await state.get_data()
    order_draft = data.get(ORDER_DRAFT_KEY) or {}
    if order_draft.get("contact_username"):
        await _show_final_confirmation(message, state, sheets)
        return

    await state.set_state(OrderStates.confirming_phone)
    await message.answer(
        ORDER_PHONE_CONFIRM_TEMPLATE.format(phone=order_draft.get("contact_phone", "")),
        reply_markup=_phone_confirm_keyboard(),
    )


@router.callback_query(
    StateFilter(OrderStates.confirming_phone), F.data == PHONE_CONFIRM_CALLBACK_DATA
)
async def on_phone_confirmed(
    query: CallbackQuery, state: FSMContext, sheets: SheetsClient
) -> None:
    await _show_final_confirmation(query.message, state, sheets)
    await query.answer()


@router.callback_query(
    StateFilter(OrderStates.confirming_phone), F.data == PHONE_EDIT_CALLBACK_DATA
)
async def on_phone_edit(query: CallbackQuery, state: FSMContext) -> None:
    """Меняет ТОЛЬКО номер телефона — не сбрасывает адрес/корзину/username."""
    await state.set_state(OrderStates.waiting_for_contact)
    await query.message.answer(ORDER_CONTACT_PROMPT, reply_markup=_contact_keyboard())
    await query.answer()


# ---------------------------------------------------------------------- #
# Шаг 3: итоговое подтверждение (7.4.3)
# ---------------------------------------------------------------------- #


async def _show_final_confirmation(
    message: Message, state: FSMContext, sheets: SheetsClient
) -> None:
    data = await state.get_data()
    order_draft = data.get(ORDER_DRAFT_KEY) or {}
    order_kind = order_draft.get("order_kind", "standard")

    if order_kind == "custom":
        # Нестандартный заказ (ТЗ §7.3.4) — нет каталожных позиций и
        # корзины, состав заказа — свободный текст из order_draft,
        # заданный при подтверждении Админом (см. handlers/custom_order.py).
        merchant_name = order_draft.get("merchant_name") or order_draft.get("merchant_id", "")
        lines = [ORDER_SUMMARY_HEADER, "", ORDER_SUMMARY_MERCHANT_LINE.format(name=merchant_name)]
        lines.append(ORDER_SUMMARY_ADDRESS_LINE.format(address=order_draft.get("address", "")))
        if order_draft.get("contact_username"):
            lines.append(
                ORDER_SUMMARY_CONTACT_USERNAME_LINE.format(username=order_draft["contact_username"])
            )
        if order_draft.get("contact_phone"):
            lines.append(ORDER_SUMMARY_CONTACT_PHONE_LINE.format(phone=order_draft["contact_phone"]))
        lines.append("")
        lines.append(
            ORDER_SUMMARY_CUSTOM_DESCRIPTION_LINE.format(
                description=order_draft.get("custom_description", "")
            )
        )
        lines.append("")
        lines.append(ORDER_SUMMARY_PAYMENT_NOTE)
        await state.set_state(OrderStates.confirming_order)
        await message.answer("\n".join(lines), reply_markup=_final_confirmation_keyboard())
        return

    cart = data.get(CART_STATE_KEY)
    if not cart or not cart.get("items"):
        await message.answer(CART_EMPTY_ALERT)
        await state.set_state(None)
        return

    merchants = await sheets.get_merchants()
    merchant = next((m for m in merchants if m.merchant_id == cart["merchant_id"]), None)
    merchant_name = merchant.name if merchant else cart["merchant_id"]

    items = items_for_merchant(await sheets.get_items(), cart["merchant_id"])
    item_by_id = {i.item_id: i for i in items}

    text = format_order_summary(
        merchant_name,
        order_draft.get("address", ""),
        order_draft.get("contact_username"),
        order_draft.get("contact_phone"),
        cart["items"],
        item_by_id,
    )
    await state.set_state(OrderStates.confirming_order)
    await message.answer(text, reply_markup=_final_confirmation_keyboard())


@router.callback_query(
    StateFilter(OrderStates.confirming_order), F.data == ORDER_BACK_TO_CART_CALLBACK_DATA
)
async def on_back_to_cart(
    query: CallbackQuery, sheets: SheetsClient, state: FSMContext
) -> None:
    await state.set_state(None)
    ok = await render_cart_view(query.message, sheets, state)
    if not ok:
        await query.answer(CART_EMPTY_ALERT, show_alert=True)
        return
    await query.answer()


@router.callback_query(
    StateFilter(OrderStates.confirming_order), F.data == ORDER_CONFIRM_CALLBACK_DATA
)
@resilient(ORDER_SAVE_FAILED_MESSAGE)
async def on_order_confirmed(
    query: CallbackQuery,
    state: FSMContext,
    sheets: SheetsClient,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
) -> None:
    # ЗАЩИТА ОТ ДУБЛИРОВАНИЯ ЗАКАЗА (живой баг: order_id=031/032 из одного
    # чекаута). Хендлер ниже выполняется 4-6 секунд (повторное чтение
    # Sheets, запись заказа, рассылка курьерам), а корзина/состояние
    # очищаются только в самом конце. Если за это время прилетает второй
    # апдейт с тем же callback_data (двойной тап нетерпеливого пользователя
    # или ретрай на нестабильном соединении — см. "Connection reset by
    # peer" рядом по времени в логах), второй вызов проходит проверку
    # "cart есть" раньше, чем первый успевает её очистить, и создаёт
    # второй заказ на тот же чекаут. Лок на user_id закрывает эту гонку:
    # второй вызов сразу отклоняется, не доходя до _generate_order_id().
    user_id = query.from_user.id if query.from_user else ""
    lock_key = ORDER_CONFIRM_LOCK_KEY_TEMPLATE.format(user_id=user_id)
    lock_acquired = await redis.set(lock_key, "1", nx=True, ex=ORDER_CONFIRM_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.warning(
            "on_order_confirmed: повторный вызов для user_id=%s заблокирован "
            "(заказ уже обрабатывается предыдущим вызовом)",
            user_id,
        )
        await query.answer()
        return

    try:
        data = await state.get_data()
        order_draft = data.get(ORDER_DRAFT_KEY) or {}
        if order_draft.get("order_kind") == "custom":
            # Нестандартный заказ (ТЗ §7.3.4) — своя ветка, без корзины/
            # каталога. Лок выше защищает и эту ветку от двойного тапа
            # ровно так же, как стандартную.
            await _confirm_custom_order(
                query, state, sheets, settings, redis, bot, scheduler, order_draft
            )
            return

        cart = data.get(CART_STATE_KEY)
        if not cart or not cart.get("items"):
            await query.answer(CART_EMPTY_ALERT, show_alert=True)
            return

        # Повторная проверка часов работы (защита от "начал чекаут в 20:14,
        # подтвердил в 20:16" — время могло пройти за время заполнения адреса
        # и контакта, см. ТЗ §6: проверка "при каждой попытке действия,
        # влияющего на создание заказа").
        status = await get_service_status(settings, redis)
        if status != ServiceStatus.OPEN:
            await query.message.answer(_service_status_message(status, settings))
            await query.answer()
            return

        # Заглушка оплаты (ТЗ §5, §13: PAYMENT_ENABLED). В MVP всегда False —
        # реальная обработка Papara/Iyzico не реализована (см. handlers/payment.py),
        # поэтому явно отказываем, а не тихо игнорируем флаг.
        if settings.PAYMENT_ENABLED:
            logger.error(
                "PAYMENT_ENABLED=True, но обработка оплаты не реализована (вне плана MVP)"
            )
            await query.message.answer(PAYMENT_NOT_IMPLEMENTED_MESSAGE)
            await query.answer()
            return

        merchant_id = cart["merchant_id"]
        # Перепроверка наличия (ТЗ, план Day 3) — bypass кеша, читаем свежие данные.
        fresh_items = items_for_merchant(await sheets.get_items(force_refresh=True), merchant_id)
        fresh_by_id = {i.item_id: i for i in fresh_items}

        has_unavailable_items = any(
            fresh_by_id.get(item_id) is None or not fresh_by_id[item_id].is_available
            for item_id in cart["items"]
        )

        if has_unavailable_items:
            await query.message.answer(ORDER_ITEMS_UNAVAILABLE)
            await query.answer()
            return

        merchants = await sheets.get_merchants()
        merchant = next((m for m in merchants if m.merchant_id == merchant_id), None)
        merchant_name = merchant.name if merchant else merchant_id

        order_draft = data.get(ORDER_DRAFT_KEY) or {}
        address_manually_edited = bool(order_draft.get("address_manually_edited", False))

        items_snapshot = [
            {
                "item_id": item_id,
                "name": fresh_by_id[item_id].name,
                "qty": qty,
                "price_try": fresh_by_id[item_id].price_try,
            }
            for item_id, qty in cart["items"].items()
        ]
        total_try = sum(item["qty"] * item["price_try"] for item in items_snapshot)

        order_id = await _generate_order_id(redis)

        order_fields = {
            "order_id": order_id,
            "timestamp_created": now_local_str(settings),
            "order_kind": "standard",
            "user_id": str(user_id),
            "username": order_draft.get("contact_username") or "",
            "phone": order_draft.get("contact_phone") or "",
            "merchant_id": merchant_id,
            "merchant_name": merchant_name,
            "items_json": json.dumps(items_snapshot, ensure_ascii=False),
            "total_try": str(total_try),
            "delivery_address": order_draft.get("address", ""),
            "geo_lat": str(order_draft.get("lat", "")),
            "geo_lon": str(order_draft.get("lon", "")),
            "address_manually_edited": str(address_manually_edited),
            "is_custom_order": "FALSE",
            "status": "new",
            # Человекочитаемая сводка состава — items_json остаётся техническим
            # снимком для будущего программного использования (Day 5+), а это
            # поле — то же самое человеку для чтения глазами (живой фидбэк Day 4).
            "admin_notes": format_order_items_text(items_snapshot),
        }

        try:
            await sheets.append_order(order_fields)
        except SheetsWriteError as exc:
            # КРИТИЧНО: если запись не удалась — НЕ говорим "заказ принят" и
            # НЕ чистим корзину, иначе заказ потеряется бесследно. Пользователь
            # может просто нажать "Подтвердить" ещё раз.
            logger.error("Не удалось записать заказ %s в Sheets: %s", order_id, exc)
            await query.message.answer(ORDER_SAVE_FAILED_MESSAGE)
            await query.answer()
            return

        logger.info(
            "order_placed: order_id=%s merchant_id=%s items=%s contact_username=%s "
            "contact_phone=%s address=%r manually_edited=%s partial_match=%s",
            order_id,
            merchant_id,
            cart["items"],
            order_draft.get("contact_username"),
            order_draft.get("contact_phone"),
            order_draft.get("address"),
            address_manually_edited,
            order_draft.get("address_partial_match", False),
        )
        await sheets.append_event(
            event_type="order_placed", actor_role="client", actor_id=str(user_id), order_id=order_id
        )

        # new → offered/no_courier (ТЗ §8.2) — рассылка курьерам на смене
        # (или синхронная эскалация Админу, если на смене никого нет).
        # Заказ уже сохранён в Sheets (append_order выше) независимо от
        # результата диспетчеризации — сбой здесь не должен трактоваться как
        # сбой ОФОРМЛЕНИЯ заказа (клиент уже получит "Заказ принят" ниже),
        # только залогирован. Повторный запуск диспетчеризации вручную для
        # заказа, где она упала, — вне текущего скоупа (Day 8, админ-команды).
        try:
            await send_offer_to_couriers(
                bot,
                sheets,
                redis,
                settings,
                scheduler,
                order_id=order_id,
                merchant_name=merchant_name,
                merchant_description=merchant.description if merchant else "",
                delivery_address=order_draft.get("address", ""),
                total_try=str(total_try),
            )
        except Exception:  # noqa: BLE001 — сбой диспетчеризации не должен ронять весь хендлер
            logger.exception("Диспетчеризация заказа %s упала с исключением", order_id)

        if address_manually_edited:
            confirmation_suffix = ORDER_ACCEPTED_MANUAL_ADDRESS_TEMPLATE.format(
                address=order_draft.get("address", "")
            )
        else:
            confirmation_suffix = ORDER_ACCEPTED_DEFAULT

        await state.update_data(**{CART_STATE_KEY: None, ORDER_DRAFT_KEY: None})
        await state.set_state(None)

        # ВАЖНО (живой фидбэк): edit_text() ЗАМЕНЯЕТ содержимое сообщения
        # целиком — раньше это стирало итоговую сводку (с телефоном, составом,
        # адресом), оставляя только "Заказ принят" без возможности свериться,
        # что именно было заказано. Теперь дописываем подтверждение К сводке,
        # а не вместо нёе; кнопки убираем (reply_markup=None) — действие уже
        # совершено, "Подтвердить"/"Изменить" больше не имеют смысла.
        summary_text = query.message.text or ""
        final_text = f"{summary_text}\n\n{confirmation_suffix}" if summary_text else confirmation_suffix
        try:
            await query.message.edit_text(final_text, reply_markup=_order_again_keyboard())
        except TelegramBadRequest as exc:
            # Безобидный симптом двойного вызова (второй апдейт с тем же
            # текстом) — не стоит шуметь ERROR-логом через telegram_safety.
            if "message is not modified" not in str(exc):
                raise
        await query.answer()
    finally:
        await redis.delete(lock_key)


async def _confirm_custom_order(
    query: CallbackQuery,
    state: FSMContext,
    sheets: SheetsClient,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    order_draft: dict,
) -> None:
    """
    Ветка order_kind="custom" (ТЗ §7.3.4) для on_order_confirmed —
    вызывается из-под того же redis-лока (см. вызывающий код), поэтому
    защита от двойного тапа уже обеспечена снаружи. Зеркалит стандартную
    ветку (проверка часов работы, заглушка оплаты, генерация order_id,
    запись в Sheets, диспетчеризация курьерам, финальное сообщение), но
    без корзины/каталога — состав заказа уже зафиксирован как свободный
    текст в order_draft при подтверждении Админом.
    """
    status = await get_service_status(settings, redis)
    if status != ServiceStatus.OPEN:
        await query.message.answer(_service_status_message(status, settings))
        await query.answer()
        return

    if settings.PAYMENT_ENABLED:
        logger.error(
            "PAYMENT_ENABLED=True, но обработка оплаты не реализована (вне плана MVP)"
        )
        await query.message.answer(PAYMENT_NOT_IMPLEMENTED_MESSAGE)
        await query.answer()
        return

    merchant_id = order_draft.get("merchant_id", "")
    merchants = await sheets.get_merchants()
    merchant = next((m for m in merchants if m.merchant_id == merchant_id), None)
    merchant_name = merchant.name if merchant else order_draft.get("merchant_name", merchant_id)

    address_manually_edited = bool(order_draft.get("address_manually_edited", False))
    user_id = query.from_user.id if query.from_user else ""
    order_id = await _generate_order_id(redis)
    custom_description = order_draft.get("custom_description", "")

    order_fields = {
        "order_id": order_id,
        "timestamp_created": now_local_str(settings),
        "order_kind": "custom",
        "user_id": str(user_id),
        "username": order_draft.get("contact_username") or "",
        "phone": order_draft.get("contact_phone") or "",
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "items_json": "",
        "total_try": "",
        "delivery_address": order_draft.get("address", ""),
        "geo_lat": str(order_draft.get("lat", "")),
        "geo_lon": str(order_draft.get("lon", "")),
        "address_manually_edited": str(address_manually_edited),
        "is_custom_order": "TRUE",
        "custom_description": custom_description,
        "custom_photo_file_id": order_draft.get("custom_photo_file_id", ""),
        "status": "new",
        # Как и у standard-заказов: admin_notes — человекочитаемая копия
        # состава для глаз Админа. У custom это тот же текст, что и
        # custom_description — состав и так текстовый, дублировать особо
        # нечего, но поле держим заполненным для единообразия со
        # standard-заказами (там оно тоже всегда непустое).
        "admin_notes": custom_description,
    }

    try:
        await sheets.append_order(order_fields)
    except SheetsWriteError as exc:
        logger.error("Не удалось записать нестандартный заказ %s в Sheets: %s", order_id, exc)
        await query.message.answer(ORDER_SAVE_FAILED_MESSAGE)
        await query.answer()
        return

    logger.info(
        "order_placed: order_id=%s merchant_id=%s order_kind=custom contact_username=%s "
        "contact_phone=%s address=%r manually_edited=%s",
        order_id,
        merchant_id,
        order_draft.get("contact_username"),
        order_draft.get("contact_phone"),
        order_draft.get("address"),
        address_manually_edited,
    )
    await sheets.append_event(
        event_type="order_placed", actor_role="client", actor_id=str(user_id), order_id=order_id
    )

    try:
        await send_offer_to_couriers(
            bot,
            sheets,
            redis,
            settings,
            scheduler,
            order_id=order_id,
            merchant_name=merchant_name,
            merchant_description=merchant.description if merchant else "",
            delivery_address=order_draft.get("address", ""),
            total_try="",
            custom_description=custom_description,
        )
    except Exception:  # noqa: BLE001 — сбой диспетчеризации не должен ронять весь хендлер
        logger.exception("Диспетчеризация нестандартного заказа %s упала с исключением", order_id)

    if address_manually_edited:
        confirmation_suffix = ORDER_ACCEPTED_MANUAL_ADDRESS_TEMPLATE.format(
            address=order_draft.get("address", "")
        )
    else:
        confirmation_suffix = ORDER_ACCEPTED_DEFAULT

    await state.update_data(**{ORDER_DRAFT_KEY: None})
    await state.set_state(None)

    summary_text = query.message.text or ""
    final_text = f"{summary_text}\n\n{confirmation_suffix}" if summary_text else confirmation_suffix
    try:
        await query.message.edit_text(final_text, reply_markup=_order_again_keyboard())
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
    await query.answer()


def _order_again_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ORDER_AGAIN_BUTTON, callback_data=ORDER_AGAIN_CALLBACK_DATA)]
        ]
    )


@router.callback_query(F.data == ORDER_AGAIN_CALLBACK_DATA)
async def on_order_again(query: CallbackQuery, sheets: SheetsClient) -> None:
    """
    По запросу: удобный вход в новый заказ прямо с экрана «Заказ принят»,
    без необходимости искать /start в истории чата. Сразу в категории —
    приветствие и оферта уже были показаны этому пользователю раньше.
    """
    await show_categories(query.message, sheets)
    await query.answer()
