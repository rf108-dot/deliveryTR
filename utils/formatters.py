"""
Форматирование текста для сообщений (карточки заказа, меню, суммы и т.д.).

Day 1: цена в лирах и карточка позиции меню (ТЗ §7.3.1).
Day 2: сводка корзины (ТЗ §7.3.3).
Day 3-4: format_order_summary(order), format_courier_card(courier) — TODO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from texts.ru import (
    CART_TOTAL_TEMPLATE,
    CART_VIEW_HEADER,
    CART_VIEW_NOTE,
    ITEM_UNAVAILABLE_BUTTON,
    ORDER_SUMMARY_ADDRESS_LINE,
    ORDER_SUMMARY_CONTACT_PHONE_LINE,
    ORDER_SUMMARY_CONTACT_USERNAME_LINE,
    ORDER_SUMMARY_HEADER,
    ORDER_SUMMARY_MERCHANT_LINE,
    ORDER_SUMMARY_PAYMENT_NOTE,
    ORDER_SUMMARY_TOTAL_LINE,
)

if TYPE_CHECKING:
    from services.sheets import MenuItem


def format_price_try(amount: float) -> str:
    """180.0 -> '180 ₺'; 179.5 -> '179.50 ₺' (два знака после запятой, если не целое)."""
    if amount == int(amount):
        return f"{int(amount)} ₺"
    return f"{amount:.2f} ₺"


def format_item_caption(item: "MenuItem", index: int, total: int) -> str:
    """
    Подпись к фото/сообщению позиции меню: название, состав, цена,
    пометка недоступности. Навигация (N из M) — в клавиатуре, не в тексте.
    """
    lines = [f"<b>{item.name}</b>"]
    if item.description:
        lines.append(item.description)
    lines.append(format_price_try(item.price_try))
    if not item.is_available:
        lines.append(f"\n{ITEM_UNAVAILABLE_BUTTON}")
    return "\n".join(lines)


def format_cart_summary(
    cart_items: dict[str, int], item_by_id: dict[str, "MenuItem"]
) -> str:
    """
    Текст просмотра корзины (ТЗ §7.3.3): построчно позиция × количество —
    подытог, затем общий итог и примечание про вознаграждение курьеру.
    Позиции, которых больше нет в меню (item_by_id), молча пропускаются —
    это защита от рассинхрона, если позицию удалили из Sheets после того,
    как её уже положили в корзину.
    """
    lines = [CART_VIEW_HEADER, ""]
    total = 0.0
    for item_id, qty in cart_items.items():
        item = item_by_id.get(item_id)
        if item is None:
            continue
        subtotal = item.price_try * qty
        total += subtotal
        lines.append(f"{item.name} × {qty} — {format_price_try(subtotal)}")
    lines.append("")
    lines.append(CART_TOTAL_TEMPLATE.format(total=format_price_try(total)))
    lines.append("")
    lines.append(CART_VIEW_NOTE)
    return "\n".join(lines)


# TODO Day 3-4: format_order_summary(order) -> str
# TODO Day 5: format_courier_card(courier) -> str


def format_order_summary(
    merchant_name: str,
    address: str,
    contact_username: str | None,
    contact_phone: str | None,
    cart_items: dict[str, int],
    item_by_id: dict[str, "MenuItem"],
) -> str:
    """
    Итоговая сводка перед подтверждением заказа (ТЗ §7.4.3): мерчант,
    адрес, контакт, состав, сумма за товары, примечание про наличную
    оплату. Оба поля contact_username/contact_phone теперь могут быть
    заданы одновременно (Day 4: телефон запрашивается всегда, для
    надёжности поиска админом — не только когда username отсутствует).
    """
    lines = [ORDER_SUMMARY_HEADER, ""]
    lines.append(ORDER_SUMMARY_MERCHANT_LINE.format(name=merchant_name))
    lines.append(ORDER_SUMMARY_ADDRESS_LINE.format(address=address))
    if contact_username:
        lines.append(ORDER_SUMMARY_CONTACT_USERNAME_LINE.format(username=contact_username))
    if contact_phone:
        lines.append(ORDER_SUMMARY_CONTACT_PHONE_LINE.format(phone=contact_phone))
    lines.append("")

    total = 0.0
    for item_id, qty in cart_items.items():
        item = item_by_id.get(item_id)
        if item is None:
            continue
        subtotal = item.price_try * qty
        total += subtotal
        lines.append(f"{item.name} × {qty} — {format_price_try(subtotal)}")

    lines.append("")
    lines.append(ORDER_SUMMARY_TOTAL_LINE.format(total=format_price_try(total)))
    lines.append("")
    lines.append(ORDER_SUMMARY_PAYMENT_NOTE)
    return "\n".join(lines)


def format_order_items_text(items_snapshot: list[dict]) -> str:
    """
    Человекочитаемая сводка состава заказа — используется для колонки
    admin_notes в листе «Заказы» (Day 4, живой фидбэк): items_json
    хранит технический снимок для будущего программного использования
    (Day 5+ — курьерский модуль), а эта строка — то же самое человеку
    для быстрого чтения глазами, без разбора JSON.

    items_snapshot — список словарей {"name", "qty", "price_try"} (тот
    же формат, что уходит в items_json, см. handlers/order.py).
    """
    parts = [
        f"{item['name']} x{item['qty']} ({format_price_try(item['price_try'] * item['qty'])})"
        for item in items_snapshot
    ]
    return "; ".join(parts)
