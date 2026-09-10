"""
P2P-доставка «из А в Б» (order_kind="p2p", ТЗ §7.6).

Флоу: кнопка [📦 Доставить что-угодно (P2P)] (handlers/catalog.py) →
описание груза (текст+опционально фото) → точка А (геолокация/адрес) →
точка Б (геолокация/адрес) → проверка ОБЕИХ точек на зону ОДНОВРЕМЕННО
(ТЗ §7.6.4 — в отличие от обычного чекаута, где зона проверяется сразу
при вводе каждой точки; здесь оба адреса сначала собираются, и только
потом проверяются вместе — если хоть одна вне зоны, весь черновик
сбрасывается, "Заказ не создаётся", а не "введите эту точку заново") →
контакт → итог → [Отправить на проверку] → строка в Sheets создаётся
СРАЗУ со статусом pending_review (в отличие от custom_order.py, где
Sheets-строка появляется только ПОСЛЕ решения Админа) → Админ:
/approve_p2p_[order_id] (→ offered, обычная диспетчеризация курьерам,
ТЗ §8) / /reject_p2p_[order_id] [причина] → по P2P_REVIEW_TIMEOUT_MIN —
НЕ авто-отклонение (в отличие от custom_order), а только напоминание
Админу (см. services/timeouts.py::_check_p2p_review_reminder).

Контакт (username + телефон) и генерация order_id переиспользуют
приватные хелперы handlers/order.py (_normalize_phone_candidate,
_generate_order_id) — это чисто техническая логика без привязки к
конкретному order_kind, дублировать её не имеет смысла (тот же подход,
что и импорт start_checkout_for_user в handlers/custom_order.py).
"""

from __future__ import annotations

import logging

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
from handlers.catalog import P2P_CALLBACK_DATA, show_categories
from handlers.order import _generate_order_id, _normalize_phone_candidate
from services.dispatch import send_offer_to_couriers
from services.geocoding import GeocodingAdapter
from services.service_hours import ServiceStatus, get_service_status
from services.sheets import SheetsClient, SheetsWriteError
from services.timeouts import cancel_p2p_review_reminder, schedule_p2p_review_reminder
from services.zone import haversine_distance_km, is_point_in_zone
from states.user_states import P2PStates
from utils.timefmt import now_local_str
from texts.ru import (
    ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE,
    ADMIN_P2P_APPROVED_ACK,
    ADMIN_P2P_NOT_FOUND_MESSAGE,
    ADMIN_P2P_REJECTED_ACK,
    ADMIN_REJECT_P2P_REASON_PROMPT,
    ADMIN_P2P_REVIEW_TEMPLATE,
    ORDER_ADDRESS_NOT_FOUND,
    ORDER_ADDRESS_PARTIAL_MATCH_WARNING,
    ORDER_CONTACT_BUTTON,
    ORDER_CONTACT_INVALID_FORMAT_MESSAGE,
    ORDER_CONTACT_RECEIVED_ACK,
    ORDER_MANUAL_ADDRESS_PROMPT,
    ORDER_SAVE_FAILED_MESSAGE,
    P2P_BACK_TO_START_BUTTON,
    P2P_CANCEL_BUTTON,
    P2P_CANCELLED_ACK,
    P2P_CONTACT_PROMPT,
    P2P_DESCRIPTION_PROMPT,
    P2P_DROPOFF_PROMPT,
    P2P_EDIT_BUTTON,
    P2P_EMPTY_TEXT_MESSAGE,
    P2P_LATE_PHOTO_ATTACHED_ACK,
    P2P_LOCATION_BUTTON,
    P2P_LOCATION_RECEIVED_ACK,
    P2P_MANUAL_ADDRESS_BUTTON,
    P2P_OUT_OF_ZONE_MESSAGE_TEMPLATE,
    P2P_PICKUP_PROMPT,
    P2P_REJECTED_CLIENT_TEMPLATE,
    P2P_REJECTED_REASON_SUFFIX_TEMPLATE,
    P2P_APPROVED_CLIENT_MESSAGE,
    P2P_SUBMIT_BUTTON,
    P2P_SUBMITTED_ACK,
    P2P_SUMMARY_CONTACT_PHONE_LINE,
    P2P_SUMMARY_CONTACT_USERNAME_LINE,
    P2P_SUMMARY_DROPOFF_LINE,
    P2P_SUMMARY_HEADER,
    P2P_SUMMARY_PICKUP_LINE,
    P2P_SUMMARY_REWARD_NOTE,
    P2P_SUMMARY_WHAT_LINE,
    P2P_UNEXPECTED_INPUT_IN_LOCATION_STEP,
    PAYMENT_NOT_IMPLEMENTED_MESSAGE,
    SERVICE_CLOSED_MESSAGE,
    SERVICE_LAST_ORDER_PASSED_MESSAGE,
    SERVICE_MANUALLY_STOPPED_MESSAGE,
)

