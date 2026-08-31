from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from handlers.order import (
    ADDRESS_CONFIRM_CALLBACK_DATA,
    ADDRESS_EDIT_CALLBACK_DATA,
    ORDER_BACK_TO_CART_CALLBACK_DATA,
    ORDER_CONFIRM_CALLBACK_DATA,
    ORDER_DRAFT_KEY,
    on_address_confirmed,
    on_address_edit,
    on_back_to_cart,
    on_checkout_start,
    on_contact_received,
    on_contact_typed_as_text,
    on_location_received,
    on_manual_address_chosen,
    on_manual_address_received,
    on_order_again,
    on_order_confirmed,
    on_phone_confirmed,
    on_phone_edit,
    on_unexpected_input_in_location_step,
)
from handlers.cart import CART_STATE_KEY
from services.geocoding import GeocodeResult
from services.sheets import Merchant, MenuItem, SheetsWriteError
from texts.ru import (
    CART_EMPTY_ALERT,
    ORDER_ACCEPTED_DEFAULT,
    ORDER_ACCEPTED_MANUAL_ADDRESS_TEMPLATE,
    ORDER_ADDRESS_CONFIRM_TEMPLATE,
    ORDER_ADDRESS_NOT_FOUND,
    ORDER_ADDRESS_PARTIAL_MATCH_WARNING,
    ORDER_CONTACT_INVALID_FORMAT_MESSAGE,
    ORDER_CONTACT_PROMPT,
    ORDER_CONTACT_RECEIVED_ACK,
    ORDER_ITEMS_UNAVAILABLE,
    ORDER_MANUAL_ADDRESS_PROMPT,
    ORDER_PHONE_CONFIRM_TEMPLATE,
    ORDER_SAVE_FAILED_MESSAGE,
    ORDER_UNEXPECTED_INPUT_IN_LOCATION_STEP,
    ORDER_ZONE_OUTSIDE_MESSAGE,
    ORDER_ZONE_OUTSIDE_UNCERTAIN_MESSAGE,
    PAYMENT_NOT_IMPLEMENTED_MESSAGE,
    SERVICE_MANUALLY_STOPPED_MESSAGE,
)

ANTALYA_LAT = 36.8841
ANTALYA_LON = 30.7056
ANKARA_LAT = 39.9334
ANKARA_LON = 32.8597


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


def _make_message(**overrides) -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.from_user = MagicMock(id=100, username=None)
    message.location = None
    message.text = None
    message.contact = None
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _make_query(message: MagicMock, from_user=None) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = from_user or MagicMock(id=100, username=None)
    query.answer = AsyncMock()
    return query


def _make_sheets(items: list[MenuItem], merchants: list[Merchant] | None = None) -> MagicMock:
    sheets = MagicMock()
    sheets.get_items = AsyncMock(return_value=items)
    sheets.get_merchants = AsyncMock(return_value=merchants or [])
    sheets.append_event = AsyncMock()
    sheets.append_order = AsyncMock()
    return sheets


def _make_redis(stopped: bool = False, order_id_seq: int = 1) -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"1" if stopped else None)
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    redis.incr = AsyncMock(return_value=order_id_seq)
    return redis


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def _make_scheduler() -> MagicMock:
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    return scheduler


@pytest.fixture(autouse=True)
def _mock_dispatch(monkeypatch):
    """
    on_order_confirmed теперь реально вызывает send_offer_to_couriers
    (Day 5) — детально это уже покрыто отдельными тестами в
    test_dispatch.py, здесь достаточно мокнуть, чтобы тесты подтверждения
    заказа не зависели от полной цепочки курьеров/Redis-захвата. Отдельный
    тест ниже (test_order_confirmed_triggers_dispatch_with_correct_args)
    проверяет именно факт и параметры вызова.
    """
    mock = AsyncMock()
    monkeypatch.setattr("handlers.order.send_offer_to_couriers", mock)
    return mock


