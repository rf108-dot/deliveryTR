from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.sheets import (
    ORDERS_SHEET_COLUMNS,
    Courier,
    Merchant,
    MenuItem,
    SheetsClient,
    SheetsWriteError,
    _column_index_to_letter,
    _find_row_number,
    find_courier_by_telegram_id,
    get_available_categories,
    items_for_merchant,
    merchants_in_category,
)

MERCHANTS_HEADER = [
    "merchant_id",
    "category",
    "name",
    "description",
    "is_active",
    "today_confirmed",
    "working_hours",
]
MERCHANTS_ROWS = [
    MERCHANTS_HEADER,
    ["rest_001", "restaurant", "MonAmi", "Европейская кухня", "TRUE", "TRUE", "10:00-21:00"],
    ["rest_002", "restaurant", "Kebab House", "Турецкая кухня", "TRUE", "FALSE", "10:00-21:00"],
    ["shop_001", "grocery", "Migros", "Магазин", "FALSE", "TRUE", "09:00-22:00"],
    ["vet_001", "vet", "VetCare", "Ветклиника", "TRUE", "TRUE", "09:00-18:00"],
]

ITEMS_HEADER = [
    "item_id",
    "merchant_id",
    "name",
    "description",
    "price_try",
    "photo_url",
    "is_active",
    "is_available",
]
ITEMS_ROWS = [
    ITEMS_HEADER,
    ["item_001", "rest_001", "Борщ", "Свёкла, говядина", "180", "https://x/1.jpg", "TRUE", "TRUE"],
    ["item_002", "rest_001", "Плов", "Рис, баранина", "150", "https://x/2.jpg", "TRUE", "FALSE"],
    ["item_003", "rest_001", "Старое блюдо", "", "100", "https://x/3.jpg", "FALSE", "TRUE"],
    ["item_004", "rest_002", "Kebab", "Мясо, лаваш", "120", "https://x/4.jpg", "TRUE", "TRUE"],
]


def _make_mock_service(merchants_rows=None, items_rows=None):
    """Мок googleapiclient service: spreadsheets().values().get(...).execute()."""
    payloads = {
        "Мерчанты": {"values": merchants_rows if merchants_rows is not None else MERCHANTS_ROWS},
        "Позиции": {"values": items_rows if items_rows is not None else ITEMS_ROWS},
    }

    def get_side_effect(spreadsheetId, range):  # noqa: A002 — имя параметра как в API
        result = MagicMock()
        result.execute = MagicMock(return_value=payloads[range])
        return result

    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.side_effect = get_side_effect
    return service


@pytest.fixture
def sheets_client() -> SheetsClient:
    service = _make_mock_service()
    return SheetsClient(
        service=service,
        menu_spreadsheet_id="menu-id",
        orders_spreadsheet_id="orders-id",
        cache_ttl_seconds=300,
    )


# ------------------------------------------------------------------ #
# get_merchants / get_items — парсинг
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_merchants_parses_all_rows(sheets_client):
    merchants = await sheets_client.get_merchants()

    assert len(merchants) == 4
    assert merchants[0] == Merchant(
        merchant_id="rest_001",
        category="restaurant",
        name="MonAmi",
        description="Европейская кухня",
        is_active=True,
        today_confirmed=True,
        working_hours="10:00-21:00",
    )


@pytest.mark.asyncio
async def test_get_merchants_parses_boolean_flags_correctly(sheets_client):
    merchants = await sheets_client.get_merchants()
    by_id = {m.merchant_id: m for m in merchants}

    assert by_id["rest_002"].today_confirmed is False
    assert by_id["shop_001"].is_active is False


@pytest.mark.asyncio
async def test_get_items_parses_price_as_float(sheets_client):
    items = await sheets_client.get_items()
    by_id = {i.item_id: i for i in items}

    assert by_id["item_001"].price_try == 180.0
    assert isinstance(by_id["item_001"].price_try, float)


@pytest.mark.asyncio
async def test_get_merchants_skips_rows_without_merchant_id(sheets_client):
    rows = [MERCHANTS_HEADER, ["", "restaurant", "Безымянный", "", "TRUE", "TRUE", ""]]
    service = _make_mock_service(merchants_rows=rows)
    client = SheetsClient(service, "menu-id", "orders-id")

    merchants = await client.get_merchants()

    assert merchants == []