logger = logging.getLogger(__name__)

router = Router(name=__name__)

P2P_DRAFT_KEY = "p2p_draft"

P2P_CANCEL_CALLBACK_DATA = "p2p_cancel"
P2P_BACK_TO_START_CALLBACK_DATA = "p2p_back_to_start"
P2P_SUBMIT_CALLBACK_DATA = "p2p_submit"
P2P_EDIT_CALLBACK_DATA = "p2p_edit"

P2P_SUBMIT_LOCK_KEY_TEMPLATE = "p2p_submit_lock:{user_id}"
# Тот же паттерн и та же причина, что у ORDER_CONFIRM_LOCK_TTL_SECONDS в
# handlers/order.py (см. подробный комментарий там) — защита от
# двойного тапа "Отправить на проверку".
P2P_SUBMIT_LOCK_TTL_SECONDS = 30

# Живой баг из тестирования: та же гонка состояний, что и утреннее
# дублирование заказов — между проверкой "статус ещё pending_review" и
# фактической записью нового статуса в cmd_approve_p2p/cmd_reject_p2p
# проходит ощутимое время (уведомление клиента, запись события, сама
# диспетчеризация курьерам для approve), и два быстрых повтора команды
# (двойной тап/ретрай Telegram/нетерпеливый Админ) успевают ОБА пройти
# проверку до того, как первый вызов её изменит — заказ уходит
# курьерам дважды. Ключ на order_id (не на admin_id) — защищает и от
# гонки между ДВУМЯ РАЗНЫМИ админами, решившими одновременно
# обработать один и тот же заказ.
P2P_ADMIN_ACTION_LOCK_KEY_TEMPLATE = "p2p_admin_action_lock:{order_id}"
P2P_ADMIN_ACTION_LOCK_TTL_SECONDS = 30


def _service_status_message(status: ServiceStatus, settings: Settings) -> str:
    if status == ServiceStatus.MANUALLY_STOPPED:
        return SERVICE_MANUALLY_STOPPED_MESSAGE
    if status == ServiceStatus.LAST_ORDER_PASSED:
        return SERVICE_LAST_ORDER_PASSED_MESSAGE.format(
            open_time=settings.SERVICE_OPEN, close_time=settings.SERVICE_CLOSE
        )
    return SERVICE_CLOSED_MESSAGE.format(open_time=settings.SERVICE_OPEN, close_time=settings.SERVICE_CLOSE)


async def _update_p2p_draft(state: FSMContext, **fields: object) -> None:
    data = await state.get_data()
    draft = dict(data.get(P2P_DRAFT_KEY) or {})
    draft.update(fields)
    await state.update_data(**{P2P_DRAFT_KEY: draft})


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=P2P_CANCEL_BUTTON, callback_data=P2P_CANCEL_CALLBACK_DATA)]
        ]
    )


def _location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=P2P_LOCATION_BUTTON, request_location=True)],
            [KeyboardButton(text=P2P_MANUAL_ADDRESS_BUTTON)],
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


def _out_of_zone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=P2P_BACK_TO_START_BUTTON, callback_data=P2P_BACK_TO_START_CALLBACK_DATA
                )
            ]
        ]
    )


def _final_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=P2P_SUBMIT_BUTTON, callback_data=P2P_SUBMIT_CALLBACK_DATA)],
            [InlineKeyboardButton(text=P2P_EDIT_BUTTON, callback_data=P2P_EDIT_CALLBACK_DATA)],
        ]
    )


def _contact_label(username: str | None, user_id: int | str) -> str:
    return f"@{username}" if username else f"ID {user_id}"