def _make_state(initial_data: dict | None = None) -> MagicMock:
    """
    В отличие от простого фиксированного мока (использовался в тестах
    Day 1-2, где каждый хендлер независим) — этот мок реально накапливает
    изменения между update_data() и get_data(). Флоу оформления заказа
    многошаговый: например, on_manual_address_received пишет адрес, затем
    сам же вызывает _proceed_to_contact_or_confirmation → _show_final_confirmation,
    которая перечитывает данные и должна увидеть то, что записал предыдущий шаг.
    """
    storage: dict = dict(initial_data or {})

    async def _get_data():
        return dict(storage)

    async def _update_data(**kwargs):
        storage.update(kwargs)
        return dict(storage)

    state = MagicMock()
    state.get_data = AsyncMock(side_effect=_get_data)
    state.update_data = AsyncMock(side_effect=_update_data)
    state.set_state = AsyncMock()
    return state


def _make_geocoding(reverse_result=None, geocode_result=None) -> MagicMock:
    geocoding = MagicMock()
    geocoding.reverse_geocode = AsyncMock(return_value=reverse_result)
    geocoding.geocode = AsyncMock(return_value=geocode_result)
    return geocoding


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    # БАГ (живой, обнаружен по факту): valid_settings_kwargs уже задаёт
    # SERVICE_OPEN/LAST_ORDER/SERVICE_CLOSE своими значениями (10:00/
    # 20:15/21:00) — setdefault() ниже был no-op, раз ключи уже
    # присутствуют в kwargs. Баг был скрыт, пока реальное время суток на
    # момент прогона pytest случайно попадало в это окно; перестало
    # быть скрытым, как только тесты запустили вечером после 21:00.
    # Явно убираем эти ключи, чтобы setdefault ниже реально сработал.
    for key in ("SERVICE_OPEN", "LAST_ORDER", "SERVICE_CLOSE"):
        kwargs.pop(key, None)
    kwargs.update(overrides)
    kwargs.setdefault("ZONE_CENTER_LAT", ANTALYA_LAT)
    kwargs.setdefault("ZONE_CENTER_LON", ANTALYA_LON)
    kwargs.setdefault("ZONE_RADIUS_KM", 15)
    # Намеренно широкое окно работы по умолчанию — чтобы тесты НЕ зависели
    # от реального времени на часах в момент запуска pytest. Тесты,
    # специально проверяющие блокировку по часам, используют
    # _make_redis(stopped=True) — это детерминированно независимо от времени.
    kwargs.setdefault("SERVICE_OPEN", "00:00")
    kwargs.setdefault("LAST_ORDER", "23:58")
    kwargs.setdefault("SERVICE_CLOSE", "23:59")
    return Settings(**kwargs)


_CART_WITH_ITEM = {"merchant_id": "rest_001", "items": {"item_001": 2}}