@pytest.mark.asyncio
async def test_get_items_tolerates_whitespace_in_headers_and_values():
    """
    Регресс-тест: реальный кейс из живого тестирования — при ручном вводе
    в Google Sheets в заголовок "price_try" и в значения ячеек могли
    попасть случайные пробелы ("price_try " как заголовок, "rest_001 "
    как значение) из-за копипаста инструкций с пробелами перед [Tab].
    Это ломало сопоставление по имени колонки и обрезание ID.
    """
    header_with_spaces = [
        "item_id",
        "merchant_id ",  # пробел в заголовке
        "name",
        "description",
        " price_try",  # пробел в заголовке
        "photo_url",
        "is_active",
        "is_available ",  # пробел в заголовке
    ]
    rows = [
        header_with_spaces,
        ["item_001", "rest_001 ", "Борщ ", "Свёкла", "180", "https://x/1.jpg  ", "TRUE", "TRUE"],
    ]
    service = _make_mock_service(items_rows=rows)
    client = SheetsClient(service, "menu-id", "orders-id")

    items = await client.get_items()

    assert len(items) == 1
    item = items[0]
    assert item.merchant_id == "rest_001"  # без пробела — иначе сломается matching
    assert item.name == "Борщ"
    assert item.price_try == 180.0  # раньше падало в 0.0 из-за " price_try"
    assert item.is_available is True  # раньше падало в False из-за "is_available "
    assert item.photo_url == "https://x/1.jpg"


# ------------------------------------------------------------------ #
# Кеш
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_merchants_uses_cache_on_second_call(sheets_client):
    await sheets_client.get_merchants()
    await sheets_client.get_merchants()

    get_mock = sheets_client._service.spreadsheets.return_value.values.return_value.get
    merchants_calls = [c for c in get_mock.call_args_list if c.kwargs.get("range") == "Мерчанты"]
    assert len(merchants_calls) == 1


@pytest.mark.asyncio
async def test_get_merchants_force_refresh_bypasses_cache(sheets_client):
    await sheets_client.get_merchants()
    await sheets_client.get_merchants(force_refresh=True)

    get_mock = sheets_client._service.spreadsheets.return_value.values.return_value.get
    merchants_calls = [c for c in get_mock.call_args_list if c.kwargs.get("range") == "Мерчанты"]
    assert len(merchants_calls) == 2


@pytest.mark.asyncio
async def test_get_merchants_refetches_after_ttl_expires(sheets_client):
    await sheets_client.get_merchants()
    # Симулируем истечение TTL, не дожидаясь реального времени.
    fetched_at, cached = sheets_client._merchants_cache
    sheets_client._merchants_cache = (fetched_at - 10_000, cached)

    await sheets_client.get_merchants()

    get_mock = sheets_client._service.spreadsheets.return_value.values.return_value.get
    merchants_calls = [c for c in get_mock.call_args_list if c.kwargs.get("range") == "Мерчанты"]
    assert len(merchants_calls) == 2


@pytest.mark.asyncio
async def test_items_cache_independent_from_merchants_cache(sheets_client):
    await sheets_client.get_merchants()
    await sheets_client.get_items()

    get_mock = sheets_client._service.spreadsheets.return_value.values.return_value.get
    assert get_mock.call_count == 2  # по одному вызову на каждый лист


# ------------------------------------------------------------------ #
# Чистые функции фильтрации
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_get_available_categories_only_visible_in_canonical_order(sheets_client):
    merchants = await sheets_client.get_merchants()

    codes = get_available_categories(merchants)

    # grocery скрыт (is_active=FALSE у единственного мерчанта), restaurant
    # и vet видны; порядок — канонический из ТЗ §4.1.
    assert codes == ["restaurant", "vet"]


@pytest.mark.asyncio
async def test_merchants_in_category_excludes_not_confirmed_today(sheets_client):
    merchants = await sheets_client.get_merchants()

    restaurants = merchants_in_category(merchants, "restaurant")

    assert [m.merchant_id for m in restaurants] == ["rest_001"]


