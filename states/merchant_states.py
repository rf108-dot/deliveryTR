"""
FSM-состояния ресторана (ТЗ v2.3, §8A — модуль самообслуживания
мерчанта).

Единственный многошаговый диалог — «➕ Добавить позицию» (§8A.4): те же
причины, что у AdminStates.adding_courier_* в states/user_states.py —
несколько последовательных текстовых полей, боту нужно знать, на каком
шаге находится ресторан, чтобы правильно интерпретировать следующее
сообщение (название? цена?).

Подтверждение меню (§8A.2) и «сегодня нет» / «есть снова» (§8A.3) —
однократные нажатия кнопки с item_id/merchant_id в callback_data,
явного состояния не требуют (тот же принцип, что у handlers/courier.py
для этапов заказа).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MerchantStates(StatesGroup):
    """См. handlers/merchant.py::on_add_item_button и последующие шаги."""

    adding_item_name = State()
    adding_item_description = State()
    adding_item_price = State()
    confirming_new_item = State()
