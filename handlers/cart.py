"""
Корзина (вариант Б — полноценная): add/inc/dec, просмотр, лимит
MAX_ITEMS_PER_TYPE (ТЗ §7.3.2-7.3.3).

Хранение: корзина живёт в FSMContext.data (Redis, см. Day 0) под ключом
CART_STATE_KEY = "cart" → {"merchant_id": str, "items": {item_id: qty}}.
Очистка корзины при смене мерчанта реализована в handlers/catalog.py
(там же происходит выбор мерчанта) — см. пояснение в шапке catalog.py
про однонаправленную зависимость cart.py → catalog.py.

Все callback-классы (ItemActionCallback, CartQtyCallback,
CartViewCallback) и построение клавиатуры позиции — в catalog.py,
этот модуль их только использует.

Day 3: кнопка «Оформить заказ» (CART_CHECKOUT_CALLBACK_DATA) теперь
обрабатывается в handlers/order.py — оформление заказа логически не
относится к корзине, только запускается с её экрана. cart.py экспортирует
render_cart_view() как публичную точку входа, чтобы order.py мог
вернуться к просмотру корзины (кнопка «Изменить» на итоговом
подтверждении) без дублирования кода.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from handlers.catalog import (
    CANCEL_SWITCH_CALLBACK_DATA,
    CartQtyCallback,
    CartViewCallback,
    ConfirmSwitchMerchantCallback,
    ItemActionCallback,
    build_item_action_keyboard,
    render_merchant_menu,
    show_categories,
)
from services.sheets import SheetsClient, items_for_merchant
from texts.ru import (
    CATALOG_MERCHANT_UNAVAILABLE,
    CART_BACK_TO_CATEGORIES_BUTTON,
    CART_CHECKOUT_BUTTON,
    CART_EDIT_BUTTON,
    CART_EMPTY_ALERT,
    CART_ITEM_LIMIT_ALERT_TEMPLATE,
)
from utils.formatters import format_cart_summary

logger = logging.getLogger(__name__)

router = Router(name=__name__)

# ВАЖНО: должен совпадать с CART_STATE_KEY в handlers/catalog.py и
# handlers/order.py.
CART_STATE_KEY = "cart"

CART_EDIT_CALLBACK_DATA = "cart_edit"
CART_BACK_TO_CATEGORIES_CALLBACK_DATA = "cart_back_to_categories"
CART_CHECKOUT_CALLBACK_DATA = "cart_checkout"


def _get_cart_items(data: dict, merchant_id: str) -> dict[str, int]:
    cart = data.get(CART_STATE_KEY)
    if not cart or cart.get("merchant_id") != merchant_id:
        return {}
    return dict(cart.get("items", {}))


async def _save_cart_items(state: FSMContext, merchant_id: str, items: dict[str, int]) -> None:
    if items:
        await state.update_data(**{CART_STATE_KEY: {"merchant_id": merchant_id, "items": items}})
    else:
        await state.update_data(**{CART_STATE_KEY: None})


async def _refresh_item_card(
    query: CallbackQuery, merchant_id: str, item_id: str, sheets: SheetsClient, state: FSMContext
) -> None:
    """Перерисовывает клавиатуру ТЕКУЩЕЙ карточки позиции (edit_reply_markup,
    не новое сообщение — см. ТЗ §7.3.2)."""
    items = items_for_merchant(await sheets.get_items(), merchant_id)
    index = next((i for i, it in enumerate(items) if it.item_id == item_id), None)
    if index is None:
        # Позицию успели убрать из меню между показом карточки и нажатием
        # кнопки — молча ничего не делаем, кроме как отвечаем на callback.
        return

    data = await state.get_data()
    cart_items = _get_cart_items(data, merchant_id)
    price_by_id = {i.item_id: i.price_try for i in items}
    cart_total_count = sum(cart_items.values())
    cart_total_price = sum(price_by_id.get(iid, 0.0) * qty for iid, qty in cart_items.items())

    keyboard = build_item_action_keyboard(
        merchant_id,
        items[index],
        index,
        len(items),
        item_qty=cart_items.get(item_id, 0),
        cart_total_count=cart_total_count,
        cart_total_price=cart_total_price,
    )
    await query.message.edit_reply_markup(reply_markup=keyboard)


def _build_cart_view_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=CART_EDIT_BUTTON, callback_data=CART_EDIT_CALLBACK_DATA),
                InlineKeyboardButton(
                    text=CART_BACK_TO_CATEGORIES_BUTTON,
                    callback_data=CART_BACK_TO_CATEGORIES_CALLBACK_DATA,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=CART_CHECKOUT_BUTTON, callback_data=CART_CHECKOUT_CALLBACK_DATA
                )
            ],
        ]
    )


# ---------------------------------------------------------------------- #
# Публичные помощники для handlers/order.py
# ---------------------------------------------------------------------- #


async def render_cart_view(message: Message, sheets: SheetsClient, state: FSMContext) -> bool:
    """
    Показывает сводку корзины новым сообщением. Публичная точка входа —
    используется и здесь (кнопка [🛒 Корзина]), и в handlers/order.py
    (кнопка «Изменить» на итоговом подтверждении заказа). Возвращает
    False, если корзина пуста (вызывающий код сам решает, как на это
    реагировать — обычно alert).
    """
    data = await state.get_data()
    cart = data.get(CART_STATE_KEY)
    if not cart or not cart.get("items"):
        return False

    items = items_for_merchant(await sheets.get_items(), cart["merchant_id"])
    item_by_id = {i.item_id: i for i in items}
    text = format_cart_summary(cart["items"], item_by_id)
    await message.answer(text, reply_markup=_build_cart_view_keyboard())
    return True


# ---------------------------------------------------------------------- #
# Хендлеры
# ---------------------------------------------------------------------- #


@router.callback_query(ItemActionCallback.filter(F.action == "add"))
async def on_add_to_cart(
    query: CallbackQuery,
    callback_data: ItemActionCallback,
    sheets: SheetsClient,
    state: FSMContext,
    settings: Settings,
) -> None:
    data = await state.get_data()
    cart_items = _get_cart_items(data, callback_data.merchant_id)
    current_qty = cart_items.get(callback_data.item_id, 0)

    if current_qty >= settings.MAX_ITEMS_PER_TYPE:
        await query.answer(
            CART_ITEM_LIMIT_ALERT_TEMPLATE.format(max=settings.MAX_ITEMS_PER_TYPE),
            show_alert=True,
        )
        return

    cart_items[callback_data.item_id] = current_qty + 1
    await _save_cart_items(state, callback_data.merchant_id, cart_items)
    logger.info(
        "item_added: merchant_id=%s item_id=%s qty=%d",
        callback_data.merchant_id,
        callback_data.item_id,
        cart_items[callback_data.item_id],
    )
    user_id = query.from_user.id if query.from_user else None
    await sheets.append_event(
        event_type="item_added", actor_role="client", actor_id=str(user_id or ""),
        details=f"{callback_data.item_id} qty={cart_items[callback_data.item_id]}",
    )

    await _refresh_item_card(query, callback_data.merchant_id, callback_data.item_id, sheets, state)
    await query.answer()


@router.callback_query(CartQtyCallback.filter(F.action == "inc"))
async def on_increment(
    query: CallbackQuery,
    callback_data: CartQtyCallback,
    sheets: SheetsClient,
    state: FSMContext,
    settings: Settings,
) -> None:
    data = await state.get_data()
    cart_items = _get_cart_items(data, callback_data.merchant_id)
    current_qty = cart_items.get(callback_data.item_id, 0)

    if current_qty >= settings.MAX_ITEMS_PER_TYPE:
        await query.answer(
            CART_ITEM_LIMIT_ALERT_TEMPLATE.format(max=settings.MAX_ITEMS_PER_TYPE),
            show_alert=True,
        )
        return

    cart_items[callback_data.item_id] = current_qty + 1
    await _save_cart_items(state, callback_data.merchant_id, cart_items)
    logger.info(
        "item_added: merchant_id=%s item_id=%s qty=%d",
        callback_data.merchant_id,
        callback_data.item_id,
        cart_items[callback_data.item_id],
    )
    user_id = query.from_user.id if query.from_user else None
    await sheets.append_event(
        event_type="item_added", actor_role="client", actor_id=str(user_id or ""),
        details=f"{callback_data.item_id} qty={cart_items[callback_data.item_id]}",
    )

    await _refresh_item_card(query, callback_data.merchant_id, callback_data.item_id, sheets, state)
    await query.answer()


@router.callback_query(CartQtyCallback.filter(F.action == "dec"))
async def on_decrement(
    query: CallbackQuery, callback_data: CartQtyCallback, sheets: SheetsClient, state: FSMContext
) -> None:
    data = await state.get_data()
    cart_items = _get_cart_items(data, callback_data.merchant_id)
    current_qty = cart_items.get(callback_data.item_id, 0)

    if current_qty <= 0:
        await query.answer()
        return

    user_id = query.from_user.id if query.from_user else None
    if current_qty <= 1:
        cart_items.pop(callback_data.item_id, None)
        logger.info(
            "item_removed: merchant_id=%s item_id=%s",
            callback_data.merchant_id,
            callback_data.item_id,
        )
        await sheets.append_event(
            event_type="item_removed", actor_role="client", actor_id=str(user_id or ""),
            details=callback_data.item_id,
        )
    else:
        cart_items[callback_data.item_id] = current_qty - 1
        logger.info(
            "item_added: merchant_id=%s item_id=%s qty=%d (уменьшение)",
            callback_data.merchant_id,
            callback_data.item_id,
            cart_items[callback_data.item_id],
        )
        await sheets.append_event(
            event_type="item_added", actor_role="client", actor_id=str(user_id or ""),
            details=f"{callback_data.item_id} qty={cart_items[callback_data.item_id]}",
        )

    await _save_cart_items(state, callback_data.merchant_id, cart_items)
    await _refresh_item_card(query, callback_data.merchant_id, callback_data.item_id, sheets, state)
    await query.answer()


@router.callback_query(CartViewCallback.filter())
async def on_view_cart(query: CallbackQuery, sheets: SheetsClient, state: FSMContext) -> None:
    ok = await render_cart_view(query.message, sheets, state)
    if not ok:
        await query.answer(CART_EMPTY_ALERT, show_alert=True)
        return

    data = await state.get_data()
    merchant_id = (data.get(CART_STATE_KEY) or {}).get("merchant_id")
    logger.info("cart_viewed: merchant_id=%s", merchant_id)
    user_id = query.from_user.id if query.from_user else None
    await sheets.append_event(
        event_type="cart_viewed", actor_role="client", actor_id=str(user_id or ""),
        details=merchant_id or "",
    )
    await query.answer()


@router.callback_query(F.data == CART_EDIT_CALLBACK_DATA)
async def on_cart_edit(query: CallbackQuery, sheets: SheetsClient, state: FSMContext) -> None:
    data = await state.get_data()
    cart = data.get(CART_STATE_KEY)
    if not cart or not cart.get("items"):
        await query.answer(CART_EMPTY_ALERT, show_alert=True)
        return

    ok = await render_merchant_menu(query.message, cart["merchant_id"], sheets, state)
    if not ok:
        await query.answer(CATALOG_MERCHANT_UNAVAILABLE, show_alert=True)
        return
    await query.answer()


@router.callback_query(ConfirmSwitchMerchantCallback.filter())
async def on_confirm_switch_merchant(
    query: CallbackQuery,
    callback_data: ConfirmSwitchMerchantCallback,
    sheets: SheetsClient,
    state: FSMContext,
) -> None:
    """Подтверждена очистка корзины ради перехода к другому мерчанту."""
    await state.update_data(**{CART_STATE_KEY: None})
    ok = await render_merchant_menu(query.message, callback_data.merchant_id, sheets, state)
    if not ok:
        await query.answer(CATALOG_MERCHANT_UNAVAILABLE, show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data == CANCEL_SWITCH_CALLBACK_DATA)
async def on_cancel_switch_merchant(
    query: CallbackQuery, sheets: SheetsClient, state: FSMContext
) -> None:
    """
    Отмена смены мерчанта — корзина всё это время оставалась нетронутой
    (мы её не трогаем при показе предупреждения), поэтому просто
    возвращаем экран просмотра корзины.
    """
    data = await state.get_data()
    cart = data.get(CART_STATE_KEY)
    if not cart or not cart.get("items"):
        # Защитный случай (маловероятная гонка состояний) — не должны падать.
        await query.answer()
        return

    merchant_id = cart["merchant_id"]
    items = items_for_merchant(await sheets.get_items(), merchant_id)
    item_by_id = {i.item_id: i for i in items}
    text = format_cart_summary(cart["items"], item_by_id)
    await query.message.edit_text(text, reply_markup=_build_cart_view_keyboard())
    await query.answer()


@router.callback_query(F.data == CART_BACK_TO_CATEGORIES_CALLBACK_DATA)
async def on_back_to_categories(query: CallbackQuery, sheets: SheetsClient) -> None:
    """
    Быстрый выход к списку категорий с экрана корзины — без очистки
    корзины (предупреждение о смене мерчанта сработает отдельно, если
    пользователь реально выберет ДРУГОЙ мерчант, см. catalog.py).
    """
    await show_categories(query.message, sheets)
    await query.answer()
