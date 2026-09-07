"""
FSM-состояния пользователя (клиента).

Day 3 (ТЗ §7.4): первое реальное использование FSM *состояний* в
проекте — до этого (корзина, Day 2) хватало произвольных данных
FSMContext.data без явных состояний, т.к. там не было
последовательного пошагового ввода. Оформление заказа — линейный
пошаговый диалог (геолокация → адрес → контакт → подтверждение), и боту
нужно точно знать, на каком шаге пользователь, чтобы правильно
интерпретировать следующее сообщение (геолокация? текст адреса? номер
телефона?).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class OrderStates(StatesGroup):
    """См. handlers/order.py для полного описания флоу по шагам."""

    waiting_for_location = State()
    waiting_for_manual_address = State()
    confirming_address = State()
    waiting_for_contact = State()
    confirming_phone = State()
    confirming_order = State()


class CustomOrderStates(StatesGroup):
    """Нестандартный заказ у мерчанта (ТЗ §7.3.4): ожидание текста/фото
    описания. После отправки на модерацию явное FSM-состояние больше не
    нужно — пока клиент ждёт решения Админа, он может свободно
    пользоваться ботом дальше (см. handlers/custom_order.py: заявка на
    модерации хранится в Redis по user_id, не в FSM).

    waiting_for_reject_reason — состояние АДМИНА (не клиента): живой
    фидбэк из тестирования — голая "/reject_custom_[id]" без причины
    раньше сразу уходила клиенту шаблонным текстом без объяснения,
    теперь запрашивает у Админа причину и ждёт её следующим обычным
    сообщением (тот же паттерн, что и waiting_for_reply_text в
    SupportStates)."""

    waiting_for_description = State()
    waiting_for_reject_reason = State()


class P2PStates(StatesGroup):
    """P2P-доставка «из А в Б» (ТЗ §7.6). В отличие от OrderStates —
    нет отдельного шага подтверждения адреса (§7.6.2/7.6.3 в ТЗ не
    показывают экран «Всё верно?» для точек А/Б — геокодинг сразу ведёт
    к следующему шагу; проверка зоны и предупреждение о неточном адресе
    (partial_match) при этом сохраняются — только явного подтверждения
    "Верно/Изменить" для каждой точки нет). Аналогично — контакт
    (username + телефон) без отдельного экрана подтверждения номера:
    P2P-заказ и так проходит модерацию Админом перед диспетчеризацией,
    опечатка в номере будет заметна на этапе проверки (в отличие от
    standard-заказа, уходящего сразу курьерам)."""

    waiting_for_description = State()
    waiting_for_pickup_location = State()
    waiting_for_pickup_manual_address = State()
    waiting_for_dropoff_location = State()
    waiting_for_dropoff_manual_address = State()
    waiting_for_contact = State()
    confirming_order = State()

    # Состояние АДМИНА (не клиента) — живой фидбэк из тестирования: та
    # же находка, что и у CustomOrderStates.waiting_for_reject_reason /
    # SupportStates.waiting_for_reply_text — голая "/reject_p2p_[id]"
    # без причины раньше сразу уходила клиенту шаблонным текстом.
    waiting_for_reject_reason = State()


class SupportStates(StatesGroup):
    """Вопрос в поддержку (ТЗ §7.5, §11): один шаг — ожидание текста
    вопроса. Экран нарочно минимальный — ТЗ описывает флоу предельно
    сжато ("кнопка... → сообщение Админу"), без промежуточных экранов
    подтверждения, в отличие от P2P/нестандартного заказа.

    waiting_for_reply_text — отдельное состояние ДЛЯ АДМИНА (не для
    клиента): живой баг из тестирования — админ ввёл голую
    "/reply_[id]" без текста (получил "текст не может быть пустым"), а
    затем естественно попытался просто дописать ответ ОТДЕЛЬНЫМ
    сообщением, не повторяя команду целиком — без этого состояния такое
    сообщение улетало в общий fallback, а не клиенту. См.
    handlers/support.py::cmd_reply / on_admin_reply_text_received."""

    waiting_for_question = State()
    waiting_for_reply_text = State()


class AdminStates(StatesGroup):
    """/add_courier (ТЗ §11): "интерактивно: имя, телефон, Telegram ID"
    — единственная админ-команда с многошаговым вводом, остальные —
    однострочные (с параметрами прямо в тексте команды)."""

    adding_courier_name = State()
    adding_courier_phone = State()
    adding_courier_telegram_id = State()