async def _reset_p2p(state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(**{P2P_DRAFT_KEY: None})


# ---------------------------------------------------------------------- #
# Вход + отмена в любой момент флоу
# ---------------------------------------------------------------------- #


@router.callback_query(F.data == P2P_CALLBACK_DATA)
async def on_p2p_entry(query: CallbackQuery, state: FSMContext, settings: Settings, redis: Redis) -> None:
    status = await get_service_status(settings, redis)
    if status != ServiceStatus.OPEN:
        await query.answer(_service_status_message(status, settings), show_alert=True)
        return

    await state.update_data(**{P2P_DRAFT_KEY: {}})
    await state.set_state(P2PStates.waiting_for_description)
    await query.message.answer(P2P_DESCRIPTION_PROMPT, reply_markup=_cancel_keyboard())
    await query.answer()


@router.callback_query(F.data == P2P_CANCEL_CALLBACK_DATA)
async def on_p2p_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await _reset_p2p(state)
    await query.message.edit_text(P2P_CANCELLED_ACK)
    await query.answer()


@router.callback_query(F.data == P2P_BACK_TO_START_CALLBACK_DATA)
async def on_p2p_back_to_start(query: CallbackQuery, state: FSMContext, sheets: SheetsClient) -> None:
    """ТЗ §7.6.4: если хотя бы одна из точек оказалась вне зоны — заказ
    не создаётся, черновик полностью сбрасывается, пользователь
    возвращается к списку категорий (не к первому шагу P2P — если он
    захочет попробовать снова, это равнозначно новому /start-подобному
    входу через кнопку [📦 Доставить что-угодно])."""
    await _reset_p2p(state)
    await show_categories(query.message, sheets)
    await query.answer()


# ---------------------------------------------------------------------- #
# Шаг 1: описание груза (7.6.1)
# ---------------------------------------------------------------------- #


@router.message(StateFilter(P2PStates.waiting_for_description))
async def on_p2p_description_received(message: Message, state: FSMContext) -> None:
    text = (message.text or message.caption or "").strip()
    photo_file_id = message.photo[-1].file_id if message.photo else ""

    if not text:
        await message.answer(P2P_EMPTY_TEXT_MESSAGE)
        return

    await _update_p2p_draft(state, description=text, photo_file_id=photo_file_id)
    await state.set_state(P2PStates.waiting_for_pickup_location)
    await message.answer(P2P_PICKUP_PROMPT, reply_markup=_location_keyboard())


@router.message(
    StateFilter(
        P2PStates.waiting_for_pickup_location,
        P2PStates.waiting_for_pickup_manual_address,
        P2PStates.waiting_for_dropoff_location,
        P2PStates.waiting_for_dropoff_manual_address,
        P2PStates.waiting_for_contact,
        P2PStates.confirming_order,
    ),
    F.photo,
)
async def on_p2p_late_photo_attached(message: Message, state: FSMContext) -> None:
    """
    Живой баг из тестирования: on_p2p_description_received ожидает фото
    ВМЕСТЕ с текстом описания в одном сообщении (message.photo рядом с
    message.text/caption) — но реальный пользователь может сначала
    отправить текст, а фото прикрепить ОТДЕЛЬНЫМ следующим сообщением.
    К этому моменту состояние уже ушло на следующий шаг (точка А/Б/
    контакт), и фото без этого хендлера попадало бы в "catch-all
    неожиданного ввода" соответствующего шага — молча терялось.

    Регистрация ВЫШЕ per-шаговых catch-all-хендлеров (on_p2p_pickup_
    unexpected_input и т.п.) обязательна: aiogram пробует хендлеры
    одного роутера в порядке объявления в файле, а F.photo для
    Message означает отсутствие message.text (у фото — caption, не
    text), поэтому конфликта с текстовыми фильтрами тех же шагов нет —
    порядок регистрации важен только относительно catch-all'ов без
    какого-либо F-фильтра.
    """
    photo_file_id = message.photo[-1].file_id
    await _update_p2p_draft(state, photo_file_id=photo_file_id)
    await message.answer(P2P_LATE_PHOTO_ATTACHED_ACK)


# ---------------------------------------------------------------------- #
# Шаг 2/3: точки А и Б (7.6.2/7.6.3) — почти идентичная пара хендлеров,
# отличаются только тем, какое поле драфта пишут и куда ведут дальше.
# Не обобщено в один параметризованный хендлер, т.к. aiogram-декораторы
# роутера регистрируются на уровне модуля и должны быть отдельными
# функциями с разными StateFilter — попытка параметризации потребовала
# бы либо фабрики хендлеров (менее читаемо), либо ручной диспетчеризации
# внутри одной функции (та же длина кода, меньше ясности "что на каком
# шаге происходит" при беглом чтении файла).
# ---------------------------------------------------------------------- #


async def _process_pickup_address_text(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter, address_text: str
) -> None:
    geocode_result = await geocoding.geocode(address_text)
    if geocode_result is None:
        await message.answer(ORDER_ADDRESS_NOT_FOUND)
        return

    if geocode_result.partial_match:
        await message.answer(ORDER_ADDRESS_PARTIAL_MATCH_WARNING)

    await _update_p2p_draft(
        state,
        pickup_address=address_text,
        pickup_lat=geocode_result.lat,
        pickup_lon=geocode_result.lon,
    )
    await state.set_state(P2PStates.waiting_for_dropoff_location)
    await message.answer(P2P_DROPOFF_PROMPT, reply_markup=_location_keyboard())


@router.message(StateFilter(P2PStates.waiting_for_pickup_location), F.location)
async def on_p2p_pickup_location_received(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter
) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    await message.answer(P2P_LOCATION_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())

    address = await geocoding.reverse_geocode(lat, lon)
    if address is None:
        await _update_p2p_draft(state, pickup_lat=lat, pickup_lon=lon)
        await state.set_state(P2PStates.waiting_for_pickup_manual_address)
        await message.answer(ORDER_MANUAL_ADDRESS_PROMPT)
        return

    await _update_p2p_draft(state, pickup_address=address, pickup_lat=lat, pickup_lon=lon)
    await state.set_state(P2PStates.waiting_for_dropoff_location)
    await message.answer(P2P_DROPOFF_PROMPT, reply_markup=_location_keyboard())


@router.message(
    StateFilter(P2PStates.waiting_for_pickup_location), F.text == P2P_MANUAL_ADDRESS_BUTTON
)
async def on_p2p_pickup_manual_chosen(message: Message, state: FSMContext) -> None:
    await state.set_state(P2PStates.waiting_for_pickup_manual_address)
    await message.answer(ORDER_MANUAL_ADDRESS_PROMPT, reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(P2PStates.waiting_for_pickup_location), F.text)
async def on_p2p_pickup_text_as_address(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter
) -> None:
    """
    Живой фидбэк из тестирования: раньше на шаге "Откуда забрать?" бот
    принимал текст ТОЛЬКО после явного нажатия кнопки [✏️ Ввести адрес]
    — набранный сразу, без нажатия, адрес попадал в catch-all
    "неожиданный ввод" и заставлял тапать кнопку зря. Кнопка
    [✏️ Ввести адрес] по-прежнему работает (см. on_p2p_pickup_manual_
    chosen выше — она просто ведёт в тот же результат другим путём),
    но теперь и прямой ввод текста, минуя кнопку, тоже принимается —
    у пользователя нет причин ждать кнопку, если он и так печатает.
    """
    address_text = message.text.strip()
    if not address_text:
        await message.answer(P2P_UNEXPECTED_INPUT_IN_LOCATION_STEP)
        return
    await _process_pickup_address_text(message, state, geocoding, address_text)


@router.message(StateFilter(P2PStates.waiting_for_pickup_location))
async def on_p2p_pickup_unexpected_input(message: Message) -> None:
    """С учётом on_p2p_pickup_text_as_address (выше, ловит F.text
    раньше) и on_p2p_late_photo_attached (ловит F.photo раньше) сюда
    доходят только по-настоящему нераспознаваемые типы ввода —
    стикеры, голосовые, документы и т.п."""
    await message.answer(P2P_UNEXPECTED_INPUT_IN_LOCATION_STEP)


@router.message(StateFilter(P2PStates.waiting_for_pickup_manual_address), F.text)
async def on_p2p_pickup_manual_address_received(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter
) -> None:
    await _process_pickup_address_text(message, state, geocoding, message.text.strip())


async def _process_dropoff_address_text(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter, settings: Settings, address_text: str
) -> None:
    geocode_result = await geocoding.geocode(address_text)
    if geocode_result is None:
        await message.answer(ORDER_ADDRESS_NOT_FOUND)
        return

    if geocode_result.partial_match:
        await message.answer(ORDER_ADDRESS_PARTIAL_MATCH_WARNING)

    await _update_p2p_draft(
        state,
        dropoff_address=address_text,
        dropoff_lat=geocode_result.lat,
        dropoff_lon=geocode_result.lon,
    )
    await _finish_points_and_check_zone(message, state, settings)


@router.message(StateFilter(P2PStates.waiting_for_dropoff_location), F.location)
async def on_p2p_dropoff_location_received(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter, settings: Settings
) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    await message.answer(P2P_LOCATION_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())

    address = await geocoding.reverse_geocode(lat, lon)
    if address is None:
        await _update_p2p_draft(state, dropoff_lat=lat, dropoff_lon=lon)
        await state.set_state(P2PStates.waiting_for_dropoff_manual_address)
        await message.answer(ORDER_MANUAL_ADDRESS_PROMPT)
        return

    await _update_p2p_draft(state, dropoff_address=address, dropoff_lat=lat, dropoff_lon=lon)
    await _finish_points_and_check_zone(message, state, settings)


@router.message(
    StateFilter(P2PStates.waiting_for_dropoff_location), F.text == P2P_MANUAL_ADDRESS_BUTTON
)
async def on_p2p_dropoff_manual_chosen(message: Message, state: FSMContext) -> None:
    await state.set_state(P2PStates.waiting_for_dropoff_manual_address)
    await message.answer(ORDER_MANUAL_ADDRESS_PROMPT, reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(P2PStates.waiting_for_dropoff_location), F.text)
async def on_p2p_dropoff_text_as_address(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter, settings: Settings
) -> None:
    """Зеркало on_p2p_pickup_text_as_address для точки Б — см. его
    docstring."""
    address_text = message.text.strip()
    if not address_text:
        await message.answer(P2P_UNEXPECTED_INPUT_IN_LOCATION_STEP)
        return
    await _process_dropoff_address_text(message, state, geocoding, settings, address_text)


@router.message(StateFilter(P2PStates.waiting_for_dropoff_location))
async def on_p2p_dropoff_unexpected_input(message: Message) -> None:
    await message.answer(P2P_UNEXPECTED_INPUT_IN_LOCATION_STEP)


@router.message(StateFilter(P2PStates.waiting_for_dropoff_manual_address), F.text)
async def on_p2p_dropoff_manual_address_received(
    message: Message, state: FSMContext, geocoding: GeocodingAdapter, settings: Settings
) -> None:
    await _process_dropoff_address_text(message, state, geocoding, settings, message.text.strip())


# ---------------------------------------------------------------------- #
# Шаг 4: проверка зоны — ОБЕИХ точек сразу (7.6.4)
# ---------------------------------------------------------------------- #


async def _finish_points_and_check_zone(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    draft = data.get(P2P_DRAFT_KEY) or {}

    pickup_in_zone = is_point_in_zone(
        draft["pickup_lat"], draft["pickup_lon"],
        settings.ZONE_CENTER_LAT, settings.ZONE_CENTER_LON, settings.ZONE_RADIUS_KM,
    )
    dropoff_in_zone = is_point_in_zone(
        draft["dropoff_lat"], draft["dropoff_lon"],
        settings.ZONE_CENTER_LAT, settings.ZONE_CENTER_LON, settings.ZONE_RADIUS_KM,
    )

    if not (pickup_in_zone and dropoff_in_zone):
        if not pickup_in_zone and not dropoff_in_zone:
            point_label = "А и Б"
        elif not pickup_in_zone:
            point_label = "А"
        else:
            point_label = "Б"

        logger.info("p2p_out_of_zone: point=%s", point_label)
        await message.answer(
            P2P_OUT_OF_ZONE_MESSAGE_TEMPLATE.format(point_label=point_label),
            reply_markup=_out_of_zone_keyboard(),
        )
        # Черновик НЕ сбрасываем здесь — состояние остаётся тем, что было,
        # но пользователю доступна только кнопка "← В начало" (сброс
        # происходит в on_p2p_back_to_start). Так пользователь не рискует
        # случайно продолжить ввод текста и запутать состояние.
        return

    await _request_p2p_contact(message, state)


# ---------------------------------------------------------------------- #
# Шаг 5: контакт (7.6.5, "как в обычном заказе" — см. docstring модуля
# про упрощение: без отдельного экрана подтверждения номера, т.к. P2P и
# так проходит модерацию Админом до диспетчеризации)
# ---------------------------------------------------------------------- #


async def _request_p2p_contact(message: Message, state: FSMContext) -> None:
    from_user = message.from_user
    if from_user and from_user.username:
        await _update_p2p_draft(state, contact_username=from_user.username)

    await state.set_state(P2PStates.waiting_for_contact)
    await message.answer(P2P_CONTACT_PROMPT, reply_markup=_contact_keyboard())


@router.message(StateFilter(P2PStates.waiting_for_contact), F.contact)
async def on_p2p_contact_received(message: Message, state: FSMContext, sheets: SheetsClient) -> None:
    contact = message.contact
    if message.from_user and contact.user_id and contact.user_id != message.from_user.id:
        await message.answer(P2P_CONTACT_PROMPT, reply_markup=_contact_keyboard())
        return

    await _update_p2p_draft(state, contact_phone=contact.phone_number)
    await message.answer(ORDER_CONTACT_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())
    await _show_p2p_final_confirmation(message, state)


@router.message(StateFilter(P2PStates.waiting_for_contact), F.text)
async def on_p2p_contact_typed_as_text(message: Message, state: FSMContext, sheets: SheetsClient) -> None:
    phone = _normalize_phone_candidate(message.text)
    if phone is None:
        await message.answer(ORDER_CONTACT_INVALID_FORMAT_MESSAGE)
        return

    await _update_p2p_draft(state, contact_phone=phone)
    await message.answer(ORDER_CONTACT_RECEIVED_ACK, reply_markup=ReplyKeyboardRemove())
    await _show_p2p_final_confirmation(message, state)


# ---------------------------------------------------------------------- #
# Шаг 6: итоговое подтверждение (7.6.6)
# ---------------------------------------------------------------------- #


async def _show_p2p_final_confirmation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get(P2P_DRAFT_KEY) or {}

    lines = [P2P_SUMMARY_HEADER, ""]
    lines.append(P2P_SUMMARY_WHAT_LINE.format(description=draft.get("description", "")))
    lines.append(P2P_SUMMARY_PICKUP_LINE.format(pickup_address=draft.get("pickup_address", "")))
    lines.append(P2P_SUMMARY_DROPOFF_LINE.format(dropoff_address=draft.get("dropoff_address", "")))
    if draft.get("contact_username"):
        lines.append(P2P_SUMMARY_CONTACT_USERNAME_LINE.format(username=draft["contact_username"]))
    if draft.get("contact_phone"):
        lines.append(P2P_SUMMARY_CONTACT_PHONE_LINE.format(phone=draft["contact_phone"]))
    lines.append("")
    lines.append(P2P_SUMMARY_REWARD_NOTE)

    await state.set_state(P2PStates.confirming_order)
    await message.answer("\n".join(lines), reply_markup=_final_confirmation_keyboard())


@router.callback_query(StateFilter(P2PStates.confirming_order), F.data == P2P_EDIT_CALLBACK_DATA)
async def on_p2p_edit(query: CallbackQuery, state: FSMContext) -> None:
    """ТЗ не детализирует, куда именно ведёт "Изменить" для P2P (в
    отличие от order.py, где это конкретно "назад к корзине") — заказ
    состоит из 4 независимых кусков (описание/А/Б/контакт), у которых
    нет единого "предыдущего экрана". Решение: начать заново с описания
    — самый простой и предсказуемый вариант, не требует запоминать,
    какой из шагов пользователь хотел поправить."""
    await state.update_data(**{P2P_DRAFT_KEY: {}})
    await state.set_state(P2PStates.waiting_for_description)
    await query.message.answer(P2P_DESCRIPTION_PROMPT, reply_markup=_cancel_keyboard())
    await query.answer()


# ---------------------------------------------------------------------- #
# Шаг 7: отправка на проверку (7.6.6, "новый заказ создаётся со статусом
# ...pending_review")
# ---------------------------------------------------------------------- #


@router.callback_query(StateFilter(P2PStates.confirming_order), F.data == P2P_SUBMIT_CALLBACK_DATA)
async def on_p2p_submit(
    query: CallbackQuery,
    state: FSMContext,
    sheets: SheetsClient,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
) -> None:
    # Тот же живой баг и то же решение, что в handlers/order.py::on_order_confirmed
    # (см. подробный комментарий там) — защита от двойного тапа "Отправить
    # на проверку".
    user_id = query.from_user.id if query.from_user else ""
    lock_key = P2P_SUBMIT_LOCK_KEY_TEMPLATE.format(user_id=user_id)
    lock_acquired = await redis.set(lock_key, "1", nx=True, ex=P2P_SUBMIT_LOCK_TTL_SECONDS)
    if not lock_acquired:
        await query.answer()
        return

    try:
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

        data = await state.get_data()
        draft = data.get(P2P_DRAFT_KEY) or {}

        order_id = await _generate_order_id(redis)
        order_fields = {
            "order_id": order_id,
            "timestamp_created": now_local_str(settings),
            "order_kind": "p2p",
            "user_id": str(user_id),
            "username": draft.get("contact_username") or "",
            "phone": draft.get("contact_phone") or "",
            "merchant_id": "",
            "merchant_name": "",
            "items_json": "",
            "total_try": "",
            "delivery_address": "",
            "geo_lat": "",
            "geo_lon": "",
            "address_manually_edited": "False",
            "is_custom_order": "FALSE",
            "custom_description": "",
            "custom_photo_file_id": "",
            "p2p_description": draft.get("description", ""),
            "p2p_photo_file_id": draft.get("photo_file_id", ""),
            "pickup_address": draft.get("pickup_address", ""),
            "pickup_lat": str(draft.get("pickup_lat", "")),
            "pickup_lon": str(draft.get("pickup_lon", "")),
            "dropoff_address": draft.get("dropoff_address", ""),
            "dropoff_lat": str(draft.get("dropoff_lat", "")),
            "dropoff_lon": str(draft.get("dropoff_lon", "")),
            "status": "pending_review",
            "admin_notes": draft.get("description", ""),
        }

        try:
            await sheets.append_order(order_fields)
        except SheetsWriteError as exc:
            logger.error("Не удалось записать P2P-заказ %s в Sheets: %s", order_id, exc)
            await query.message.answer(ORDER_SAVE_FAILED_MESSAGE)
            await query.answer()
            return

        logger.info("p2p_submitted_for_review: order_id=%s user_id=%s", order_id, user_id)
        await sheets.append_event(
            event_type="p2p_submitted_for_review",
            actor_role="client",
            actor_id=str(user_id),
            order_id=order_id,
        )

        distance_km = haversine_distance_km(
            float(draft["pickup_lat"]), float(draft["pickup_lon"]),
            float(draft["dropoff_lat"]), float(draft["dropoff_lon"]),
        )
        contact = _contact_label(draft.get("contact_username"), user_id)
        admin_text = ADMIN_P2P_REVIEW_TEMPLATE.format(
            order_id=order_id,
            description=draft.get("description", ""),
            pickup_address=draft.get("pickup_address", ""),
            dropoff_address=draft.get("dropoff_address", ""),
            distance_km=distance_km,
            contact=contact,
        )
        photo_file_id = draft.get("photo_file_id", "")
        for admin_id in settings.admin_ids:
            try:
                if photo_file_id:
                    await bot.send_photo(admin_id, photo_file_id, caption=admin_text)
                else:
                    await bot.send_message(admin_id, admin_text)
            except TelegramBadRequest as exc:
                logger.warning("Не удалось отправить P2P-заказ %s на проверку админу %s: %s", order_id, admin_id, exc)

        schedule_p2p_review_reminder(scheduler, order_id, settings.P2P_REVIEW_TIMEOUT_MIN)

        await _reset_p2p(state)

        summary_text = query.message.text or ""
        final_text = f"{summary_text}\n\n{P2P_SUBMITTED_ACK}" if summary_text else P2P_SUBMITTED_ACK
        try:
            await query.message.edit_text(final_text, reply_markup=None)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
        await query.answer()
    finally:
        await redis.delete(lock_key)


# ---------------------------------------------------------------------- #
# Команды Админа: /approve_p2p_[order_id], /reject_p2p_[order_id]
# ---------------------------------------------------------------------- #


def _parse_order_id(command_prefix: str, text: str) -> tuple[str, str] | None:
    if not text.startswith(command_prefix):
        return None
    rest = text[len(command_prefix):]
    parts = rest.split(maxsplit=1)
    if not parts:
        return None
    order_id = parts[0]
    reason = parts[1].strip() if len(parts) > 1 else ""
    return order_id, reason


@router.message(F.text.startswith("/approve_p2p_"))
async def cmd_approve_p2p(
    message: Message,
    settings: Settings,
    sheets: SheetsClient,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    parsed = _parse_order_id("/approve_p2p_", message.text or "")
    if parsed is None:
        return
    order_id, _reason = parsed

    lock_key = P2P_ADMIN_ACTION_LOCK_KEY_TEMPLATE.format(order_id=order_id)
    lock_acquired = await redis.set(lock_key, "1", nx=True, ex=P2P_ADMIN_ACTION_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.info("p2p_approve_blocked_by_lock: order_id=%s admin_id=%s", order_id, message.from_user.id)
        await message.answer(ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE)
        return

    try:
        order = await sheets.get_order(order_id)
        if order is None or order.get("status") != "pending_review":
            logger.info(
                "p2p_approve_not_found: order_id=%s admin_id=%s order_status=%r",
                order_id, message.from_user.id, order.get("status") if order else None,
            )
            await message.answer(ADMIN_P2P_NOT_FOUND_MESSAGE)
            return

        cancel_p2p_review_reminder(scheduler, order_id)

        client_user_id = order.get("user_id")
        if client_user_id:
            try:
                await bot.send_message(int(client_user_id), P2P_APPROVED_CLIENT_MESSAGE)
            except TelegramBadRequest as exc:
                logger.warning("Не удалось уведомить клиента об одобрении P2P-заказа %s: %s", order_id, exc)

        logger.info("p2p_admin_approved: order_id=%s admin_id=%s", order_id, message.from_user.id)
        await sheets.append_event(
            event_type="p2p_admin_approved",
            actor_role="admin",
            actor_id=str(message.from_user.id),
            order_id=order_id,
        )

        await send_offer_to_couriers(
            bot,
            sheets,
            redis,
            settings,
            scheduler,
            order_id=order_id,
            merchant_name="",
            merchant_description="",
            delivery_address="",
            total_try="",
            p2p_pickup_address=order.get("pickup_address", ""),
            p2p_dropoff_address=order.get("dropoff_address", ""),
            p2p_description=order.get("p2p_description", ""),
        )

        await message.answer(ADMIN_P2P_APPROVED_ACK)
    finally:
        await redis.delete(lock_key)


async def _reject_p2p_order(
    message: Message,
    sheets: SheetsClient,
    scheduler: AsyncIOScheduler,
    bot: Bot,
    order_id: str,
    reason: str,
) -> None:
    """Общая логика отклонения P2P-заказа — используется и из
    cmd_reject_p2p (причина сразу в команде), и из
    on_p2p_reject_reason_received (причина отдельным сообщением после
    голой команды). Зеркало _reject_custom_order в
    handlers/custom_order.py."""
    cancel_p2p_review_reminder(scheduler, order_id)
    await sheets.update_order_fields(order_id, {"status": "rejected", "cancel_reason": reason})

    order = await sheets.get_order(order_id)
    client_user_id = order.get("user_id") if order else None
    reason_suffix = P2P_REJECTED_REASON_SUFFIX_TEMPLATE.format(reason=reason) if reason else ""
    if client_user_id:
        try:
            await bot.send_message(
                int(client_user_id), P2P_REJECTED_CLIENT_TEMPLATE.format(reason_suffix=reason_suffix)
            )
        except TelegramBadRequest as exc:
            logger.warning("Не удалось уведомить клиента об отклонении P2P-заказа %s: %s", order_id, exc)

    logger.info("p2p_admin_rejected: order_id=%s reason=%r", order_id, reason)
    await sheets.append_event(
        event_type="p2p_admin_rejected",
        actor_role="admin",
        actor_id=str(message.from_user.id) if message.from_user else "",
        order_id=order_id,
        details=reason,
    )
    await message.answer(ADMIN_P2P_REJECTED_ACK)


@router.message(F.text.startswith("/reject_p2p_"))
async def cmd_reject_p2p(
    message: Message,
    settings: Settings,
    sheets: SheetsClient,
    scheduler: AsyncIOScheduler,
    bot: Bot,
    state: FSMContext,
    redis: Redis,
) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    parsed = _parse_order_id("/reject_p2p_", message.text or "")
    if parsed is None:
        return
    order_id, reason = parsed

    lock_key = P2P_ADMIN_ACTION_LOCK_KEY_TEMPLATE.format(order_id=order_id)
    lock_acquired = await redis.set(lock_key, "1", nx=True, ex=P2P_ADMIN_ACTION_LOCK_TTL_SECONDS)
    if not lock_acquired:
        logger.info("p2p_reject_blocked_by_lock: order_id=%s admin_id=%s", order_id, message.from_user.id)
        await message.answer(ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE)
        return

    try:
        order = await sheets.get_order(order_id)
        if order is None or order.get("status") != "pending_review":
            logger.info(
                "p2p_reject_not_found: order_id=%s admin_id=%s order_status=%r",
                order_id, message.from_user.id, order.get("status") if order else None,
            )
            await message.answer(ADMIN_P2P_NOT_FOUND_MESSAGE)
            return

        if not reason:
            # Живой фидбэк из тестирования: та же находка, что и у
            # /reject_custom_ в handlers/custom_order.py — голая команда
            # без причины запрашивает текст и ждёт его следующим сообщением,
            # вместо того чтобы сразу уходить клиенту шаблоном без объяснения.
            # Лок НЕ держим через весь период ожидания — снимаем сразу же в
            # finally ниже, как только показали приглашение; повторная его
            # выдача (короткая) происходит в on_p2p_reject_reason_received
            # непосредственно перед самой записью.
            await state.update_data(p2p_reject_target_order_id=order_id)
            await state.set_state(P2PStates.waiting_for_reject_reason)
            await message.answer(ADMIN_REJECT_P2P_REASON_PROMPT)
            return

        await state.set_state(None)
        await _reject_p2p_order(message, sheets, scheduler, bot, order_id, reason)
    finally:
        await redis.delete(lock_key)


@router.message(StateFilter(P2PStates.waiting_for_reject_reason), F.text)
async def on_p2p_reject_reason_received(
    message: Message,
    settings: Settings,
    sheets: SheetsClient,
    scheduler: AsyncIOScheduler,
    bot: Bot,
    state: FSMContext,
    redis: Redis,
) -> None:
    """Продолжение cmd_reject_p2p — см. docstring
    P2PStates.waiting_for_reject_reason. Регистрация ПОСЛЕ cmd_approve_p2p/
    cmd_reject_p2p обязательна (та же причина, что в handlers/support.py
    и handlers/custom_order.py): новая команда /approve_p2p_/reject_p2p_
    во время ожидания причины должна попасть в соответствующий cmd_*,
    а не быть проглочена этим хендлером.

    Здесь берём СВОЙ, отдельный (короткий) лок непосредственно перед
    самой записью — тот же order_id, что и в cmd_approve_p2p/
    cmd_reject_p2p, поэтому конкурентный /approve_p2p_[того же id]
    (пока Админ печатает причину) тоже будет с ним считаться."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    data = await state.get_data()
    order_id = data.get("p2p_reject_target_order_id")
    if order_id is None:
        await state.set_state(None)
        return

    reason = message.text.strip()
    if not reason:
        await message.answer(ADMIN_REJECT_P2P_REASON_PROMPT)
        return

    lock_key = P2P_ADMIN_ACTION_LOCK_KEY_TEMPLATE.format(order_id=order_id)
    lock_acquired = await redis.set(lock_key, "1", nx=True, ex=P2P_ADMIN_ACTION_LOCK_TTL_SECONDS)
    if not lock_acquired:
        await message.answer(ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE)
        return

    try:
        order = await sheets.get_order(order_id)
        if order is None or order.get("status") != "pending_review":
            # Заказ мог измениться, пока Админ печатал причину — например,
            # кто-то другой успел одобрить его за это время.
            logger.info(
                "p2p_reject_followup_not_found: order_id=%s admin_id=%s order_status=%r",
                order_id, message.from_user.id, order.get("status") if order else None,
            )
            await message.answer(ADMIN_P2P_NOT_FOUND_MESSAGE)
            return

        await state.set_state(None)
        await state.update_data(p2p_reject_target_order_id=None)
        await _reject_p2p_order(message, sheets, scheduler, bot, order_id, reason)
    finally:
        await redis.delete(lock_key)