# ------------------------------------------------------------------ #
# Точка входа
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_checkout_start_empty_cart_shows_alert(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state()
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    await on_checkout_start(query, state, settings, redis)

    query.answer.assert_awaited_once_with(CART_EMPTY_ALERT, show_alert=True)
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_checkout_start_sends_location_prompt(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    await on_checkout_start(query, state, settings, redis)

    message.answer.assert_awaited_once()
    state.set_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkout_start_blocked_when_service_manually_stopped(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis(stopped=True)

    await on_checkout_start(query, state, settings, redis)

    query.answer.assert_awaited_once_with(SERVICE_MANUALLY_STOPPED_MESSAGE, show_alert=True)
    state.set_state.assert_not_called()


# ------------------------------------------------------------------ #
# Шаг 1: геолокация
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_location_outside_zone_is_rejected_and_allows_retry(valid_settings_kwargs):
    """
    Регресс-тест: реальный баг из живого тестирования — отказ "вне зоны"
    раньше сбрасывал FSM-состояние в None, из-за чего следующая попытка
    пользователя (другой адрес) вообще не попадала ни в один хендлер и
    оставалась без ответа. Состояние должно остаться waiting_for_location.
    """
    message = _make_message(location=MagicMock(latitude=ANKARA_LAT, longitude=ANKARA_LON))
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding()
    settings = _make_settings(valid_settings_kwargs)

    await on_location_received(message, state, geocoding, settings)

    assert message.answer.await_count == 2
    first_args, _ = message.answer.call_args_list[0]
    assert first_args[0] == ORDER_ZONE_OUTSIDE_MESSAGE
    geocoding.reverse_geocode.assert_not_called()
    state.set_state.assert_not_called()  # состояние НЕ сброшено — можно повторить


@pytest.mark.asyncio
async def test_location_inside_zone_with_address_shows_confirmation(valid_settings_kwargs):
    message = _make_message(location=MagicMock(latitude=ANTALYA_LAT, longitude=ANTALYA_LON))
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(reverse_result="Lara Cad. 12, Antalya")
    settings = _make_settings(valid_settings_kwargs)

    await on_location_received(message, state, geocoding, settings)

    # Первый вызов — "получили геолокацию", второй — сама карточка адреса
    assert message.answer.await_count == 2
    last_args, last_kwargs = message.answer.call_args
    assert last_args[0] == ORDER_ADDRESS_CONFIRM_TEMPLATE.format(address="Lara Cad. 12, Antalya")
    assert "reply_markup" in last_kwargs


@pytest.mark.asyncio
async def test_location_inside_zone_geocode_fails_falls_back_to_manual(valid_settings_kwargs):
    message = _make_message(location=MagicMock(latitude=ANTALYA_LAT, longitude=ANTALYA_LON))
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(reverse_result=None)
    settings = _make_settings(valid_settings_kwargs)

    await on_location_received(message, state, geocoding, settings)

    last_args, _ = message.answer.call_args
    assert last_args[0] == ORDER_MANUAL_ADDRESS_PROMPT


@pytest.mark.asyncio
async def test_manual_address_chosen_prompts_for_text():
    message = _make_message()
    state = _make_state()

    await on_manual_address_chosen(message, state)

    message.answer.assert_awaited_once()
    args, _ = message.answer.call_args
    assert args[0] == ORDER_MANUAL_ADDRESS_PROMPT


@pytest.mark.asyncio
async def test_unexpected_input_in_location_step_reminds_buttons():
    message = _make_message()

    await on_unexpected_input_in_location_step(message)

    message.answer.assert_awaited_once_with(ORDER_UNEXPECTED_INPUT_IN_LOCATION_STEP)


# ------------------------------------------------------------------ #
# Ручной ввод адреса
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_manual_address_not_found_asks_to_retype(valid_settings_kwargs):
    message = _make_message(text="дом у моря")
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(geocode_result=None)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets([])

    await on_manual_address_received(message, state, geocoding, settings, sheets)

    message.answer.assert_awaited_once_with(ORDER_ADDRESS_NOT_FOUND)
    state.set_state.assert_not_called()


@pytest.mark.asyncio
async def test_manual_address_outside_zone_is_rejected_and_allows_retry(valid_settings_kwargs):
    message = _make_message(text="где-то в Анкаре")
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(geocode_result=GeocodeResult(lat=ANKARA_LAT, lon=ANKARA_LON, partial_match=False))
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets([])

    await on_manual_address_received(message, state, geocoding, settings, sheets)

    assert message.answer.await_count == 2
    first_args, _ = message.answer.call_args_list[0]
    assert first_args[0] == ORDER_ZONE_OUTSIDE_MESSAGE
    state.set_state.assert_not_called()  # состояние НЕ сброшено — можно ввести другой адрес


@pytest.mark.asyncio
async def test_manual_address_outside_zone_with_partial_match_shows_uncertain_message(
    valid_settings_kwargs,
):
    """
    Живой фидбэк: выдуманный адрес геокодировался неточно (partial_match)
    и точка попала вне зоны — пользователь должен понимать, что дело в
    неточности адреса, а не в обычном "вне зоны".
    """
    message = _make_message(text="Liman, 1234 sokak 336")
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(
        geocode_result=GeocodeResult(lat=ANKARA_LAT, lon=ANKARA_LON, partial_match=True)
    )
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets([])

    await on_manual_address_received(message, state, geocoding, settings, sheets)

    first_args, _ = message.answer.call_args_list[0]
    assert first_args[0] == ORDER_ZONE_OUTSIDE_UNCERTAIN_MESSAGE


@pytest.mark.asyncio
async def test_manual_address_inside_zone_with_username_still_asks_for_phone(valid_settings_kwargs):
    """
    Изменено по живому фидбэку Day 4: телефон теперь запрашивается ВСЕГДА
    (для надёжности поиска админом), даже если у пользователя есть
    @username — раньше в этом случае шаг контакта пропускался целиком.
    """
    message = _make_message(text="Лара Кад. 12", from_user=MagicMock(id=100, username="ivan"))
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(geocode_result=GeocodeResult(lat=ANTALYA_LAT, lon=ANTALYA_LON, partial_match=False))
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_manual_address_received(message, state, geocoding, settings, sheets)

    last_args, last_kwargs = message.answer.call_args
    assert last_args[0] == ORDER_CONTACT_PROMPT
    assert "reply_markup" in last_kwargs
    # username уже сохранён в черновике, несмотря на то что просим ещё и телефон
    data = await state.get_data()
    assert data[ORDER_DRAFT_KEY]["contact_username"] == "ivan"


@pytest.mark.asyncio
async def test_manual_address_without_username_asks_for_contact(valid_settings_kwargs):
    message = _make_message(text="Лара Кад. 12", from_user=MagicMock(id=100, username=None))
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(geocode_result=GeocodeResult(lat=ANTALYA_LAT, lon=ANTALYA_LON, partial_match=False))
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_manual_address_received(message, state, geocoding, settings, sheets)

    last_args, _ = message.answer.call_args
    assert last_args[0] == ORDER_CONTACT_PROMPT


@pytest.mark.asyncio
async def test_contact_received_after_username_shows_both_in_final_summary(valid_settings_kwargs):
    """
    Сквозной сценарий: username уже был сохранён на шаге адреса, затем
    пользователь присылает телефон — итоговая сводка должна показать ОБА
    контакта, а не только последний записанный (username не затирается).
    """
    message = _make_message(
        contact=MagicMock(phone_number="+905551234567", user_id=100),
        from_user=MagicMock(id=100, username=None),
    )
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "contact_username": "ivan"},
        }
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_contact_received(message, state, sheets)

    last_args, _ = message.answer.call_args
    assert "@ivan" in last_args[0]
    assert "+905551234567" in last_args[0]


@pytest.mark.asyncio
async def test_manual_address_partial_match_warns_but_does_not_block(valid_settings_kwargs):
    """
    Живой фидбэк: несуществующий адрес ("Liman 1234 sokak 34") Google
    геокодирует с partial_match=True (откатился на реальный район).
    Заказ НЕ блокируется — только предупреждаем клиента, точность адреса
    и так не проверяется строго по ТЗ §7.4.1 (курьер уточнит на месте).
    """
    message = _make_message(
        text="Liman 1234 sokak 34, Antalya", from_user=MagicMock(id=100, username="ivan")
    )
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    geocoding = _make_geocoding(
        geocode_result=GeocodeResult(lat=ANTALYA_LAT, lon=ANTALYA_LON, partial_match=True)
    )
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_manual_address_received(message, state, geocoding, settings, sheets)

    # Первый answer — предупреждение о неточности, второй — запрос телефона
    # (с Day 4 телефон запрашивается всегда, даже при наличии username)
    assert message.answer.await_count == 2
    warning_args, _ = message.answer.call_args_list[0]
    assert warning_args[0] == ORDER_ADDRESS_PARTIAL_MATCH_WARNING
    last_args, _ = message.answer.call_args
    assert last_args[0] == ORDER_CONTACT_PROMPT
    data = await state.get_data()
    assert data[ORDER_DRAFT_KEY]["address"] == "Liman 1234 sokak 34, Antalya"


# ------------------------------------------------------------------ #
# Подтверждение/редактирование геокодированного адреса
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_address_confirmed_proceeds_to_contact_or_summary():
    message = _make_message()
    query = _make_query(message, from_user=MagicMock(id=100, username="ivan"))
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "address_manually_edited": False},
        }
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_address_confirmed(query, state, sheets)

    message.answer.assert_awaited_once()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_address_edit_switches_to_manual_entry():
    message = _make_message()
    query = _make_query(message)
    state = _make_state()

    await on_address_edit(query, state)

    message.edit_text.assert_awaited_once_with(ORDER_MANUAL_ADDRESS_PROMPT)
    state.set_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_address_edit_shows_current_address_for_copying():
    """
    Регресс-тест на living-тестовый фидбэк: «Изменить» должен показывать
    уже распознанный адрес (чтобы скопировать и поправить одну деталь,
    например номер дома), а не пустой запрос "введите текстом".
    """
    message = _make_message()
    query = _make_query(message)
    state = _make_state(
        {ORDER_DRAFT_KEY: {"address": "Çağlayan, 2054. Sk. No:38, Antalya"}}
    )

    await on_address_edit(query, state)

    message.edit_text.assert_awaited_once()
    args, _ = message.edit_text.call_args
    assert "Çağlayan, 2054. Sk. No:38, Antalya" in args[0]


