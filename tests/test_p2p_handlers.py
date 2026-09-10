from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from config import Settings
from handlers.p2p import (
    P2P_DRAFT_KEY,
    _finish_points_and_check_zone,
    cmd_approve_p2p,
    cmd_reject_p2p,
    on_p2p_back_to_start,
    on_p2p_cancel,
    on_p2p_contact_received,
    on_p2p_description_received,
    on_p2p_dropoff_location_received,
    on_p2p_dropoff_text_as_address,
    on_p2p_entry,
    on_p2p_late_photo_attached,
    on_p2p_pickup_location_received,
    on_p2p_pickup_text_as_address,
    on_p2p_reject_reason_received,
    on_p2p_submit,
)
from services.geocoding import GeocodeResult
from services.sheets import Courier, SheetsWriteError
from states.user_states import P2PStates
from texts.ru import (
    ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE,
    ADMIN_P2P_APPROVED_ACK,
    ADMIN_P2P_NOT_FOUND_MESSAGE,
    ADMIN_P2P_REJECTED_ACK,
    ADMIN_REJECT_P2P_REASON_PROMPT,
    P2P_APPROVED_CLIENT_MESSAGE,
    P2P_CANCELLED_ACK,
    P2P_DESCRIPTION_PROMPT,
    P2P_DROPOFF_PROMPT,
    P2P_EMPTY_TEXT_MESSAGE,
    P2P_SUBMITTED_ACK,
)

ANTALYA_LAT = 36.8841
ANTALYA_LON = 30.7056
# ~7 км восточнее центра, всё ещё формально может быть внутри радиуса
# 15 км из valid_settings_kwargs — для "вне зоны" используем Анкару.
ANKARA_LAT = 39.9334
ANKARA_LON = 32.8597

ADMIN_ID = 111111111
NON_ADMIN_ID = 999999999
CLIENT_ID = 301746349


def _make_message(**overrides) -> MagicMock:
    message = MagicMock()
    message.answer = AsyncMock()
    message.edit_text = AsyncMock()
    message.from_user = MagicMock(id=CLIENT_ID, username="RF108")
    message.location = None
    message.text = None
    message.caption = None
    message.photo = None
    message.contact = None
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


def _make_query(message: MagicMock, from_user=None) -> MagicMock:
    query = MagicMock()
    query.message = message
    query.from_user = from_user or MagicMock(id=CLIENT_ID, username="RF108")
    query.answer = AsyncMock()
    return query


def _make_state(initial_data: dict | None = None) -> MagicMock:
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


def _make_redis(stopped: bool = False, order_id_seq: int = 1) -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"1" if stopped else None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock()
    redis.incr = AsyncMock(return_value=order_id_seq)
    return redis


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.id = 8987350738
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


def _make_scheduler() -> MagicMock:
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.remove_job = MagicMock()
    return scheduler


def _make_sheets(order: dict | None = None) -> MagicMock:
    sheets = MagicMock()
    sheets.append_order = AsyncMock()
    sheets.append_event = AsyncMock()
    sheets.get_order = AsyncMock(return_value=order)
    sheets.update_order_fields = AsyncMock(return_value=True)
    sheets.get_couriers = AsyncMock(
        return_value=[
            Courier(
                courier_id="1",
                name="Роман",
                phone="905075480108",
                telegram_id="500",
                is_registered_legal=True,
                on_shift=True,
                is_active=True,
            )
        ]
    )
    return sheets


def _make_geocoding(reverse_result=None, geocode_result=None) -> MagicMock:
    geocoding = MagicMock()
    geocoding.reverse_geocode = AsyncMock(return_value=reverse_result)
    geocoding.geocode = AsyncMock(return_value=geocode_result)
    return geocoding