@pytest.mark.asyncio
async def test_items_for_merchant_keeps_unavailable_but_excludes_inactive(sheets_client):
    items = await sheets_client.get_items()

    rest_001_items = items_for_merchant(items, "rest_001")

    ids = {i.item_id for i in rest_001_items}
    assert ids == {"item_001", "item_002"}  # item_003 исключён (is_active=FALSE)
    unavailable = next(i for i in rest_001_items if i.item_id == "item_002")
    assert unavailable.is_available is False  # показан, но помечен недоступным


def test_items_for_merchant_filters_by_merchant_id():
    items = [
        MenuItem("a", "m1", "A", "", 10, "", True, True),
        MenuItem("b", "m2", "B", "", 20, "", True, True),
    ]

    result = items_for_merchant(items, "m1")

    assert [i.item_id for i in result] == ["a"]


# ------------------------------------------------------------------ #
# health_check() — регресс-тест на баг с asyncio.gather (Day 1 hotfix)
# ------------------------------------------------------------------ #
#
# health_check() раньше проверял оба документа через asyncio.gather —
# оба вызова использовали ОДИН self._service на httplib2.Http, который не
# потокобезопасен для параллельных запросов через asyncio.to_thread. Это
# приводило к тому, что один запрос проходил, а второй зависал (реальный
# репорт: "Меню ОК, Orders в таймауте", хотя доступ был корректным).
# Фикс: проверки идут строго последовательно. Тесты ниже фиксируют это.


def _make_health_check_service(menu_ok: bool = True, orders_ok: bool = True) -> MagicMock:
    def get_side_effect(spreadsheetId, fields=None):  # noqa: A002 — имя как в API
        result = MagicMock()
        if spreadsheetId == "menu-id":
            if menu_ok:
                result.execute = MagicMock(return_value={"spreadsheetId": "menu-id"})
            else:
                result.execute = MagicMock(side_effect=Exception("menu boom"))
        else:
            if orders_ok:
                result.execute = MagicMock(return_value={"spreadsheetId": "orders-id"})
            else:
                result.execute = MagicMock(side_effect=Exception("orders boom"))
        return result

    service = MagicMock()
    service.spreadsheets.return_value.get.side_effect = get_side_effect
    return service


@pytest.mark.asyncio
async def test_health_check_passes_when_both_documents_accessible():
    service = _make_health_check_service(menu_ok=True, orders_ok=True)
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.health_check()  # не должно бросить исключение

    get_mock = service.spreadsheets.return_value.get
    assert get_mock.call_count == 2


@pytest.mark.asyncio
async def test_health_check_reports_orders_failure_after_checking_menu():
    from services.sheets import SheetsHealthCheckError

    service = _make_health_check_service(menu_ok=True, orders_ok=False)
    client = SheetsClient(service, "menu-id", "orders-id")

    with pytest.raises(SheetsHealthCheckError, match="Заказы"):
        await client.health_check()

    # Меню успело проверитьcя (не упало раньше времени), Orders — упал
    get_mock = service.spreadsheets.return_value.get
    assert get_mock.call_count == 2


@pytest.mark.asyncio
async def test_health_check_fails_fast_on_menu_without_calling_orders():
    """
    Регресс-тест: проверки последовательные, поэтому при сбое на «Меню»
    к «Заказы» мы даже не обращаемся (это и есть признак того, что нет
    asyncio.gather — при параллельном запуске оба вызова стартовали бы
    независимо от результата друг друга).
    """
    from services.sheets import SheetsHealthCheckError

    service = _make_health_check_service(menu_ok=False, orders_ok=True)
    client = SheetsClient(service, "menu-id", "orders-id")

    with pytest.raises(SheetsHealthCheckError, match="Меню"):
        await client.health_check()

    get_mock = service.spreadsheets.return_value.get
    assert get_mock.call_count == 1


# ------------------------------------------------------------------ #
# append_event() / append_order() — запись в лист «Заказы» (Day 4)
# ------------------------------------------------------------------ #


def _make_append_service(side_effect=None, return_value=None):
    """Мок googleapiclient service для spreadsheets().values().append(...)."""
    service = MagicMock()
    append_execute = MagicMock()
    if side_effect is not None:
        append_execute.side_effect = side_effect
    else:
        append_execute.return_value = return_value or {}
    service.spreadsheets.return_value.values.return_value.append.return_value.execute = (
        append_execute
    )
    return service