# ------------------------------------------------------------------ #
# Контакт
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_contact_received_own_contact_shows_phone_confirmation():
    message = _make_message(
        contact=MagicMock(phone_number="+905551234567", user_id=100),
        from_user=MagicMock(id=100, username=None),
    )
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_contact_received(message, state, sheets)

    # Первый answer — подтверждение получения контакта, второй — подтверждение
    # номера (нет username -> шаг не пропускается)
    assert message.answer.await_count == 2
    last_args, _ = message.answer.call_args
    assert "+905551234567" in last_args[0]


@pytest.mark.asyncio
async def test_contact_received_mismatched_user_asks_again():
    message = _make_message(
        contact=MagicMock(phone_number="+905551234567", user_id=999),  # чужой контакт
        from_user=MagicMock(id=100, username=None),
    )
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    sheets = _make_sheets([_item()])

    await on_contact_received(message, state, sheets)

    message.answer.assert_awaited_once()
    args, _ = message.answer.call_args
    assert args[0] == ORDER_CONTACT_PROMPT


@pytest.mark.asyncio
async def test_contact_typed_as_text_accepts_valid_looking_number():
    """
    Регресс-тест: реальный баг из живого тестирования — если пользователь
    печатает номер текстом (например, кнопка request_contact не сработала
    на Desktop — та же история, что и с геолокацией на Day 3), сообщение
    раньше вообще не попадало ни в один хендлер (полная тишина).
    """
    message = _make_message(text="+905551234567")
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_contact_typed_as_text(message, state, sheets)

    assert message.answer.await_count == 2
    first_args, _ = message.answer.call_args_list[0]
    assert first_args[0] == ORDER_CONTACT_RECEIVED_ACK
    last_args, _ = message.answer.call_args
    assert "+905551234567" in last_args[0]  # подтверждение номера (нет username)
    data = await state.get_data()
    assert data[ORDER_DRAFT_KEY]["contact_phone"] == "+905551234567"