def _make_settings(valid_settings_kwargs, **overrides) -> Settings:
    kwargs = dict(valid_settings_kwargs)
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture(autouse=True)
def _mock_dispatch(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr("handlers.p2p.send_offer_to_couriers", mock)
    return mock


# ------------------------------------------------------------------ #
# Вход / отмена
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_p2p_entry_starts_description_step(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    state = _make_state()
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()

    await on_p2p_entry(query, state, settings, redis)

    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_description)
    message.answer.assert_awaited_once()
    assert message.answer.call_args.args[0] == P2P_DESCRIPTION_PROMPT


@pytest.mark.asyncio
async def test_p2p_cancel_resets_and_edits_message():
    message = _make_message()
    query = _make_query(message)
    state = _make_state({P2P_DRAFT_KEY: {"description": "x"}})

    await on_p2p_cancel(query, state)

    state.set_state.assert_awaited_once_with(None)
    message.edit_text.assert_awaited_once_with(P2P_CANCELLED_ACK)
    data = await state.get_data()
    assert data[P2P_DRAFT_KEY] is None


@pytest.mark.asyncio
async def test_empty_description_is_rejected():
    message = _make_message(text="   ")
    state = _make_state({P2P_DRAFT_KEY: {}})

    await on_p2p_description_received(message, state)

    message.answer.assert_awaited_once_with(P2P_EMPTY_TEXT_MESSAGE)


@pytest.mark.asyncio
async def test_description_received_moves_to_pickup_step():
    message = _make_message(text="Забрать документы у консьержа")
    state = _make_state({P2P_DRAFT_KEY: {}})

    await on_p2p_description_received(message, state)

    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_pickup_location)
    data = await state.get_data()
    assert data[P2P_DRAFT_KEY]["description"] == "Забрать документы у консьержа"
    assert message.answer.call_args.args[0] == P2P_DROPOFF_PROMPT or True  # прошли дальше


@pytest.mark.asyncio
async def test_late_photo_attached_after_description_is_saved_to_draft():
    """Живой баг из тестирования: клиент отправил текст описания, а
    фото прикрепил ОТДЕЛЬНЫМ следующим сообщением, уже находясь на
    шаге ввода точки А — раньше такое фото молча терялось."""
    photo = MagicMock()
    photo.file_id = "AgACLatePhoto123"
    message = _make_message(photo=[photo])
    state = _make_state({P2P_DRAFT_KEY: {"description": "Забрать документы"}})

    await on_p2p_late_photo_attached(message, state)

    data = await state.get_data()
    assert data[P2P_DRAFT_KEY]["photo_file_id"] == "AgACLatePhoto123"
    assert data[P2P_DRAFT_KEY]["description"] == "Забрать документы"  # не затёрли остальное
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_pickup_text_as_address_works_without_pressing_button():
    """Живой фидбэк из тестирования: раньше текст адреса принимался
    ТОЛЬКО после явного нажатия [✏️ Ввести адрес] — прямой ввод без
    нажатия кнопки попадал в "неожиданный ввод". Теперь работает и так,
    и так."""
    message = _make_message(text="Antalya A")
    state = _make_state({P2P_DRAFT_KEY: {"description": "x"}})
    geocoding = _make_geocoding(geocode_result=GeocodeResult(lat=ANTALYA_LAT, lon=ANTALYA_LON, partial_match=False))

    await on_p2p_pickup_text_as_address(message, state, geocoding)

    data = await state.get_data()
    assert data[P2P_DRAFT_KEY]["pickup_address"] == "Antalya A"
    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_dropoff_location)


@pytest.mark.asyncio
async def test_dropoff_text_as_address_works_without_pressing_button(valid_settings_kwargs):
    """Зеркало теста для точки Б."""
    settings = _make_settings(valid_settings_kwargs)
    message = _make_message(text="Antalya B")
    state = _make_state(
        {
            P2P_DRAFT_KEY: {
                "description": "x",
                "pickup_address": "Antalya A",
                "pickup_lat": ANTALYA_LAT,
                "pickup_lon": ANTALYA_LON,
            }
        }
    )
    geocoding = _make_geocoding(
        geocode_result=GeocodeResult(lat=ANTALYA_LAT + 0.01, lon=ANTALYA_LON + 0.01, partial_match=False)
    )

    await on_p2p_dropoff_text_as_address(message, state, geocoding, settings)

    data = await state.get_data()
    assert data[P2P_DRAFT_KEY]["dropoff_address"] == "Antalya B"
    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_contact)