@pytest.mark.asyncio
async def test_append_event_writes_row_with_correct_columns():
    service = _make_append_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.append_event(
        event_type="bot_start", actor_role="client", actor_id="12345", order_id="", details=""
    )

    append_mock = service.spreadsheets.return_value.values.return_value.append
    append_mock.assert_called_once()
    call_kwargs = append_mock.call_args.kwargs
    assert call_kwargs["spreadsheetId"] == "orders-id"
    assert call_kwargs["range"] == "События"
    row = call_kwargs["body"]["values"][0]
    # [event_id, timestamp, actor_role, actor_id, order_id, event_type, details]
    assert row[2] == "client"
    assert row[3] == "12345"
    assert row[5] == "bot_start"


@pytest.mark.asyncio
async def test_append_event_does_not_raise_on_failure():
    """Best-effort: сбой аналитики НЕ должен ломать пользовательский флоу."""
    service = _make_append_service(side_effect=Exception("boom"))
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.append_event(event_type="bot_start", actor_role="client", actor_id="1")
    # если долетели сюда без исключения — тест пройден


@pytest.mark.asyncio
async def test_append_order_writes_row_in_correct_column_order():
    service = _make_append_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.append_order(
        {
            "order_id": "001",
            "order_kind": "standard",
            "merchant_id": "rest_001",
            "total_try": "330.0",
            "status": "new",
        }
    )

    append_mock = service.spreadsheets.return_value.values.return_value.append
    call_kwargs = append_mock.call_args.kwargs
    assert call_kwargs["spreadsheetId"] == "orders-id"
    assert call_kwargs["range"] == "Заказы"
    row = call_kwargs["body"]["values"][0]
    # Апостроф-префикс форсирует текстовый тип ячейки (см. ниже,
    # test_append_order_prefixes_order_id_with_apostrophe) — сравниваем
    # с этим учётом, не с голым "001".
    assert row[ORDERS_SHEET_COLUMNS.index("order_id")] == "'001"
    assert row[ORDERS_SHEET_COLUMNS.index("status")] == "new"


@pytest.mark.asyncio
async def test_append_order_prefixes_order_id_with_apostrophe():
    """
    Регресс-тест на живой баг: Google Sheets при valueInputOption=
    USER_ENTERED интерпретирует "011" как число 11, съедая ведущий
    ноль — дальнейший поиск заказа по order_id="011"
    (update_order_fields/get_order — все этапы курьера) молча не
    находил строку. Ведущий апостроф форсирует текстовый тип ячейки.
    """
    service = _make_append_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.append_order({"order_id": "011", "status": "new"})

    append_mock = service.spreadsheets.return_value.values.return_value.append
    row = append_mock.call_args.kwargs["body"]["values"][0]
    assert row[ORDERS_SHEET_COLUMNS.index("order_id")] == "'011"


@pytest.mark.asyncio
async def test_append_order_does_not_prefix_other_columns():
    service = _make_append_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.append_order({"order_id": "001", "merchant_id": "011", "status": "new"})

    append_mock = service.spreadsheets.return_value.values.return_value.append
    row = append_mock.call_args.kwargs["body"]["values"][0]
    # merchant_id не числовой ("rest_001" в реальных данных) — но даже
    # если бы он выглядел числом, апостроф добавляется ТОЛЬКО к
    # order_id (единственное поле, используемое как ключ поиска).
    assert row[ORDERS_SHEET_COLUMNS.index("merchant_id")] == "011"
    # Поля, не переданные явно (P2P/курьер) — должны быть пустой строкой.
    assert row[ORDERS_SHEET_COLUMNS.index("courier_id")] == ""
    assert row[ORDERS_SHEET_COLUMNS.index("p2p_description")] == ""


@pytest.mark.asyncio
async def test_append_order_raises_on_failure():
    """
    В отличие от append_event — сбой ЗАПИСИ ЗАКАЗА должен долетать до
    вызывающего кода (handlers/order.py), иначе пользователь услышит
    ложное "Заказ принят", хотя заказ никуда не сохранился.
    """
    service = _make_append_service(side_effect=Exception("boom"))
    client = SheetsClient(service, "menu-id", "orders-id")

    with pytest.raises(SheetsWriteError):
        await client.append_order({"order_id": "001"})


# ------------------------------------------------------------------ #
# Day 5: чистые функции _find_row_number / _column_index_to_letter
# ------------------------------------------------------------------ #