@pytest.mark.asyncio
async def test_contact_typed_as_text_tolerates_spaces_and_dashes():
    message = _make_message(text="+90 555-123-45-67")
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_contact_typed_as_text(message, state, sheets)

    data = await state.get_data()
    assert data[ORDER_DRAFT_KEY]["contact_phone"] == "+905551234567"


@pytest.mark.asyncio
async def test_contact_typed_as_text_rejects_garbage():
    message = _make_message(text="позвоните мне позже")
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    sheets = _make_sheets([_item()])

    await on_contact_typed_as_text(message, state, sheets)

    message.answer.assert_awaited_once_with(ORDER_CONTACT_INVALID_FORMAT_MESSAGE)


# ------------------------------------------------------------------ #
# Подтверждение/изменение номера телефона (Day 4 hotfix)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_phone_confirmed_proceeds_to_final_summary():
    query = _make_query(_make_message())
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "contact_phone": "+905551234567"},
        }
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_phone_confirmed(query, state, sheets)

    query.message.answer.assert_awaited_once()
    last_args, _ = query.message.answer.call_args
    assert "+905551234567" in last_args[0]
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_phone_edit_only_changes_phone_not_whole_checkout():
    """
    Регресс-тест на живой фидбэк: «Изменить» на подтверждении номера
    должно менять ТОЛЬКО телефон — адрес и username в черновике должны
    остаться нетронутыми (иначе пользователю пришлось бы заново проходить
    весь чекаут, рискуя задвоением заказа при повторном подтверждении).
    """
    message = _make_message()
    query = _make_query(message)
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {
                "address": "Lara Cad. 12",
                "contact_phone": "+905551234567",  # ошибочный номер
            },
        }
    )

    await on_phone_edit(query, state)

    message.answer.assert_awaited_once()
    args, kwargs = message.answer.call_args
    assert args[0] == ORDER_CONTACT_PROMPT
    assert "reply_markup" in kwargs
    state.set_state.assert_awaited_once()
    # Адрес не должен быть тронут — меняем только телефон
    data = await state.get_data()
    assert data[ORDER_DRAFT_KEY]["address"] == "Lara Cad. 12"


