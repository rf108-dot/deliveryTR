from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from handlers.catalog import (
    CategoryCallback,
    ItemActionCallback,
    ItemNavCallback,
    MerchantCallback,
    build_item_action_keyboard,
    on_category_selected,
    on_item_nav,
    on_item_unavailable,
    on_merchant_selected,
    on_noop,
    show_categories,
)
from services.sheets import Merchant, MenuItem
from texts.ru import (
    CATALOG_CHOOSE_CATEGORY,
    CATALOG_CHOOSE_MERCHANT,
    CATALOG_MERCHANT_UNAVAILABLE,
    CATALOG_NO_CATEGORIES,
    CATALOG_NO_MERCHANTS_IN_CATEGORY,
    CART_SWITCH_WARNING_TEMPLATE,
    ITEM_ADD_BUTTON,
    ITEM_QTY_DEC_BUTTON,
    ITEM_QTY_INC_BUTTON,
    ITEM_UNAVAILABLE_ALERT,
)


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


def _make_message() -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.edit_text = AsyncMock()
    message.edit_media = AsyncMock()
    message.edit_reply_markup = AsyncMock()
    message.delete = AsyncMock()
    return message


def _make_query(message: MagicMock) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = MagicMock(id=42)
    query.answer = AsyncMock()
    return query


def _make_sheets(merchants: list[Merchant], items: list[MenuItem]) -> MagicMock:
    sheets = MagicMock()
    sheets.get_merchants = AsyncMock(return_value=merchants)
    sheets.get_items = AsyncMock(return_value=items)
    sheets.append_event = AsyncMock()
    return sheets


def _make_state(initial_data: dict | None = None) -> MagicMock:
    state = MagicMock()
    state.get_data = AsyncMock(return_value=dict(initial_data or {}))
    state.update_data = AsyncMock()
    return state


# ------------------------------------------------------------------ #
# show_categories
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_show_categories_renders_keyboard_when_merchants_available():
    message = _make_message()
    sheets = _make_sheets([_merchant()], [])

    await show_categories(message, sheets)

    message.answer.assert_awaited_once()
    args, kwargs = message.answer.call_args
    assert args[0] == CATALOG_CHOOSE_CATEGORY
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_show_categories_no_merchants_shows_placeholder():
    message = _make_message()
    sheets = _make_sheets([], [])

    await show_categories(message, sheets)

    message.answer.assert_awaited_once_with(CATALOG_NO_CATEGORIES)


# ------------------------------------------------------------------ #
# on_category_selected
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_category_selected_auto_enters_single_merchant():
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant()
    item = _item()
    sheets = _make_sheets([merchant], [item])
    state = _make_state()

    await on_category_selected(query, CategoryCallback(code="restaurant"), sheets, state)

    # единственный мерчант -> сразу карточка первой позиции новым сообщением
    message.answer_photo.assert_awaited_once()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_category_selected_shows_merchant_list_when_multiple():
    message = _make_message()
    query = _make_query(message)
    merchants = [
        _merchant(merchant_id="rest_001"),
        _merchant(merchant_id="rest_002", name="Kebab House"),
    ]
    sheets = _make_sheets(merchants, [])
    state = _make_state()

    await on_category_selected(query, CategoryCallback(code="restaurant"), sheets, state)

    message.edit_text.assert_awaited_once()
    args, _ = message.edit_text.call_args
    assert args[0] == CATALOG_CHOOSE_MERCHANT


@pytest.mark.asyncio
async def test_category_selected_no_merchants_shows_alert():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([], [])
    state = _make_state()

    await on_category_selected(query, CategoryCallback(code="grocery"), sheets, state)

    query.answer.assert_awaited_once_with(CATALOG_NO_MERCHANTS_IN_CATEGORY, show_alert=True)


@pytest.mark.asyncio
async def test_category_selected_with_other_merchant_cart_shows_switch_warning():
    """Корзина уже есть для rest_002, выбираем rest_001 -> предупреждение, не сразу меню."""
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant(merchant_id="rest_001")
    sheets = _make_sheets([merchant], [_item(merchant_id="rest_001")])
    state = _make_state({"cart": {"merchant_id": "rest_002", "items": {"item_099": 2}}})

    await on_category_selected(query, CategoryCallback(code="restaurant"), sheets, state)

    message.edit_text.assert_awaited_once()
    args, _ = message.edit_text.call_args
    assert args[0] == CART_SWITCH_WARNING_TEMPLATE.format(count=2)
    message.answer_photo.assert_not_called()


# ------------------------------------------------------------------ #
# on_merchant_selected
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_merchant_selected_enters_menu():
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant()
    item = _item()
    sheets = _make_sheets([merchant], [item])
    state = _make_state()

    await on_merchant_selected(query, MerchantCallback(merchant_id="rest_001"), sheets, state)

    message.answer_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_merchant_selected_falls_back_to_text_when_photo_url_is_broken():
    """
    Регресс-тест: реальный случай из живого тестирования — photo_url
    указывал не на файл изображения, а на страницу (Telegram отвечает
    TelegramBadRequest "wrong type of the web page content"). Карточка не
    должна падать полностью — вместо фото показываем текст.
    """
    message = _make_message()
    message.answer_photo.side_effect = TelegramBadRequest(
        method=MagicMock(), message="wrong type of the web page content"
    )
    query = _make_query(message)
    merchant = _merchant()
    item = _item(photo_url="https://example.com/not-a-real-image-page")
    sheets = _make_sheets([merchant], [item])
    state = _make_state()

    await on_merchant_selected(query, MerchantCallback(merchant_id="rest_001"), sheets, state)

    message.answer_photo.assert_awaited_once()  # попытка была
    message.answer.assert_awaited_once()  # но упало в текстовый fallback