def test_column_index_to_letter_single_letters():
    assert _column_index_to_letter(0) == "A"
    assert _column_index_to_letter(25) == "Z"


def test_column_index_to_letter_double_letters():
    assert _column_index_to_letter(26) == "AA"
    assert _column_index_to_letter(27) == "AB"
    # admin_notes — 35-я (0-indexed 34) колонка ORDERS_SHEET_COLUMNS,
    # живой фидбэк подтвердил именно "AI" при ручной проверке в Sheets.
    assert _column_index_to_letter(34) == "AI"


def test_find_row_number_locates_matching_row():
    values = [
        ["order_id", "status"],
        ["001", "new"],
        ["002", "assigned"],
    ]

    result = _find_row_number(values, "order_id", "002")

    assert result is not None
    header, row_number = result
    assert header == ["order_id", "status"]
    assert row_number == 3  # 1-indexed, с учётом строки заголовка


def test_find_row_number_returns_none_when_not_found():
    values = [["order_id", "status"], ["001", "new"]]

    assert _find_row_number(values, "order_id", "999") is None


def test_find_row_number_returns_none_for_empty_values():
    assert _find_row_number([], "order_id", "001") is None


def test_find_row_number_returns_none_when_key_column_missing():
    values = [["a", "b"], ["1", "2"]]

    assert _find_row_number(values, "order_id", "1") is None


def test_find_row_number_strips_whitespace_in_cells():
    values = [["order_id"], [" 001 "]]

    result = _find_row_number(values, "order_id", "001")

    assert result is not None


# ------------------------------------------------------------------ #
# Day 5: get_couriers() — лист «Курьеры» живёт в документе «Заказы»
# ------------------------------------------------------------------ #

COURIERS_HEADER = [
    "courier_id",
    "name",
    "phone",
    "telegram_id",
    "is_registered_legal",
    "on_shift",
    "is_active",
]
COURIERS_ROWS = [
    COURIERS_HEADER,
    ["cur_001", "Ahmet Y.", "+90 5xx1", "111111111", "TRUE", "TRUE", "TRUE"],
    ["cur_002", "Boris K.", "+90 5xx2", "222222222", "TRUE", "FALSE", "TRUE"],
    ["cur_003", "Ушедший", "+90 5xx3", "333333333", "TRUE", "TRUE", "FALSE"],
]


def _make_couriers_service(rows=None):
    payloads = {("menu-id", "Мерчанты"): {"values": MERCHANTS_ROWS}}
    couriers_payload = {"values": rows if rows is not None else COURIERS_ROWS}

    def get_side_effect(spreadsheetId, range):  # noqa: A002
        result = MagicMock()
        if spreadsheetId == "orders-id" and range == "Курьеры":
            result.execute = MagicMock(return_value=couriers_payload)
        else:
            result.execute = MagicMock(return_value=payloads.get((spreadsheetId, range), {"values": []}))
        return result

    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.side_effect = get_side_effect
    return service


@pytest.mark.asyncio
async def test_get_couriers_reads_from_orders_spreadsheet_not_menu():
    """
    Регресс-тест на архитектурный нюанс ТЗ §4.2: лист «Курьеры» лежит в
    документе «Заказы», НЕ в «Меню» (в отличие от Мерчантов/Позиций).
    """
    service = _make_couriers_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    await client.get_couriers()

    get_mock = service.spreadsheets.return_value.values.return_value.get
    call = next(c for c in get_mock.call_args_list if c.kwargs.get("range") == "Курьеры")
    assert call.kwargs["spreadsheetId"] == "orders-id"


@pytest.mark.asyncio
async def test_get_couriers_parses_rows_correctly():
    service = _make_couriers_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    couriers = await client.get_couriers()

    assert len(couriers) == 3
    assert couriers[0] == Courier(
        courier_id="cur_001",
        name="Ahmet Y.",
        phone="+90 5xx1",
        telegram_id="111111111",
        is_registered_legal=True,
        on_shift=True,
        is_active=True,
    )


@pytest.mark.asyncio
async def test_get_couriers_available_for_dispatch_excludes_off_shift_and_inactive():
    service = _make_couriers_service()
    client = SheetsClient(service, "menu-id", "orders-id")

    couriers = await client.get_couriers()
    available = [c for c in couriers if c.is_available_for_dispatch]

    assert [c.courier_id for c in available] == ["cur_001"]


