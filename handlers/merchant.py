"""
Модуль ресторана (самообслуживание мерчанта) — ТЗ v2.3, §8A.

Цель (§8A, вводный абзац): дать ресторану возможность самому вести своё
меню, если он готов этим заниматься. Режим необязательный и полностью
сосуществует с текущим способом (Админ через Google Sheets, ТЗ §10.1,
§10.3): оба работают с одними и теми же листами «Мерчанты» и «Позиции»,
переключение — просто заполнение/очистка поля merchant_telegram_ids
Админом в таблице, без изменений в коде.

§8A.1 Роль и доступ: роль определяется по Telegram ID (ТЗ §2, тот же
принцип, что у курьера в handlers/courier.py). Ресторан видит и меняет
только позиции СВОЕГО merchant_id — see _get_merchant_for_telegram_id
ниже и явную проверку item.merchant_id на каждом действии с позицией.

§8A.2 Подтверждение позиций, §8A.3 «Сегодня нет» / «Есть снова».

РЕШЕНИЕ (живой фидбэк после теста): §8A.4 «Добавление новых позиций
рестораном» сознательно НЕ реализовано в этом файле — функцию
попробовали, она оказалась путающей для ресторана и всплыл смежный баг
в handlers/admin.py (свободный текст названия позиции ломал
HTML-сообщение в /toggle_item_). Решили не чинить, а убрать саму
функцию: ресторан добавляет/удаляет позиции через прежний интерфейс
(обращение к Админу), как было до ТЗ v2.3. services/sheets.py::add_item()
при этом НЕ удалён — он не используется отсюда, но трогать sheets.py
второй раз ради этого не стали.

§8A.5 Границы: ресторан НЕ меняет цену/описание существующих позиций,
is_active мерчанта, категорию — таких хендлеров в этом файле нет.
§8A.6 События (actor_role="merchant").

ВАЖНОЕ ОГРАНИЧЕНИЕ (пока не подключено): роль ресторана должна была бы
маршрутизироваться в handlers/start.py вместе с ролями клиента/курьера/
админа (ТЗ §2), но этот файл сюда не передавался — соответственно, вход
в модуль пока только по команде /my_menu (работает независимо от
start.py). Персистентная reply-клавиатура с кнопкой «📋 Моё меню»
(build_merchant_keyboard) готова и подключена сюда же — её нужно будет
показывать пользователю при /start, аналогично build_shift_keyboard
курьера, когда появится доступ к start.py.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from config import Settings
from services.notifications import notify_admins
from services.sheets import Merchant, MenuItem, SheetsClient, items_for_merchant
from texts.ru import (
    ADMIN_MERCHANT_ITEM_UNAVAILABLE_TEMPLATE,
    ADMIN_MERCHANT_MENU_CONFIRMED_TEMPLATE,
    MERCHANT_ITEM_AVAILABLE_BUTTON,
    MERCHANT_ITEM_LINE_TEMPLATE,
    MERCHANT_ITEM_MARKED_AVAILABLE_ACK_TEMPLATE,
    MERCHANT_ITEM_MARKED_UNAVAILABLE_ACK_TEMPLATE,
    MERCHANT_ITEM_NOT_FOUND_MESSAGE,
    MERCHANT_ITEM_UNAVAILABLE_BUTTON,
    MERCHANT_MENU_ALREADY_CONFIRMED_LINE,
    MERCHANT_MENU_BUTTON,
    MERCHANT_MENU_CONFIRM_BUTTON,
    MERCHANT_MENU_CONFIRMED_ACK,
    MERCHANT_MENU_EMPTY_MESSAGE,
    MERCHANT_MENU_HEADER_TEMPLATE,
    MERCHANT_MENU_OPENED_ACK,
    MERCHANT_NOT_REGISTERED_MESSAGE,
)
from utils.telegram_safety import resilient
from utils.telegram_safety import safe_answer as _safe_answer

logger = logging.getLogger(__name__)

router = Router(name=__name__)

MERCHANT_TOGGLE_UNAVAILABLE_CALLBACK_PREFIX = "merchant_item_off:"
MERCHANT_TOGGLE_AVAILABLE_CALLBACK_PREFIX = "merchant_item_on:"
MERCHANT_CONFIRM_MENU_CALLBACK_DATA = "merchant_confirm_menu"

_resilient = lambda handler: resilient(MERCHANT_NOT_REGISTERED_MESSAGE)(handler)  # noqa: E731


def _esc(value: object) -> str:
    """Экранирование для текста, который идёт в сообщение с
    parse_mode=HTML (DefaultBotProperties в bot.py — HTML для всего
    бота). Название позиции и название мерчанта — свободный текст,
    введённый человеком (Админом в Sheets); без экранирования символ
    вроде "<" в названии ломает отправку сообщения (Telegram вернёт
    "can't parse entities") или искажает разметку. НЕ применяется к
    тексту инлайн-кнопок (они не парсятся как HTML) и к тексту
    всплывающих алертов query.answer() (Telegram их тоже не парсит как
    HTML) — там экранирование не нужно и не используется."""
    return html.escape(str(value), quote=False)


# ---------------------------------------------------------------------- #
# Помощники
# ---------------------------------------------------------------------- #


async def _get_merchant_for_telegram_id(sheets: SheetsClient, telegram_id: int) -> Merchant | None:
    """Мерчант, у которого этот Telegram ID перечислен в
    merchant_telegram_ids, и который при этом активен (is_active) — по
    аналогии с _get_courier в handlers/courier.py: неактивный мерчант не
    должен иметь доступ к самообслуживанию, даже если его ID всё ещё
    остался в таблице.

    force_refresh=True — та же причина, что у _get_courier: Админ мог
    только что вписать ID в merchant_telegram_ids и сразу попросить
    ресторан попробовать; без этого 5-минутный кеш мог бы вернуть
    "не найдено" ещё какое-то время. Не hot path — лишние запросы не
    страшны.

    ПРЕДПОЛОЖЕНИЕ: один Telegram ID числится рестораном только у ОДНОГО
    мерчанта. Если Админ по ошибке впишет один ID сразу в
    merchant_telegram_ids нескольких мерчантов, вернётся первый
    найденный — это не описано в ТЗ и не обрабатывается отдельно."""
    merchants = await sheets.get_merchants(force_refresh=True)
    telegram_id_str = str(telegram_id)
    for merchant in merchants:
        if telegram_id_str in merchant.merchant_telegram_ids and merchant.is_active:
            return merchant
    return None


def build_merchant_keyboard() -> ReplyKeyboardMarkup:
    """Персистентная кнопка входа в модуль (ТЗ §8A, по аналогии с
    build_shift_keyboard курьера) — пока подключена только на /my_menu,
    см. предупреждение в docstring модуля про handlers/start.py."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MERCHANT_MENU_BUTTON)]], resize_keyboard=True
    )