@pytest.mark.asyncio
async def test_phone_without_username_shows_confirmation_step():
    """Без @username шаг подтверждения номера НЕ пропускается."""
    message = _make_message(
        contact=MagicMock(phone_number="+905551234567", user_id=100),
        from_user=MagicMock(id=100, username=None),
    )
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_contact_received(message, state, sheets)

    last_args, _ = message.answer.call_args
    assert last_args[0] == ORDER_PHONE_CONFIRM_TEMPLATE.format(phone="+905551234567")


@pytest.mark.asyncio
async def test_phone_with_username_skips_confirmation_step():
    """
    С @username шаг подтверждения номера пропускается — админ/курьер и
    так найдут пользователя по юзернейму, даже если номер окажется
    неверным (продуктовое решение по запросу).
    """
    message = _make_message(
        contact=MagicMock(phone_number="+905551234567", user_id=100),
        from_user=MagicMock(id=100, username="ivan"),
    )
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "contact_username": "ivan"},
        }
    )
    sheets = _make_sheets([_item()], merchants=[_merchant()])

    await on_contact_received(message, state, sheets)

    last_args, _ = message.answer.call_args
    # Сразу итоговая сводка, а не запрос подтверждения номера
    assert "@ivan" in last_args[0]
    assert last_args[0] != ORDER_PHONE_CONFIRM_TEMPLATE.format(phone="+905551234567")


# ------------------------------------------------------------------ #
# Итоговое подтверждение
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_order_confirmed_default_message_when_address_not_edited(valid_settings_kwargs):
    query = _make_query(_make_message(text="📋 Проверьте заказ:\n...\nКонтакт: +905551234567"))
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "address_manually_edited": False},
        }
    )
    sheets = _make_sheets([_item(is_available=True)])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.message.edit_text.assert_awaited_once()
    args, kwargs = query.message.edit_text.call_args
    # Сводка (включая контакт) должна остаться видна, не стереться —
    # живой фидбэк: раньше edit_text() заменял весь текст на "Заказ принят"
    assert "Контакт: +905551234567" in args[0]
    assert ORDER_ACCEPTED_DEFAULT in args[0]
    assert kwargs.get("reply_markup") is not None  # кнопка «Заказать ещё раз»
    state.update_data.assert_any_call(cart=None, order_draft=None)