def test_find_courier_by_telegram_id_found():
    couriers = [
        Courier("cur_001", "A", "+1", "111", True, True, True),
        Courier("cur_002", "B", "+2", "222", True, False, True),
    ]

    result = find_courier_by_telegram_id(couriers, "222")

    assert result is not None
    assert result.courier_id == "cur_002"


def test_find_courier_by_telegram_id_not_found():
    couriers = [Courier("cur_001", "A", "+1", "111", True, True, True)]

    assert find_courier_by_telegram_id(couriers, "999") is None


# ------------------------------------------------------------------ #
# Day 5: update_courier_on_shift() / update_order_fields() / get_order()
# ------------------------------------------------------------------ #


def _make_batch_update_service(get_side_effect_fn, batch_update_side_effect=None):
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.side_effect = get_side_effect_fn

    batch_execute = MagicMock()
    if batch_update_side_effect is not None:
        batch_execute.side_effect = batch_update_side_effect
    else:
        batch_execute.return_value = {}
    service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute = (
        batch_execute
    )
    return service


def _couriers_get_side_effect(spreadsheetId, range):  # noqa: A002
    result = MagicMock()
    if spreadsheetId == "orders-id" and range == "Курьеры":
        result.execute = MagicMock(return_value={"values": COURIERS_ROWS})
    else:
        result.execute = MagicMock(return_value={"values": []})
    return result


@pytest.mark.asyncio
async def test_update_courier_on_shift_writes_correct_cell():
    service = _make_batch_update_service(_couriers_get_side_effect)
    client = SheetsClient(service, "menu-id", "orders-id")

    result = await client.update_courier_on_shift("222222222", True)

    assert result is True
    batch_update_mock = service.spreadsheets.return_value.values.return_value.batchUpdate
    call_kwargs = batch_update_mock.call_args.kwargs
    assert call_kwargs["spreadsheetId"] == "orders-id"
    data = call_kwargs["body"]["data"]
    # cur_002 — вторая строка данных -> строка 3 (с учётом заголовка);
    # on_shift — 6-я колонка (0-indexed 5) -> "F".
    assert data == [{"range": "Курьеры!F3", "values": [["TRUE"]]}]


@pytest.mark.asyncio
async def test_update_courier_on_shift_returns_false_when_not_found():
    service = _make_batch_update_service(_couriers_get_side_effect)
    client = SheetsClient(service, "menu-id", "orders-id")

    result = await client.update_courier_on_shift("000000000", True)

    assert result is False
    service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()


ORDERS_ROWS_SAMPLE = [
    list(ORDERS_SHEET_COLUMNS),
    ["001"] + [""] * (len(ORDERS_SHEET_COLUMNS) - 1),
    ["002"] + [""] * (len(ORDERS_SHEET_COLUMNS) - 1),
]


def _orders_get_side_effect(spreadsheetId, range):  # noqa: A002
    result = MagicMock()
    if spreadsheetId == "orders-id" and range == "Заказы":
        result.execute = MagicMock(return_value={"values": ORDERS_ROWS_SAMPLE})
    else:
        result.execute = MagicMock(return_value={"values": []})
    return result


@pytest.mark.asyncio
async def test_update_order_fields_writes_multiple_cells_in_one_batch():
    service = _make_batch_update_service(_orders_get_side_effect)
    client = SheetsClient(service, "menu-id", "orders-id")

    result = await client.update_order_fields(
        "002", {"status": "assigned", "courier_name": "Ahmet Y."}
    )

    assert result is True
    batch_update_mock = service.spreadsheets.return_value.values.return_value.batchUpdate
    assert batch_update_mock.call_count == 1  # один HTTP-вызов на обе ячейки
    data = batch_update_mock.call_args.kwargs["body"]["data"]
    assert len(data) == 2
    ranges = {d["range"] for d in data}
    status_col = _column_index_to_letter(ORDERS_SHEET_COLUMNS.index("status"))
    name_col = _column_index_to_letter(ORDERS_SHEET_COLUMNS.index("courier_name"))
    assert f"Заказы!{status_col}3" in ranges  # order_id="002" -> строка 3
    assert f"Заказы!{name_col}3" in ranges


