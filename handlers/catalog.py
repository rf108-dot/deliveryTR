"""
Категории → мерчанты → меню.

Day 1 (ТЗ §7.1-7.3.1): inline-клавиатура категорий (только видимые
мерчанты), список мерчантов (авто-выбор при единственном), карточки
позиций меню с фото и пагинацией.

Day 2 (ТЗ §7.3.2): карточка позиции стала cart-aware — показывает
[➕ Добавить] / [− N +] / [🚫 Недоступно] в зависимости от состояния
корзины, плюс кнопку [🛒 Корзина (N) · X ₺], если корзина не пуста.
Смена мерчанта с непустой корзиной другого мерчанта → предупреждение
с подтверждением очистки.

ВАЖНО про структуру модулей: все callback-классы (ItemActionCallback,
CartQtyCallback, CartViewCallback) и функция построения клавиатуры
позиции (build_item_action_keyboard) определены ЗДЕСЬ, а не в
handlers/cart.py — это сделано специально, чтобы избежать циклического
импорта между catalog.py и cart.py. cart.py импортирует нужное отсюда
(однонаправленная зависимость cart → catalog), а catalog.py читает
состояние корзины напрямую из FSMContext.data по ключу CART_STATE_KEY,
не импортируя ничего из cart.py. Ключ CART_STATE_KEY продублирован (как
строковый литерал "cart") в обоих файлах — это осознанный компромисс:
делить один настолько маленький литерал через отдельный модуль было бы
избыточно.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from services.sheets import (
    Merchant,
    MenuItem,
    SheetsClient,
    get_available_categories,
    items_for_merchant,
    merchants_in_category,
)
from texts.ru import (
    CART_BUTTON_TEMPLATE,
    CART_SWITCH_CANCEL_BUTTON,
    CART_SWITCH_CONFIRM_BUTTON,
    CART_SWITCH_WARNING_TEMPLATE,
    CATALOG_CHOOSE_CATEGORY,
    CATALOG_CHOOSE_MERCHANT,
    CATALOG_EMPTY_MENU_TEMPLATE,
    CATALOG_MERCHANT_UNAVAILABLE,
    CATALOG_NO_CATEGORIES,
    CATALOG_NO_MERCHANTS_IN_CATEGORY,
    CATEGORY_LABELS,
    CUSTOM_ORDER_BUTTON,
    ITEM_ADD_BUTTON,
    ITEM_NAV_BACK,
    ITEM_NAV_FORWARD,
    ITEM_QTY_COUNT_TEMPLATE,
    ITEM_QTY_DEC_BUTTON,
    ITEM_QTY_INC_BUTTON,
    ITEM_UNAVAILABLE_ALERT,
    ITEM_UNAVAILABLE_BUTTON,
    P2P_BUTTON_TEXT,
    SUPPORT_BUTTON,
)
from utils.formatters import format_item_caption, format_price_try

logger = logging.getLogger(__name__)

router = Router(name=__name__)

# Ключ в FSMContext.data. ВАЖНО: должен совпадать с CART_STATE_KEY в
# handlers/cart.py (см. пояснение в шапке модуля).
CART_STATE_KEY = "cart"


# ---------------------------------------------------------------------- #
# Callback data
# ---------------------------------------------------------------------- #


class CategoryCallback(CallbackData, prefix="cat"):
    code: str


class MerchantCallback(CallbackData, prefix="merch"):
    merchant_id: str


class ItemNavCallback(CallbackData, prefix="item_nav"):
    merchant_id: str
    index: int


class ItemActionCallback(CallbackData, prefix="item_act"):
    merchant_id: str
    item_id: str
    action: str  # "add" | "unavailable" — "add" обрабатывается в cart.py


class CartQtyCallback(CallbackData, prefix="cart_qty"):
    merchant_id: str
    item_id: str
    action: str  # "inc" | "dec" — обрабатывается в cart.py


class CartViewCallback(CallbackData, prefix="cart_view"):
    """Без полей: текущая корзина (с её merchant_id) уже есть в FSM-состоянии."""


class ConfirmSwitchMerchantCallback(CallbackData, prefix="switch_merch"):
    merchant_id: str  # мерчант, в который переходим при подтверждении очистки


class CustomOrderEntryCallback(CallbackData, prefix="custom_order_entry"):
    """Кнопка [📝 Нет нужного? Написать запрос] на карточке позиции меню
    (ТЗ §7.3.4). Определён здесь, а не в handlers/custom_order.py — по
    той же причине, что и остальные callback-классы этого модуля (см.
    docstring файла про однонаправленную зависимость custom_order.py →
    catalog.py, зеркально cart.py → catalog.py)."""

    merchant_id: str


P2P_CALLBACK_DATA = "p2p:start"
NOOP_CALLBACK_DATA = "noop"
CANCEL_SWITCH_CALLBACK_DATA = "cancel_switch"
# ТЗ §7.5: "Кнопка [❓ Вопрос по заказу] доступна на всех активных
# экранах" — полный охват всех экранов потребовал бы правок во всех
# handlers-модулях сразу (нет единого механизма "постоянной" клавиатуры
# в проекте, см. docstring handlers/support.py); прагматичный компромисс
# для MVP — команда /support работает отовсюду (см. support.py) плюс
# кнопка здесь, на самом часто посещаемом "домашнем" экране (сюда ведут
# start.py, fallback.py, cart.py::on_back_to_categories,
# p2p.py::on_p2p_back_to_start).
SUPPORT_CALLBACK_DATA = "support:start"


# ---------------------------------------------------------------------- #
# Точка входа (вызывается из handlers/start.py)
# ---------------------------------------------------------------------- #


async def show_categories(message: Message, sheets: SheetsClient) -> None:
    """Показывает клавиатуру категорий. Вызывается сразу после /start."""
    merchants = await sheets.get_merchants()
    codes = get_available_categories(merchants)
    if not codes:
        await message.answer(CATALOG_NO_CATEGORIES)
        return
    await message.answer(CATALOG_CHOOSE_CATEGORY, reply_markup=_build_categories_keyboard(codes))


# ---------------------------------------------------------------------- #
# Публичные помощники для handlers/cart.py
# ---------------------------------------------------------------------- #


def build_item_action_keyboard(
    merchant_id: str,
    item: MenuItem,
    index: int,
    total: int,
    *,
    item_qty: int,
    cart_total_count: int,
    cart_total_price: float,
) -> InlineKeyboardMarkup:
    """
    Публичная функция — используется и здесь (при первом показе позиции),
    и в handlers/cart.py (при перерисовке клавиатуры после +/-, через
    edit_message_reply_markup, см. ТЗ §7.3.2 "не новое сообщение").
    """
    nav_row: list[InlineKeyboardButton] = []
    if index > 0:
        nav_row.append(
            InlineKeyboardButton(
                text=ITEM_NAV_BACK,
                callback_data=ItemNavCallback(merchant_id=merchant_id, index=index - 1).pack(),
            )
        )
    nav_row.append(
        InlineKeyboardButton(text=f"{index + 1} / {total}", callback_data=NOOP_CALLBACK_DATA)
    )
    if index < total - 1:
        nav_row.append(
            InlineKeyboardButton(
                text=ITEM_NAV_FORWARD,
                callback_data=ItemNavCallback(merchant_id=merchant_id, index=index + 1).pack(),
            )
        )

    if not item.is_available:
        extra_rows = [
            [
                InlineKeyboardButton(
                    text=ITEM_UNAVAILABLE_BUTTON,
                    callback_data=ItemActionCallback(
                        merchant_id=merchant_id, item_id=item.item_id, action="unavailable"
                    ).pack(),
                )
            ]
        ]
    elif item_qty <= 0:
        extra_rows = [
            [
                InlineKeyboardButton(
                    text=ITEM_ADD_BUTTON,
                    callback_data=ItemActionCallback(
                        merchant_id=merchant_id, item_id=item.item_id, action="add"
                    ).pack(),
                )
            ]
        ]
    else:
        # Количество — отдельной полноширинной строкой (не влезает в треть
        # ряда без обрезания), −/+ — по два в ряд, а не по три: так у
        # каждой кнопки вдвое больше места и слова не обрезаются Telegram
        # (баг найден на живом тестировании — см. CHECKLIST.md).
        extra_rows = [
            [
                InlineKeyboardButton(
                    text=ITEM_QTY_COUNT_TEMPLATE.format(qty=item_qty),
                    callback_data=NOOP_CALLBACK_DATA,
                )
            ],
            [
                InlineKeyboardButton(
                    text=ITEM_QTY_DEC_BUTTON,
                    callback_data=CartQtyCallback(
                        merchant_id=merchant_id, item_id=item.item_id, action="dec"
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=ITEM_QTY_INC_BUTTON,
                    callback_data=CartQtyCallback(
                        merchant_id=merchant_id, item_id=item.item_id, action="inc"
                    ).pack(),
                ),
            ],
        ]

    rows = [nav_row, *extra_rows]
    if cart_total_count > 0:
        cart_button_text = CART_BUTTON_TEMPLATE.format(
            count=cart_total_count, price=format_price_try(cart_total_price)
        )
        rows.append(
            [InlineKeyboardButton(text=cart_button_text, callback_data=CartViewCallback().pack())]
        )

    # ТЗ §7.3.4: доступна всегда при просмотре меню мерчанта, независимо
    # от наличия/состояния корзины — пользователь может не найти нужную
    # позицию вообще (а не просто увидеть её "недоступной").
    rows.append(
        [
            InlineKeyboardButton(
                text=CUSTOM_ORDER_BUTTON,
                callback_data=CustomOrderEntryCallback(merchant_id=merchant_id).pack(),
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_merchant_menu(
    message: Message, merchant_id: str, sheets: SheetsClient, state: FSMContext
) -> bool:
    """
    Показывает первую позицию меню мерчанта новым сообщением. Публичная
    точка входа для handlers/cart.py (кнопка «✏️ Изменить» на экране
    корзины). Возвращает False, если мерчант не найден/недоступен.
    """
    merchants = await sheets.get_merchants()
    merchant = next(
        (m for m in merchants if m.merchant_id == merchant_id and m.is_visible), None
    )
    if merchant is None:
        return False
    await _enter_merchant(message, merchant, sheets, state)
    return True


# ---------------------------------------------------------------------- #
# Хендлеры
# ---------------------------------------------------------------------- #


@router.callback_query(CategoryCallback.filter())
async def on_category_selected(
    query: CallbackQuery, callback_data: CategoryCallback, sheets: SheetsClient, state: FSMContext
) -> None:
    user_id = query.from_user.id if query.from_user else None
    logger.info("category_selected: user_id=%s category=%s", user_id, callback_data.code)
    await sheets.append_event(
        event_type="category_selected",
        actor_role="client",
        actor_id=str(user_id or ""),
        details=callback_data.code,
    )

    merchants = merchants_in_category(await sheets.get_merchants(), callback_data.code)
    if not merchants:
        await query.answer(CATALOG_NO_MERCHANTS_IN_CATEGORY, show_alert=True)
        return

    if len(merchants) == 1:
        await _enter_merchant_or_confirm_switch(query.message, merchants[0], sheets, state)
    else:
        await query.message.edit_text(
            CATALOG_CHOOSE_MERCHANT, reply_markup=_build_merchants_keyboard(merchants)
        )
    await query.answer()


@router.callback_query(MerchantCallback.filter())
async def on_merchant_selected(
    query: CallbackQuery, callback_data: MerchantCallback, sheets: SheetsClient, state: FSMContext
) -> None:
    user_id = query.from_user.id if query.from_user else None
    logger.info(
        "merchant_selected: user_id=%s merchant_id=%s", user_id, callback_data.merchant_id
    )
    await sheets.append_event(
        event_type="merchant_selected",
        actor_role="client",
        actor_id=str(user_id or ""),
        details=callback_data.merchant_id,
    )

    merchants = await sheets.get_merchants()
    merchant = next(
        (m for m in merchants if m.merchant_id == callback_data.merchant_id and m.is_visible),
        None,
    )
    if merchant is None:
        await query.answer(CATALOG_MERCHANT_UNAVAILABLE, show_alert=True)
        return

    await _enter_merchant_or_confirm_switch(query.message, merchant, sheets, state)
    await query.answer()


# on_confirm_switch_merchant и on_cancel_switch_merchant — см. handlers/cart.py.
# Оба результата (очистить корзину / вернуться к её просмотру) — операции
# над корзиной, поэтому логично живут там, а не здесь. Сам колбэк-класс
# ConfirmSwitchMerchantCallback и константа CANCEL_SWITCH_CALLBACK_DATA
# остаются в этом модуле, т.к. клавиатуру предупреждения строит catalog.py
# (_build_switch_confirm_keyboard, ниже).


@router.callback_query(ItemNavCallback.filter())
async def on_item_nav(
    query: CallbackQuery, callback_data: ItemNavCallback, sheets: SheetsClient, state: FSMContext
) -> None:
    merchants = await sheets.get_merchants()
    merchant = next(
        (m for m in merchants if m.merchant_id == callback_data.merchant_id), None
    )
    if merchant is None:
        await query.answer(CATALOG_MERCHANT_UNAVAILABLE, show_alert=True)
        return

    items = items_for_merchant(await sheets.get_items(), merchant.merchant_id)
    if not (0 <= callback_data.index < len(items)):
        await query.answer()
        return

    await _display_item(
        query.message, merchant, items, callback_data.index, as_new_message=False, state=state
    )
    await query.answer()


@router.callback_query(ItemActionCallback.filter(F.action == "unavailable"))
async def on_item_unavailable(query: CallbackQuery, callback_data: ItemActionCallback) -> None:
    await query.answer(ITEM_UNAVAILABLE_ALERT, show_alert=True)


@router.callback_query(F.data == NOOP_CALLBACK_DATA)
async def on_noop(query: CallbackQuery) -> None:
    await query.answer()


# ---------------------------------------------------------------------- #
# Внутренние помощники
# ---------------------------------------------------------------------- #


def _get_cart_items_for_merchant(state_data: dict, merchant_id: str) -> dict[str, int]:
    """Позиции корзины, если корзина сейчас принадлежит этому мерчанту, иначе {}."""
    cart = state_data.get(CART_STATE_KEY)
    if not cart or cart.get("merchant_id") != merchant_id:
        return {}
    return dict(cart.get("items", {}))


def _cart_summary_for_merchant(
    state_data: dict, merchant_id: str, items: list[MenuItem]
) -> tuple[int, float]:
    """(количество товаров, сумма) корзины для мерчанта — для кнопки [🛒 Корзина]."""
    cart_items = _get_cart_items_for_merchant(state_data, merchant_id)
    if not cart_items:
        return 0, 0.0
    price_by_id = {i.item_id: i.price_try for i in items}
    total_count = sum(cart_items.values())
    total_price = sum(price_by_id.get(iid, 0.0) * qty for iid, qty in cart_items.items())
    return total_count, total_price


async def _enter_merchant_or_confirm_switch(
    message: Message, merchant: Merchant, sheets: SheetsClient, state: FSMContext
) -> None:
    """
    Если в корзине уже есть товары ДРУГОГО мерчанта — показывает
    предупреждение с подтверждением очистки (ТЗ §7.3.2), иначе сразу
    показывает меню мерчанта.
    """
    data = await state.get_data()
    cart = data.get(CART_STATE_KEY)
    if cart and cart.get("items") and cart.get("merchant_id") != merchant.merchant_id:
        count = sum(cart["items"].values())
        await message.edit_text(
            CART_SWITCH_WARNING_TEMPLATE.format(count=count),
            reply_markup=_build_switch_confirm_keyboard(merchant.merchant_id),
        )
        return
    await _enter_merchant(message, merchant, sheets, state)


async def _enter_merchant(
    message: Message, merchant: Merchant, sheets: SheetsClient, state: FSMContext
) -> None:
    items = items_for_merchant(await sheets.get_items(), merchant.merchant_id)
    if not items:
        try:
            await message.edit_text(CATALOG_EMPTY_MENU_TEMPLATE.format(name=merchant.name))
        except TelegramBadRequest:
            # Живой фидбэк: двойной клик по мерчанту — два апдейта почти
            # одновременно правят то же сообщение на тот же самый текст;
            # Telegram отвечает "message is not modified" на второй —
            # безобидно (результат уже верный), но раньше давало пугающий
            # необработанный traceback в логах.
            pass
        return
    await _display_item(message, merchant, items, index=0, as_new_message=True, state=state)


async def _display_item(
    message: Message,
    merchant: Merchant,
    items: list[MenuItem],
    index: int,
    *,
    as_new_message: bool,
    state: FSMContext,
) -> None:
    item = items[index]
    data = await state.get_data()
    item_qty = _get_cart_items_for_merchant(data, merchant.merchant_id).get(item.item_id, 0)
    cart_count, cart_price = _cart_summary_for_merchant(data, merchant.merchant_id, items)

    caption = format_item_caption(item, index, len(items))
    keyboard = build_item_action_keyboard(
        merchant.merchant_id,
        item,
        index,
        len(items),
        item_qty=item_qty,
        cart_total_count=cart_count,
        cart_total_price=cart_price,
    )

    if as_new_message:
        # Переход из текстового сообщения (список категорий/мерчантов) —
        # Telegram не позволяет превратить текстовое сообщение в медиа,
        # поэтому карточка позиции всегда отправляется новым сообщением.
        await _send_item_message(message, item, caption, keyboard)
        return

    try:
        if item.photo_url:
            await message.edit_media(
                media=InputMediaPhoto(media=item.photo_url, caption=caption, parse_mode="HTML"),
                reply_markup=keyboard,
            )
        else:
            await message.edit_text(caption, reply_markup=keyboard)
    except TelegramBadRequest:
        # Смена типа контента между соседними позициями (фото ↔ без фото) —
        # Telegram не даёт отредактировать медиа-тип на лету, шлём заново.
        # (Известное упрощение Day 1, см. README.)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await _send_item_message(message, item, caption, keyboard)


async def _send_item_message(
    message: Message, item: MenuItem, caption: str, keyboard: InlineKeyboardMarkup
) -> None:
    if item.photo_url:
        try:
            await message.answer_photo(
                photo=item.photo_url, caption=caption, reply_markup=keyboard
            )
            return
        except TelegramBadRequest as exc:
            # Битая/непрямая ссылка на фото (например, ссылка на HTML-страницу,
            # а не на файл изображения) не должна ронять всю карточку —
            # показываем её текстом и логируем, какая именно позиция виновата.
            logger.warning(
                "Не удалось загрузить фото для item_id=%s (photo_url=%r): %s. "
                "Показываю карточку без фото.",
                item.item_id,
                item.photo_url,
                exc,
            )
    await message.answer(caption, reply_markup=keyboard)


def _build_categories_keyboard(codes: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(codes), 2):
        row_codes = codes[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    text=CATEGORY_LABELS.get(code, code),
                    callback_data=CategoryCallback(code=code).pack(),
                )
                for code in row_codes
            ]
        )
    rows.append([InlineKeyboardButton(text=P2P_BUTTON_TEXT, callback_data=P2P_CALLBACK_DATA)])
    rows.append([InlineKeyboardButton(text=SUPPORT_BUTTON, callback_data=SUPPORT_CALLBACK_DATA)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_merchants_keyboard(merchants: list[Merchant]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=m.name,
                callback_data=MerchantCallback(merchant_id=m.merchant_id).pack(),
            )
        ]
        for m in merchants
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_switch_confirm_keyboard(new_merchant_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CART_SWITCH_CONFIRM_BUTTON,
                    callback_data=ConfirmSwitchMerchantCallback(
                        merchant_id=new_merchant_id
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=CART_SWITCH_CANCEL_BUTTON, callback_data=CANCEL_SWITCH_CALLBACK_DATA
                )
            ],
        ]
    )