@pytest.mark.asyncio
async def test_merchant_selected_unknown_merchant_shows_alert():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([], [])
    state = _make_state()

    await on_merchant_selected(query, MerchantCallback(merchant_id="ghost"), sheets, state)

    query.answer.assert_awaited_once()
    assert query.answer.call_args.kwargs.get("show_alert") is True


# ------------------------------------------------------------------ #
# on_item_nav
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_item_nav_edits_message_in_place_for_photo_item():
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant()
    items = [_item(item_id="item_001"), _item(item_id="item_002", name="Плов")]
    sheets = _make_sheets([merchant], items)
    state = _make_state()

    await on_item_nav(query, ItemNavCallback(merchant_id="rest_001", index=1), sheets, state)

    message.edit_media.assert_awaited_once()
    message.answer_photo.assert_not_called()


@pytest.mark.asyncio
async def test_item_nav_falls_back_to_new_message_on_media_type_change():
    message = _make_message()
    message.edit_media.side_effect = TelegramBadRequest(method=MagicMock(), message="can't edit")
    query = _make_query(message)
    merchant = _merchant()
    items = [_item(item_id="item_001")]
    sheets = _make_sheets([merchant], items)
    state = _make_state()

    await on_item_nav(query, ItemNavCallback(merchant_id="rest_001", index=0), sheets, state)

    message.delete.assert_awaited_once()
    message.answer_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_item_nav_out_of_range_index_is_noop():
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant()
    items = [_item()]
    sheets = _make_sheets([merchant], items)
    state = _make_state()

    await on_item_nav(query, ItemNavCallback(merchant_id="rest_001", index=5), sheets, state)

    message.edit_media.assert_not_called()
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_item_nav_shows_existing_cart_quantity():
    """При навигации на позицию, уже лежащую в корзине, клавиатура должна
    показать [− N +], а не [➕ Добавить] — проверяем через сам keyboard."""
    message = _make_message()
    query = _make_query(message)
    merchant = _merchant()
    items = [_item(item_id="item_001"), _item(item_id="item_002", name="Плов")]
    sheets = _make_sheets([merchant], items)
    state = _make_state({"cart": {"merchant_id": "rest_001", "items": {"item_002": 3}}})

    await on_item_nav(query, ItemNavCallback(merchant_id="rest_001", index=1), sheets, state)

    _, kwargs = message.edit_media.call_args
    keyboard = kwargs["reply_markup"]
    button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("3" in t for t in button_texts)
    assert ITEM_QTY_INC_BUTTON in button_texts
    assert ITEM_QTY_DEC_BUTTON in button_texts


# ------------------------------------------------------------------ #
# on_item_unavailable / on_p2p_entry / on_noop
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_item_unavailable_shows_alert():
    query = _make_query(_make_message())

    await on_item_unavailable(
        query, ItemActionCallback(merchant_id="rest_001", item_id="item_001", action="unavailable")
    )

    query.answer.assert_awaited_once_with(ITEM_UNAVAILABLE_ALERT, show_alert=True)


@pytest.mark.asyncio
async def test_noop_answers_without_alert():
    query = _make_query(_make_message())

    await on_noop(query)

    query.answer.assert_awaited_once_with()


# ------------------------------------------------------------------ #
# build_item_action_keyboard — чистая функция, юнит-тесты без хендлеров
# ------------------------------------------------------------------ #


def test_build_item_keyboard_shows_add_button_when_qty_zero():
    keyboard = build_item_action_keyboard(
        "rest_001", _item(), 0, 1, item_qty=0, cart_total_count=0, cart_total_price=0.0
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert ITEM_ADD_BUTTON in texts
    assert ITEM_QTY_DEC_BUTTON not in texts


def test_build_item_keyboard_shows_qty_controls_when_in_cart():
    keyboard = build_item_action_keyboard(
        "rest_001", _item(), 0, 1, item_qty=2, cart_total_count=2, cart_total_price=360.0
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert ITEM_QTY_DEC_BUTTON in texts
    assert any("2" in t for t in texts)
    assert ITEM_QTY_INC_BUTTON in texts
    assert ITEM_ADD_BUTTON not in texts


def test_build_item_keyboard_shows_unavailable_regardless_of_qty():
    keyboard = build_item_action_keyboard(
        "rest_001",
        _item(is_available=False),
        0,
        1,
        item_qty=0,
        cart_total_count=0,
        cart_total_price=0.0,
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "🚫 Недоступно сейчас" in texts


def test_build_item_keyboard_adds_cart_button_when_cart_non_empty():
    keyboard = build_item_action_keyboard(
        "rest_001", _item(), 0, 1, item_qty=1, cart_total_count=3, cart_total_price=540.0
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Корзина (3)" in t for t in texts)


def test_build_item_keyboard_no_cart_button_when_cart_empty():
    keyboard = build_item_action_keyboard(
        "rest_001", _item(), 0, 1, item_qty=0, cart_total_count=0, cart_total_price=0.0
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert not any("Корзина" in t for t in texts)