@pytest.mark.asyncio
async def test_update_order_fields_ignores_unknown_column_names():
    service = _make_batch_update_service(_orders_get_side_effect)
    client = SheetsClient(service, "menu-id", "orders-id")

    result = await client.update_order_fields("001", {"not_a_real_column": "x"})

    assert result is True  # заказ найден, просто нечего обновлять
    service.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()


@pytest.mark.asyncio
async def test_update_order_fields_returns_false_when_order_not_found():
    service = _make_batch_update_service(_orders_get_side_effect)
    client = SheetsClient(service, "menu-id", "orders-id")

    result = await client.update_order_fields("999", {"status": "new"})

    assert result is False


@pytest.mark.asyncio
async def test_get_order_returns_full_row_as_dict():
    rows = [
        list(ORDERS_SHEET_COLUMNS),
        ["001", "", "standard", "555", "", "", "rest_001", "MonAmi"] + [""] * (len(ORDERS_SHEET_COLUMNS) - 8),
    ]

    def get_side_effect(spreadsheetId, range):  # noqa: A002
        result = MagicMock()
        if spreadsheetId == "orders-id" and range == "Заказы":
            result.execute = MagicMock(return_value={"values": rows})
        else:
            result.execute = MagicMock(return_value={"values": []})
        return result

    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.side_effect = get_side_effect
    client = SheetsClient(service, "menu-id", "orders-id")

    order = await client.get_order("001")

    assert order is not None
    assert order["user_id"] == "555"
    assert order["merchant_name"] == "MonAmi"


@pytest.mark.asyncio
async def test_get_order_returns_none_when_not_found():
    service = _make_batch_update_service(_orders_get_side_effect)
    client = SheetsClient(service, "menu-id", "orders-id")

    order = await client.get_order("does-not-exist")

    assert order is None


# ------------------------------------------------------------------ #
# Day 5 hotfix: защита кеша от подозрительного опустения (живой баг —
# кратковременный сетевой сбой вернул пустой список вместо ошибки,
# бот закешировал "меню пусто" для всех мерчантов на весь TTL)
# ------------------------------------------------------------------ #


def _make_sequential_service(sequence: list[dict]):
    """Мок, отдающий разные ответы на последовательные вызовы .execute()
    — не важно, к какому листу/документу обращение, всегда следующий
    элемент sequence."""
    call_count = {"n": 0}

    def get_side_effect(spreadsheetId, range):  # noqa: A002
        result = MagicMock()
        idx = min(call_count["n"], len(sequence) - 1)
        call_count["n"] += 1
        result.execute = MagicMock(return_value=sequence[idx])
        return result

    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.side_effect = get_side_effect
    return service


@pytest.mark.asyncio
async def test_get_merchants_ignores_suspicious_empty_refresh():
    service = _make_sequential_service(
        [{"values": MERCHANTS_ROWS}, {"values": []}]
    )
    client = SheetsClient(service, "menu-id", "orders-id")

    first = await client.get_merchants()
    assert len(first) > 0

    second = await client.get_merchants(force_refresh=True)

    assert second == first  # не затёрлось пустым — отдан прошлый хороший кеш


@pytest.mark.asyncio
async def test_get_merchants_accepts_empty_when_cache_was_never_populated():
    """Если ДО этого кеша вообще не было (самый первый вызов) — пустой
    результат принимается как есть, это не "подозрительное опустение",
    а возможно реально пустой лист на старте."""
    service = _make_sequential_service([{"values": []}])
    client = SheetsClient(service, "menu-id", "orders-id")

    result = await client.get_merchants()

    assert result == []


@pytest.mark.asyncio
async def test_get_items_ignores_suspicious_empty_refresh():
    service = _make_sequential_service([{"values": ITEMS_ROWS}, {"values": []}])
    client = SheetsClient(service, "menu-id", "orders-id")

    first = await client.get_items()
    assert len(first) > 0

    second = await client.get_items(force_refresh=True)

    assert second == first


@pytest.mark.asyncio
async def test_get_couriers_ignores_suspicious_empty_refresh():
    service = _make_sequential_service([{"values": COURIERS_ROWS}, {"values": []}])
    client = SheetsClient(service, "menu-id", "orders-id")

    first = await client.get_couriers()
    assert len(first) > 0

    second = await client.get_couriers(force_refresh=True)

    assert second == first