@pytest.mark.asyncio
async def test_order_confirmed_manual_address_message(valid_settings_kwargs):
    query = _make_query(_make_message(text="📋 Проверьте заказ:\n...\nАдрес: Lara Cad. 12"))
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "address_manually_edited": True},
        }
    )
    sheets = _make_sheets([_item(is_available=True)])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    args, kwargs = query.message.edit_text.call_args
    assert "Адрес: Lara Cad. 12" in args[0]
    assert ORDER_ACCEPTED_MANUAL_ADDRESS_TEMPLATE.format(address="Lara Cad. 12") in args[0]
    assert kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_order_confirmed_blocks_when_item_became_unavailable(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item(is_available=False)])  # стало недоступно
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.message.answer.assert_awaited_once()
    args, _ = query.message.answer.call_args
    assert args[0] == ORDER_ITEMS_UNAVAILABLE
    query.message.edit_text.assert_not_called()
    state.update_data.assert_not_called()  # корзину не трогаем — заказ не создан
    sheets.append_order.assert_not_called()


@pytest.mark.asyncio
async def test_order_confirmed_empty_cart_shows_alert(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state()
    sheets = _make_sheets([])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.answer.assert_awaited_once_with(CART_EMPTY_ALERT, show_alert=True)


@pytest.mark.asyncio
async def test_order_confirmed_blocked_when_service_manually_stopped(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item(is_available=True)])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis(stopped=True)

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.message.answer.assert_awaited_once_with(SERVICE_MANUALLY_STOPPED_MESSAGE)
    sheets.append_order.assert_not_called()
    state.update_data.assert_not_called()


@pytest.mark.asyncio
async def test_order_confirmed_blocked_when_payment_enabled(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item(is_available=True)])
    settings = _make_settings(
        valid_settings_kwargs, PAYMENT_ENABLED=True, IYZICO_API_KEY="fake-key"
    )
    redis = _make_redis()

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.message.answer.assert_awaited_once_with(PAYMENT_NOT_IMPLEMENTED_MESSAGE)
    sheets.append_order.assert_not_called()


@pytest.mark.asyncio
async def test_order_confirmed_writes_order_with_generated_id(valid_settings_kwargs):
    query = _make_query(_make_message())
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,  # {"item_001": 2}, price 180.0 -> 360.0
            ORDER_DRAFT_KEY: {
                "address": "Lara Cad. 12",
                "lat": ANTALYA_LAT,
                "lon": ANTALYA_LON,
                "contact_username": "ivan",
                "address_manually_edited": False,
            },
        }
    )
    sheets = _make_sheets([_item(is_available=True)], merchants=[_merchant()])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis(order_id_seq=42)

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    redis.incr.assert_awaited_once()
    sheets.append_order.assert_awaited_once()
    order_fields = sheets.append_order.call_args.args[0]
    assert order_fields["order_id"] == "042"
    assert order_fields["merchant_id"] == "rest_001"
    assert order_fields["merchant_name"] == "MonAmi"
    assert order_fields["total_try"] == "360.0"
    assert order_fields["status"] == "new"
    assert order_fields["username"] == "ivan"
    sheets.append_event.assert_awaited_with(
        event_type="order_placed", actor_role="client", actor_id="100", order_id="042"
    )


@pytest.mark.asyncio
async def test_order_confirmed_triggers_dispatch_with_correct_args(
    valid_settings_kwargs, _mock_dispatch
):
    """Day 5: после успешной записи заказа должна запуститься диспетчеризация
    курьерам (send_offer_to_couriers) с правильными данными заказа."""
    query = _make_query(_make_message())
    state = _make_state(
        {
            CART_STATE_KEY: _CART_WITH_ITEM,
            ORDER_DRAFT_KEY: {"address": "Lara Cad. 12", "contact_username": "ivan"},
        }
    )
    sheets = _make_sheets([_item(is_available=True)], merchants=[_merchant()])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis(order_id_seq=7)
    bot = _make_bot()

    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    _mock_dispatch.assert_awaited_once()
    call = _mock_dispatch.call_args
    assert call.args[0] is bot
    assert call.args[1] is sheets
    assert call.args[2] is redis
    assert call.args[3] is settings
    assert call.args[4] is scheduler
    assert call.kwargs["order_id"] == "007"
    assert call.kwargs["merchant_name"] == "MonAmi"
    assert call.kwargs["delivery_address"] == "Lara Cad. 12"


