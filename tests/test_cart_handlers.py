from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from handlers.cart import (
    CART_BACK_TO_CATEGORIES_CALLBACK_DATA,
    CART_CHECKOUT_CALLBACK_DATA,
    CART_EDIT_CALLBACK_DATA,
    on_add_to_cart,
    on_back_to_categories,
    on_cancel_switch_merchant,
    on_cart_edit,
    on_confirm_switch_merchant,
    on_decrement,
    on_increment,
    on_view_cart,
)
from handlers.catalog import CartQtyCallback, ConfirmSwitchMerchantCallback, ItemActionCallback
from services.sheets import Merchant, MenuItem
from texts.ru import (
    CART_EMPTY_ALERT,
    CART_ITEM_LIMIT_ALERT_TEMPLATE,
)


def _item(**overrides) -> MenuItem:
    defaults = dict(
        item_id="item_001",
        merchant_id="rest_001",
        name="Борщ",
        description="",
        price_try=180.0,
        photo_url="https://example.com/1.jpg",
        is_active=True,
        is_available=True,
    )
    defaults.update(overrides)
    return MenuItem(**defaults)


def _merchant(**overrides) -> Merchant:
    defaults = dict(
        merchant_id="rest_001",
        category="restaurant",
        name="MonAmi",
        description="",
        is_active=True,
        today_confirmed=True,
        working_hours="10:00-21:00",
    )
    defaults.update(overrides)
    return Merchant(**defaults)


def _make_message() -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.edit_text = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    return message


def _make_query(message: MagicMock) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = MagicMock(id=42)
    query.answer = AsyncMock()
    return query


def _make_sheets(items: list[MenuItem], merchants: list[Merchant] | None = None) -> MagicMock:
    sheets = MagicMock()
    sheets.get_items = AsyncMock(return_value=items)
    sheets.get_merchants = AsyncMock(return_value=merchants or [])
    sheets.append_event = AsyncMock()
    return sheets


def _make_state(initial_data: dict | None = None) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=dict(initial_data or {}))
    state.update_data = AsyncMock()
    return state


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


# ------------------------------------------------------------------ #
# on_add_to_cart
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_add_to_cart_first_time_creates_cart(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item()])
    state = _make_state()
    settings = _make_settings(valid_settings_kwargs, MAX_ITEMS_PER_TYPE=10)

    await on_add_to_cart(
        query,
        ItemActionCallback(merchant_id="rest_001", item_id="item_001", action="add"),
        sheets,
        state,
        settings,
    )

    state.update_data.assert_awaited_once_with(
        cart={"merchant_id": "rest_001", "items": {"item_001": 1}}
    )
    message.edit_reply_markup.assert_awaited_once()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_to_cart_respects_limit(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item()])
    state = _make_state({"cart": {"merchant_id": "rest_001", "items": {"item_001": 2}}})
    settings = _make_settings(valid_settings_kwargs, MAX_ITEMS_PER_TYPE=2)

    await on_add_to_cart(
        query,
        ItemActionCallback(merchant_id="rest_001", item_id="item_001", action="add"),
        sheets,
        state,
        settings,
    )

    state.update_data.assert_not_called()
    query.answer.assert_awaited_once_with(
        CART_ITEM_LIMIT_ALERT_TEMPLATE.format(max=2), show_alert=True
    )


# ------------------------------------------------------------------ #
# on_increment / on_decrement
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_increment_existing_item(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item()])
    state = _make_state({"cart": {"merchant_id": "rest_001", "items": {"item_001": 1}}})
    settings = _make_settings(valid_settings_kwargs, MAX_ITEMS_PER_TYPE=10)

    await on_increment(
        query,
        CartQtyCallback(merchant_id="rest_001", item_id="item_001", action="inc"),
        sheets,
        state,
        settings,
    )

    state.update_data.assert_awaited_once_with(
        cart={"merchant_id": "rest_001", "items": {"item_001": 2}}
    )


@pytest.mark.asyncio
async def test_decrement_reduces_quantity():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item()])
    state = _make_state({"cart": {"merchant_id": "rest_001", "items": {"item_001": 2}}})

    await on_decrement(
        query,
        CartQtyCallback(merchant_id="rest_001", item_id="item_001", action="dec"),
        sheets,
        state,
    )

    state.update_data.assert_awaited_once_with(
        cart={"merchant_id": "rest_001", "items": {"item_001": 1}}
    )