def _menu_keyboard(items: list[MenuItem], *, menu_confirmed: bool) -> InlineKeyboardMarkup:
    """Клавиатура «Моё меню»: по одной строке-переключателю на позицию
    (§8A.3) и кнопка подтверждения меню, если ещё не подтверждено
    сегодня (§8A.2). Кнопки добавления позиции нет — §8A.4 убран."""
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        if item.is_available:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{item.name}: {MERCHANT_ITEM_UNAVAILABLE_BUTTON}",
                        callback_data=f"{MERCHANT_TOGGLE_UNAVAILABLE_CALLBACK_PREFIX}{item.item_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{item.name}: {MERCHANT_ITEM_AVAILABLE_BUTTON}",
                        callback_data=f"{MERCHANT_TOGGLE_AVAILABLE_CALLBACK_PREFIX}{item.item_id}",
                    )
                ]
            )
    if not menu_confirmed:
        rows.append(
            [
                InlineKeyboardButton(
                    text=MERCHANT_MENU_CONFIRM_BUTTON,
                    callback_data=MERCHANT_CONFIRM_MENU_CALLBACK_DATA,
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_menu(sheets: SheetsClient, merchant: Merchant) -> tuple[str, InlineKeyboardMarkup]:
    items = items_for_merchant(await sheets.get_items(), merchant.merchant_id)
    header = MERCHANT_MENU_HEADER_TEMPLATE.format(merchant_name=_esc(merchant.name))
    if merchant.today_confirmed:
        header += "\n" + MERCHANT_MENU_ALREADY_CONFIRMED_LINE
    if not items:
        text = header + "\n\n" + MERCHANT_MENU_EMPTY_MESSAGE
    else:
        lines = [
            MERCHANT_ITEM_LINE_TEMPLATE.format(
                name=_esc(item.name),
                price=item.price_try,
                status="✅" if item.is_available else "⛔️",
            )
            for item in items
        ]
        text = header + "\n\n" + "\n".join(lines)
    return text, _menu_keyboard(items, menu_confirmed=merchant.today_confirmed)


# ---------------------------------------------------------------------- #
# §8A.2/§8A.3: «Моё меню» — просмотр, подтверждение, доступность позиций
# ---------------------------------------------------------------------- #


@router.message(Command("my_menu"))
@router.message(F.text == MERCHANT_MENU_BUTTON)
async def on_my_menu(message: Message, settings: Settings, sheets: SheetsClient) -> None:
    if not message.from_user:
        return
    merchant = await _get_merchant_for_telegram_id(sheets, message.from_user.id)
    if merchant is None:
        await message.answer(MERCHANT_NOT_REGISTERED_MESSAGE)
        return
    # Постоянная клавиатура едет ОТДЕЛЬНЫМ коротким сообщением — то же
    # ограничение Telegram, что и у build_shift_keyboard в courier.py:
    # reply-клавиатура и инлайн-клавиатура не могут быть на одном
    # сообщении одновременно, а меню ниже — инлайн (кнопки по позициям).
    await message.answer(MERCHANT_MENU_OPENED_ACK, reply_markup=build_merchant_keyboard())
    text, keyboard = await _render_menu(sheets, merchant)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == MERCHANT_CONFIRM_MENU_CALLBACK_DATA)
@_resilient
async def on_confirm_menu(query: CallbackQuery, settings: Settings, sheets: SheetsClient, bot: Bot) -> None:
    if not query.from_user:
        await _safe_answer(query)
        return
    merchant = await _get_merchant_for_telegram_id(sheets, query.from_user.id)
    if merchant is None:
        await _safe_answer(query, MERCHANT_NOT_REGISTERED_MESSAGE, show_alert=True)
        return

    await sheets.update_merchant_fields(merchant.merchant_id, {"today_confirmed": "TRUE"})
    logger.info(
        "merchant_menu_confirmed: merchant_id=%s telegram_id=%s",
        merchant.merchant_id,
        query.from_user.id,
    )
    await sheets.append_event(
        event_type="merchant_menu_confirmed",
        actor_role="merchant",
        actor_id=str(query.from_user.id),
        details=f"merchant_id={merchant.merchant_id}",
    )
    await notify_admins(
        bot,
        settings,
        ADMIN_MERCHANT_MENU_CONFIRMED_TEMPLATE.format(merchant_name=_esc(merchant.name)),
    )

    # Перечитываем мерчанта, чтобы клавиатура сразу отразила
    # today_confirmed=TRUE (update_merchant_fields инвалидирует кеш, так
    # что следующий get_merchants() уже вернёт актуальное значение).
    refreshed = await _get_merchant_for_telegram_id(sheets, query.from_user.id)
    if refreshed is not None and query.message:
        text, keyboard = await _render_menu(sheets, refreshed)
        await query.message.edit_text(text, reply_markup=keyboard)
    await _safe_answer(query, MERCHANT_MENU_CONFIRMED_ACK)


async def _toggle_item_availability(
    query: CallbackQuery,
    settings: Settings,
    sheets: SheetsClient,
    bot: Bot,
    *,
    item_id: str,
    make_available: bool,
) -> None:
    if not query.from_user:
        await _safe_answer(query)
        return
    merchant = await _get_merchant_for_telegram_id(sheets, query.from_user.id)
    if merchant is None:
        await _safe_answer(query, MERCHANT_NOT_REGISTERED_MESSAGE, show_alert=True)
        return

    items = await sheets.get_items()
    item = next((i for i in items if i.item_id == item_id), None)
    # Проверка владения (§8A.1): ресторан не должен уметь переключать
    # позицию другого мерчанта, даже подставив чужой item_id в callback.
    if item is None or item.merchant_id != merchant.merchant_id:
        await _safe_answer(query, MERCHANT_ITEM_NOT_FOUND_MESSAGE, show_alert=True)
        return

    await sheets.update_item_fields(item_id, {"is_available": "TRUE" if make_available else "FALSE"})
    event_type = "merchant_item_available" if make_available else "merchant_item_unavailable"
    logger.info(
        "%s: item_id=%s merchant_id=%s telegram_id=%s",
        event_type,
        item_id,
        merchant.merchant_id,
        query.from_user.id,
    )
    await sheets.append_event(
        event_type=event_type,
        actor_role="merchant",
        actor_id=str(query.from_user.id),
        order_id="",
        details=f"item_id={item_id}",
    )
    if not make_available:
        # §8A.3: Админ уведомляется только про «нет сегодня» — про
        # возврат в наличие уведомления в ТЗ нет.
        await notify_admins(
            bot,
            settings,
            ADMIN_MERCHANT_ITEM_UNAVAILABLE_TEMPLATE.format(
                merchant_name=_esc(merchant.name), item_name=_esc(item.name)
            ),
        )

    refreshed_merchant = await _get_merchant_for_telegram_id(sheets, query.from_user.id)
    if refreshed_merchant is not None and query.message:
        text, keyboard = await _render_menu(sheets, refreshed_merchant)
        await query.message.edit_text(text, reply_markup=keyboard)
    ack = (
        MERCHANT_ITEM_MARKED_AVAILABLE_ACK_TEMPLATE
        if make_available
        else MERCHANT_ITEM_MARKED_UNAVAILABLE_ACK_TEMPLATE
    ).format(name=item.name)
    await _safe_answer(query, ack)


@router.callback_query(F.data.startswith(MERCHANT_TOGGLE_UNAVAILABLE_CALLBACK_PREFIX))
@_resilient
async def on_item_unavailable(query: CallbackQuery, settings: Settings, sheets: SheetsClient, bot: Bot) -> None:
    item_id = query.data[len(MERCHANT_TOGGLE_UNAVAILABLE_CALLBACK_PREFIX):]
    await _toggle_item_availability(query, settings, sheets, bot, item_id=item_id, make_available=False)


@router.callback_query(F.data.startswith(MERCHANT_TOGGLE_AVAILABLE_CALLBACK_PREFIX))
@_resilient
async def on_item_available(query: CallbackQuery, settings: Settings, sheets: SheetsClient, bot: Bot) -> None:
    item_id = query.data[len(MERCHANT_TOGGLE_AVAILABLE_CALLBACK_PREFIX):]
    await _toggle_item_availability(query, settings, sheets, bot, item_id=item_id, make_available=True)