@pytest.mark.asyncio
async def test_order_confirmed_dispatch_failure_does_not_break_confirmation(
    valid_settings_kwargs, _mock_dispatch
):
    """
    Сбой диспетчеризации (например, Google Sheets временно недоступен)
    не должен помешать клиенту увидеть "Заказ принят" — заказ уже
    надёжно сохранён (append_order прошёл), диспетчеризация — отдельный
    шаг после этого, сбой только логируется.
    """
    _mock_dispatch.side_effect = Exception("dispatch boom")
    query = _make_query(_make_message(text="сводка заказа"))
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item(is_available=True)], merchants=[_merchant()])
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()

    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.message.edit_text.assert_awaited_once()  # заказ всё равно подтверждён


@pytest.mark.asyncio
async def test_order_confirmed_save_failure_shows_error_and_keeps_cart(valid_settings_kwargs):
    """
    КРИТИЧНО: если запись заказа в Sheets не удалась — пользователь НЕ
    должен услышать "Заказ принят", и корзина не должна очищаться (иначе
    заказ теряется бесследно, см. services/sheets.py SheetsWriteError).
    """
    query = _make_query(_make_message())
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item(is_available=True)], merchants=[_merchant()])
    sheets.append_order.side_effect = SheetsWriteError("boom")
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    bot = _make_bot()
    scheduler = _make_scheduler()
    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.message.answer.assert_awaited_once_with(ORDER_SAVE_FAILED_MESSAGE)
    query.message.edit_text.assert_not_called()
    state.update_data.assert_not_called()


# ------------------------------------------------------------------ #
# Назад к корзине
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_back_to_cart_shows_cart_summary():
    message = _make_message()
    query = _make_query(message)
    state = _make_state({CART_STATE_KEY: _CART_WITH_ITEM})
    sheets = _make_sheets([_item()])

    await on_back_to_cart(query, sheets, state)

    message.answer.assert_awaited_once()
    state.set_state.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_back_to_cart_empty_cart_shows_alert():
    query = _make_query(_make_message())
    state = _make_state()
    sheets = _make_sheets([])

    await on_back_to_cart(query, sheets, state)

    query.answer.assert_awaited_once_with(CART_EMPTY_ALERT, show_alert=True)


@pytest.mark.asyncio
async def test_order_again_shows_categories():
    message = _make_message()
    query = _make_query(message)
    sheets = _make_sheets([], merchants=[_merchant()])

    await on_order_again(query, sheets)

    message.answer.assert_awaited_once()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_order_confirmed_shows_fallback_alert_on_unexpected_exception(
    valid_settings_kwargs,
):
    """
    Живой фидбэк: реальный случай — почти одновременный повторный клик
    по «Подтвердить» наткнулся на кратковременный SSLError при
    перепроверке наличия товаров (sheets.get_items) и остался без
    ЛЮБОГО ответа пользователю. @resilient(...) — последний рубеж:
    непредвиденное исключение теперь превращается в понятный алерт
    вместо тишины.
    """
    query = _make_query(_make_message())
    state = _make_state(
        {CART_STATE_KEY: _CART_WITH_ITEM, ORDER_DRAFT_KEY: {"address": "Lara Cad. 12"}}
    )
    sheets = _make_sheets([_item(is_available=True)], merchants=[_merchant()])
    sheets.get_items.side_effect = OSError("Can't assign requested address")
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_order_confirmed(query, state, sheets, settings, redis, bot, scheduler)

    query.answer.assert_awaited_once_with(ORDER_SAVE_FAILED_MESSAGE, show_alert=True)