@pytest.mark.asyncio
async def test_decrement_from_one_removes_item():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item()])
    state = _make_state({"cart": {"merchant_id": "rest_001", "items": {"item_001": 1}}})

    await on_decrement(
        query,
        CartQtyCallback(merchant_id="rest_001", item_id="item_001", action="dec"),
        sheets,
        state,
    )

    state.update_data.assert_awaited_once_with(cart=None)


@pytest.mark.asyncio
async def test_decrement_below_zero_is_noop():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item()])
    state = _make_state()  # пустая корзина

    await on_decrement(
        query,
        CartQtyCallback(merchant_id="rest_001", item_id="item_001", action="dec"),
        sheets,
        state,
    )

    state.update_data.assert_not_called()
    query.answer.assert_awaited_once_with()


# ------------------------------------------------------------------ #
# on_view_cart
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_view_cart_shows_summary():
    message = _make_message()
    query = _make_query(message)
    items = [_item(item_id="item_001", price_try=180.0), _item(item_id="item_002", name="Плов", price_try=150.0)]
    sheets = _make_sheets(items)
    state = _make_state(
        {"cart": {"merchant_id": "rest_001", "items": {"item_001": 2, "item_002": 1}}}
    )

    await on_view_cart(query, sheets, state)

    message.answer.assert_awaited_once()
    args, _ = message.answer.call_args
    text = args[0]
    assert "Борщ" in text
    assert "Плов" in text
    assert "510" in text  # 180*2 + 150*1 = 510


@pytest.mark.asyncio
async def test_view_cart_empty_shows_alert():
    query = _make_query(_make_message())
    sheets = _make_sheets([])
    state = _make_state()

    await on_view_cart(query, sheets, state)

    query.answer.assert_awaited_once_with(CART_EMPTY_ALERT, show_alert=True)


# ------------------------------------------------------------------ #
# on_cart_edit
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_cart_edit_empty_cart_shows_alert():
    query = _make_query(_make_message())
    sheets = _make_sheets([])
    state = _make_state()

    await on_cart_edit(query, sheets, state)

    query.answer.assert_awaited_once_with(CART_EMPTY_ALERT, show_alert=True)


@pytest.mark.asyncio
async def test_back_to_categories_shows_categories_without_clearing_cart():
    message = _make_message()
    query = _make_query(message)
    sheets = MagicMock()
    sheets.get_merchants = AsyncMock(return_value=[])

    await on_back_to_categories(query, sheets)

    message.answer.assert_awaited_once()
    query.answer.assert_awaited_once()


# ------------------------------------------------------------------ #
# confirm/cancel смены мерчанта (ТЗ §7.3.2)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_confirm_switch_merchant_clears_cart_and_enters_new_menu():
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant(merchant_id="rest_001")
    sheets = _make_sheets([_item(merchant_id="rest_001")], merchants=[merchant])
    state = _make_state({"cart": {"merchant_id": "rest_002", "items": {"item_099": 2}}})

    await on_confirm_switch_merchant(
        query, ConfirmSwitchMerchantCallback(merchant_id="rest_001"), sheets, state
    )

    state.update_data.assert_any_call(cart=None)
    message.answer_photo.assert_awaited_once()  # новое меню мерчанта открылось


@pytest.mark.asyncio
async def test_cancel_switch_merchant_returns_to_cart_view():
    """
    Регресс-тест: реальный баг из живого тестирования — "Отмена" раньше
    просто гасила алерт, оставляя пользователя на экране предупреждения
    без выхода. Теперь должна вернуть экран просмотра корзины (которая
    всё это время оставалась нетронутой).
    """
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([_item(merchant_id="rest_001", price_try=330.0)])
    state = _make_state({"cart": {"merchant_id": "rest_001", "items": {"item_001": 3}}})

    await on_cancel_switch_merchant(query, sheets, state)

    message.edit_text.assert_awaited_once()
    args, _ = message.edit_text.call_args
    assert "Борщ" in args[0]
    assert "990" in args[0]  # 330 * 3
    state.update_data.assert_not_called()  # корзину не трогаем


@pytest.mark.asyncio
async def test_cancel_switch_merchant_empty_cart_is_safe_noop():
    query = _make_query(_make_message())
    sheets = _make_sheets([])
    state = _make_state()  # корзины нет вовсе — защитный случай

    await on_cancel_switch_merchant(query, sheets, state)

    query.answer.assert_awaited_once_with()


def test_callback_data_constants_are_distinct():
    values = {
        CART_EDIT_CALLBACK_DATA,
        CART_BACK_TO_CATEGORIES_CALLBACK_DATA,
        CART_CHECKOUT_CALLBACK_DATA,
    }
    assert len(values) == 3