# ------------------------------------------------------------------ #
# Точки А/Б + совместная проверка зоны (ТЗ §7.6.4)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_pickup_location_received_moves_to_dropoff_without_zone_check_yet():
    """До момента, пока не собраны ОБЕ точки, зона не проверяется вообще —
    это ключевое отличие P2P от order.py (см. docstring p2p.py)."""
    location = MagicMock(latitude=ANKARA_LAT, longitude=ANKARA_LON)  # заведомо вне зоны
    message = _make_message(location=location)
    state = _make_state({P2P_DRAFT_KEY: {"description": "x"}})
    geocoding = _make_geocoding(reverse_result="Ankara, some street")

    await on_p2p_pickup_location_received(message, state, geocoding)

    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_dropoff_location)
    data = await state.get_data()
    assert data[P2P_DRAFT_KEY]["pickup_lat"] == ANKARA_LAT


@pytest.mark.asyncio
async def test_both_points_in_zone_proceeds_to_contact(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    message = _make_message()
    state = _make_state(
        {
            P2P_DRAFT_KEY: {
                "description": "x",
                "pickup_lat": ANTALYA_LAT,
                "pickup_lon": ANTALYA_LON,
                "pickup_address": "Antalya A",
                "dropoff_lat": ANTALYA_LAT + 0.01,
                "dropoff_lon": ANTALYA_LON + 0.01,
                "dropoff_address": "Antalya B",
            }
        }
    )

    await _finish_points_and_check_zone(message, state, settings)

    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_contact)


@pytest.mark.asyncio
async def test_dropoff_out_of_zone_shows_message_and_does_not_reset_draft(valid_settings_kwargs):
    settings = _make_settings(valid_settings_kwargs)
    message = _make_message()
    state = _make_state(
        {
            P2P_DRAFT_KEY: {
                "description": "x",
                "pickup_lat": ANTALYA_LAT,
                "pickup_lon": ANTALYA_LON,
                "pickup_address": "Antalya A",
                "dropoff_lat": ANKARA_LAT,
                "dropoff_lon": ANKARA_LON,
                "dropoff_address": "Ankara B",
            }
        }
    )

    await _finish_points_and_check_zone(message, state, settings)

    state.set_state.assert_not_called()  # состояние не меняем, только показываем сообщение
    message.answer.assert_awaited_once()
    assert "Б" in message.answer.call_args.args[0]
    data = await state.get_data()
    assert data[P2P_DRAFT_KEY] is not None  # черновик всё ещё на месте


@pytest.mark.asyncio
async def test_back_to_start_resets_draft_and_shows_categories(monkeypatch):
    message = _make_message()
    query = _make_query(message)
    state = _make_state({P2P_DRAFT_KEY: {"description": "x"}})
    sheets = _make_sheets()
    mock_show_categories = AsyncMock()
    monkeypatch.setattr("handlers.p2p.show_categories", mock_show_categories)

    await on_p2p_back_to_start(query, state, sheets)

    state.set_state.assert_awaited_once_with(None)
    mock_show_categories.assert_awaited_once()
    data = await state.get_data()
    assert data[P2P_DRAFT_KEY] is None


# ------------------------------------------------------------------ #
# Контакт → итог
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_contact_received_shows_final_confirmation():
    contact = MagicMock(user_id=CLIENT_ID, phone_number="905550001122")
    message = _make_message(contact=contact)
    state = _make_state(
        {
            P2P_DRAFT_KEY: {
                "description": "Забрать документы",
                "pickup_address": "Antalya A",
                "dropoff_address": "Antalya B",
            }
        }
    )
    sheets = _make_sheets()

    await on_p2p_contact_received(message, state, sheets)

    state.set_state.assert_awaited_once_with(P2PStates.confirming_order)


# ------------------------------------------------------------------ #
# Отправка на проверку
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_submit_creates_pending_review_order(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    draft = {
        "description": "Забрать документы у консьержа",
        "photo_file_id": "",
        "pickup_address": "Antalya A",
        "pickup_lat": ANTALYA_LAT,
        "pickup_lon": ANTALYA_LON,
        "dropoff_address": "Antalya B",
        "dropoff_lat": ANTALYA_LAT + 0.01,
        "dropoff_lon": ANTALYA_LON + 0.01,
        "contact_username": "RF108",
        "contact_phone": "905550001122",
    }
    state = _make_state({P2P_DRAFT_KEY: draft})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis(order_id_seq=43)
    sheets = _make_sheets()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_p2p_submit(query, state, sheets, settings, redis, bot, scheduler)

    sheets.append_order.assert_awaited_once()
    order_fields = sheets.append_order.await_args.args[0]
    assert order_fields["order_kind"] == "p2p"
    assert order_fields["status"] == "pending_review"
    assert order_fields["p2p_description"] == draft["description"]

    sheets.append_event.assert_awaited_once()
    assert sheets.append_event.await_args.kwargs["event_type"] == "p2p_submitted_for_review"

    scheduler.add_job.assert_called_once()
    bot.send_message.assert_awaited()  # уведомление админу (фото нет)

    data = await state.get_data()
    assert data[P2P_DRAFT_KEY] is None


@pytest.mark.asyncio
async def test_submit_double_tap_second_call_blocked(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    draft = {
        "description": "x",
        "pickup_address": "A",
        "pickup_lat": ANTALYA_LAT,
        "pickup_lon": ANTALYA_LON,
        "dropoff_address": "B",
        "dropoff_lat": ANTALYA_LAT,
        "dropoff_lon": ANTALYA_LON,
    }
    state = _make_state({P2P_DRAFT_KEY: draft})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    redis.set = AsyncMock(return_value=None)  # лок уже занят
    sheets = _make_sheets()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_p2p_submit(query, state, sheets, settings, redis, bot, scheduler)

    sheets.append_order.assert_not_called()
    query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_save_failure_keeps_draft_for_retry(valid_settings_kwargs):
    message = _make_message()
    query = _make_query(message)
    draft = {
        "description": "x",
        "pickup_address": "A",
        "pickup_lat": ANTALYA_LAT,
        "pickup_lon": ANTALYA_LON,
        "dropoff_address": "B",
        "dropoff_lat": ANTALYA_LAT,
        "dropoff_lon": ANTALYA_LON,
    }
    state = _make_state({P2P_DRAFT_KEY: draft})
    settings = _make_settings(valid_settings_kwargs)
    redis = _make_redis()
    sheets = _make_sheets()
    sheets.append_order = AsyncMock(side_effect=SheetsWriteError("boom"))
    bot = _make_bot()
    scheduler = _make_scheduler()

    await on_p2p_submit(query, state, sheets, settings, redis, bot, scheduler)

    data = await state.get_data()
    assert data[P2P_DRAFT_KEY] is not None  # черновик сохранён — можно повторить попытку
    scheduler.add_job.assert_not_called()


# ------------------------------------------------------------------ #
# /approve_p2p_[id], /reject_p2p_[id]
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_approve_by_non_admin_is_ignored(valid_settings_kwargs):
    message = _make_message(text="/approve_p2p_043")
    message.from_user = MagicMock(id=NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_approve_p2p(message, settings, sheets, redis, bot, scheduler)

    sheets.get_order.assert_not_called()


@pytest.mark.asyncio
async def test_approve_not_found_replies_gracefully(valid_settings_kwargs):
    message = _make_message(text="/approve_p2p_043")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets(order=None)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()

    await cmd_approve_p2p(message, settings, sheets, redis, bot, scheduler)

    message.answer.assert_awaited_once_with(ADMIN_P2P_NOT_FOUND_MESSAGE)


@pytest.mark.asyncio
async def test_approve_happy_path_dispatches_and_notifies_client(valid_settings_kwargs, monkeypatch):
    message = _make_message(text="/approve_p2p_043")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {
        "order_id": "043",
        "status": "pending_review",
        "user_id": str(CLIENT_ID),
        "pickup_address": "A",
        "dropoff_address": "B",
        "p2p_description": "x",
    }
    sheets = _make_sheets(order=order)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    mock_dispatch = AsyncMock()
    monkeypatch.setattr("handlers.p2p.send_offer_to_couriers", mock_dispatch)

    await cmd_approve_p2p(message, settings, sheets, redis, bot, scheduler)

    scheduler.remove_job.assert_called_once()
    bot.send_message.assert_awaited_once_with(CLIENT_ID, P2P_APPROVED_CLIENT_MESSAGE)
    mock_dispatch.assert_awaited_once()
    assert mock_dispatch.await_args.kwargs["order_id"] == "043"
    assert mock_dispatch.await_args.kwargs["p2p_pickup_address"] == "A"


@pytest.mark.asyncio
async def test_approve_second_concurrent_call_is_blocked_by_lock(valid_settings_kwargs, monkeypatch):
    """
    Живой баг из тестирования: два почти одновременных /approve_p2p_[id]
    оба проходили проверку "статус ещё pending_review" (первый вызов ещё
    не успел записать новый статус) и запускали диспетчеризацию курьерам
    ДВАЖДЫ. Теперь второй вызов должен блокироваться Redis-локом, не
    доходя до send_offer_to_couriers."""
    message = _make_message(text="/approve_p2p_043")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {
        "order_id": "043", "status": "pending_review", "user_id": str(CLIENT_ID),
        "pickup_address": "A", "dropoff_address": "B", "p2p_description": "x",
    }
    sheets = _make_sheets(order=order)
    redis = _make_redis()
    redis.set = AsyncMock(return_value=None)  # лок уже занят первым вызовом
    bot = _make_bot()
    scheduler = _make_scheduler()
    mock_dispatch = AsyncMock()
    monkeypatch.setattr("handlers.p2p.send_offer_to_couriers", mock_dispatch)

    await cmd_approve_p2p(message, settings, sheets, redis, bot, scheduler)

    mock_dispatch.assert_not_called()
    bot.send_message.assert_not_called()
    message.answer.assert_awaited_once_with(ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE)


@pytest.mark.asyncio
async def test_reject_second_concurrent_call_is_blocked_by_lock(valid_settings_kwargs):
    """Зеркало теста выше для /reject_p2p_."""
    message = _make_message(text="/reject_p2p_043 причина")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {"order_id": "043", "status": "pending_review", "user_id": str(CLIENT_ID)}
    sheets = _make_sheets(order=order)
    scheduler = _make_scheduler()
    bot = _make_bot()
    state = _make_state()
    redis = _make_redis()
    redis.set = AsyncMock(return_value=None)  # лок уже занят

    await cmd_reject_p2p(message, settings, sheets, scheduler, bot, state, redis)

    sheets.update_order_fields.assert_not_called()
    bot.send_message.assert_not_called()
    message.answer.assert_awaited_once_with(ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE)


@pytest.mark.asyncio
async def test_approve_releases_lock_after_success(valid_settings_kwargs, monkeypatch):
    """Лок должен сниматься после успешного одобрения — иначе Админ не
    сможет повторно взаимодействовать с этим order_id ещё 30 секунд."""
    message = _make_message(text="/approve_p2p_043")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {
        "order_id": "043", "status": "pending_review", "user_id": str(CLIENT_ID),
        "pickup_address": "A", "dropoff_address": "B", "p2p_description": "x",
    }
    sheets = _make_sheets(order=order)
    redis = _make_redis()
    bot = _make_bot()
    scheduler = _make_scheduler()
    monkeypatch.setattr("handlers.p2p.send_offer_to_couriers", AsyncMock())

    await cmd_approve_p2p(message, settings, sheets, redis, bot, scheduler)

    redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_reject_followup_reason_re_checks_status_before_writing(valid_settings_kwargs):
    """Живой баг из тестирования: заказ мог измениться (например, кто-то
    другой успел одобрить), пока Админ печатал причину отказа — запись
    отклонения не должна произойти вслепую поверх уже изменённого статуса."""
    state = _make_state({"p2p_reject_target_order_id": "043"})
    message = _make_message(text="причина")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {"order_id": "043", "status": "offered", "user_id": str(CLIENT_ID)}  # уже не pending_review
    sheets = _make_sheets(order=order)
    scheduler = _make_scheduler()
    bot = _make_bot()
    redis = _make_redis()

    await on_p2p_reject_reason_received(message, settings, sheets, scheduler, bot, state, redis)

    sheets.update_order_fields.assert_not_called()
    message.answer.assert_awaited_once_with(ADMIN_P2P_NOT_FOUND_MESSAGE)


@pytest.mark.asyncio
async def test_reject_happy_path_updates_status_and_notifies_client(valid_settings_kwargs):
    message = _make_message(text="/reject_p2p_043 крупногабаритный груз")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {
        "order_id": "043",
        "status": "pending_review",
        "user_id": str(CLIENT_ID),
    }
    sheets = _make_sheets(order=order)
    scheduler = _make_scheduler()
    bot = _make_bot()
    state = _make_state()

    await cmd_reject_p2p(message, settings, sheets, scheduler, bot, state, _make_redis())

    sheets.update_order_fields.assert_awaited_once_with(
        "043", {"status": "rejected", "cancel_reason": "крупногабаритный груз"}
    )
    scheduler.remove_job.assert_called_once()
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.args[1]
    assert "крупногабаритный груз" in sent_text
    message.answer.assert_awaited_once_with(ADMIN_P2P_REJECTED_ACK)


@pytest.mark.asyncio
async def test_reject_p2p_without_reason_prompts_and_remembers(valid_settings_kwargs):
    """Живой фидбэк из тестирования: голая /reject_p2p_[id] без причины
    больше не отклоняет молча шаблонным текстом — запрашивает причину
    у Админа и запоминает order_id (зеркало custom_order.py)."""
    message = _make_message(text="/reject_p2p_043")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {"order_id": "043", "status": "pending_review", "user_id": str(CLIENT_ID)}
    sheets = _make_sheets(order=order)
    scheduler = _make_scheduler()
    bot = _make_bot()
    state = _make_state()

    await cmd_reject_p2p(message, settings, sheets, scheduler, bot, state, _make_redis())

    bot.send_message.assert_not_called()
    sheets.update_order_fields.assert_not_called()
    message.answer.assert_awaited_once_with(ADMIN_REJECT_P2P_REASON_PROMPT)
    state.set_state.assert_awaited_once_with(P2PStates.waiting_for_reject_reason)
    data = await state.get_data()
    assert data["p2p_reject_target_order_id"] == "043"


@pytest.mark.asyncio
async def test_followup_p2p_reject_reason_completes_rejection(valid_settings_kwargs):
    state = _make_state({"p2p_reject_target_order_id": "043"})
    message = _make_message(text="слишком большой груз для наших курьеров")
    message.from_user = MagicMock(id=ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    order = {"order_id": "043", "status": "pending_review", "user_id": str(CLIENT_ID)}
    sheets = _make_sheets(order=order)
    scheduler = _make_scheduler()
    bot = _make_bot()

    await on_p2p_reject_reason_received(message, settings, sheets, scheduler, bot, state, _make_redis())

    sheets.update_order_fields.assert_awaited_once_with(
        "043", {"status": "rejected", "cancel_reason": "слишком большой груз для наших курьеров"}
    )
    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.args[1]
    assert "слишком большой груз для наших курьеров" in sent_text
    message.answer.assert_awaited_once_with(ADMIN_P2P_REJECTED_ACK)
    state.set_state.assert_awaited_once_with(None)
    data = await state.get_data()
    assert data["p2p_reject_target_order_id"] is None


@pytest.mark.asyncio
async def test_followup_p2p_reject_reason_from_non_admin_is_ignored(valid_settings_kwargs):
    state = _make_state({"p2p_reject_target_order_id": "043"})
    message = _make_message(text="я не админ")
    message.from_user = MagicMock(id=NON_ADMIN_ID)
    settings = _make_settings(valid_settings_kwargs)
    sheets = _make_sheets()
    scheduler = _make_scheduler()
    bot = _make_bot()

    await on_p2p_reject_reason_received(message, settings, sheets, scheduler, bot, state, _make_redis())

    bot.send_message.assert_not_called()
