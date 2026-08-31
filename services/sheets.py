"""
Адаптер Google Sheets API.

Day 0: инициализация клиента, health-check доступности двух документов.
Day 1: чтение листов «Мерчанты» / «Позиции» (документ «Меню», read-only)
с кешированием на CACHE_TTL_SECONDS, плюс чистые функции фильтрации по
категориям/мерчанту (ТЗ §4.1, §7.1-7.3.1).
Day 4: запись в документ «Заказы» — append_event() (лист «События»,
best-effort) и append_order() (лист «Заказы», ошибка пробрасывается —
см. ТЗ §4.2).
Day 5 (ТЗ §4.2, §8): лист «Курьеры» живёт в ТОМ ЖЕ документе «Заказы»
(не в «Меню» — важно, т.к. курьеров и читаем, и пишем, в отличие от
read-only мерчантов/позиций). get_couriers(), update_courier_on_shift(),
update_order_fields() (точечное обновление ячеек — переходы статуса
заказа), get_order() (чтение одной строки по order_id, без кеша —
нужны самые свежие данные для уведомлений).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httplib2
import google_auth_httplib2
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Ретрай на транзиентные сетевые сбои (живой баг из тестирования: частые
# ssl.SSLError("record layer failure") / ConnectionResetError на пути к
# Google API — см. _execute_with_retry). Оба класса ошибок — подклассы
# OSError, поэтому один except OSError покрывает и обрыв SSL, и сброс
# TCP-соединения. НЕ ретраим asyncio.TimeoutError (это уже полные
# self._http_timeout_seconds ожидания — повтор удвоил бы и без того
# долгое ожидание) и НЕ ретраим HttpError от googleapiclient (это уже
# полученный от API ответ с конкретным кодом, например 403/404 — не
# временная проблема сети, а нечто, что не исчезнет от повторной попытки).
_NETWORK_RETRY_ATTEMPTS = 2
_NETWORK_RETRY_DELAY_SECONDS = 0.5

# Канонический порядок категорий, см. ТЗ §4.1 и мокап клавиатуры в §7.1.
# Порядок важен: именно в нём категории показываются в клавиатуре /start.
CATEGORY_ORDER: tuple[str, ...] = (
    "restaurant",
    "grocery",
    "pharmacy",
    "vet",
    "water",
    "hardware",
)

# Порядок колонок листа «Заказы» (ТЗ §4.2) — используется в append_order().
# order_kind="standard" (Day 3-4): p2p_*/pickup_*/dropoff_*/courier_*
# остаются пустыми до Day 5 (курьеры) и Day 7 (P2P).
ORDERS_SHEET_COLUMNS: tuple[str, ...] = (
    "order_id",
    "timestamp_created",
    "order_kind",
    "user_id",
    "username",
    "phone",
    "merchant_id",
    "merchant_name",
    "items_json",
    "total_try",
    "courier_reward_suggested",
    "delivery_address",
    "geo_lat",
    "geo_lon",
    "address_manually_edited",
    "is_custom_order",
    "custom_description",
    "custom_photo_file_id",
    "p2p_description",
    "p2p_photo_file_id",
    "pickup_address",
    "pickup_lat",
    "pickup_lon",
    "dropoff_address",
    "dropoff_lat",
    "dropoff_lon",
    "courier_id",
    "courier_name",
    "courier_phone",
    "status",
    "timestamp_offered",
    "timestamp_assigned",
    "timestamp_picked_up",
    "timestamp_delivered",
    "timestamp_cancelled",
    "cancel_reason",
    "admin_notes",
)

_TRUE_VALUES = {"true", "1", "yes", "да"}


class SheetsHealthCheckError(RuntimeError):
    """Google Sheets недоступны или сервисный аккаунт не имеет доступа."""


class SheetsWriteError(RuntimeError):
    """Не удалось записать данные в Google Sheets (append_order/append_event)."""


# ---------------------------------------------------------------------- #
# Домен: строки листов «Мерчанты» и «Позиции» (ТЗ §4.1)
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class Merchant:
    merchant_id: str
    category: str
    name: str
    description: str
    is_active: bool
    today_confirmed: bool
    working_hours: str

    @property
    def is_visible(self) -> bool:
        """Мерчант показывается пользователю только если оба флага TRUE."""
        return self.is_active and self.today_confirmed


@dataclass(frozen=True)
class MenuItem:
    item_id: str
    merchant_id: str
    name: str
    description: str
    price_try: float
    photo_url: str
    is_active: bool
    is_available: bool


@dataclass(frozen=True)
class Courier:
    """Строка листа «Курьеры» (документ «Заказы», ТЗ §4.2, §8.1)."""

    courier_id: str
    name: str
    phone: str
    telegram_id: str
    is_registered_legal: bool
    on_shift: bool
    is_active: bool

    @property
    def is_available_for_dispatch(self) -> bool:
        """is_registered_legal сознательно НЕ участвует в этой проверке —
        судя по ТЗ, это информационный/комплаенс-флаг для админа, а не
        флаг, управляющий поведением бота (в отличие от on_shift/is_active,
        у которых в ТЗ явно описано поведение)."""
        return self.is_active and self.on_shift


def find_courier_by_telegram_id(couriers: list[Courier], telegram_id: str) -> Courier | None:
    """
    Чистая функция без I/O — используется и в handlers/start.py (роутинг
    /start по роли — ТЗ §2: "роль определяется по ID"), и в
    handlers/courier.py (проверка регистрации на /shift_on и т.д.).
    """
    return next((c for c in couriers if c.telegram_id == telegram_id), None)


def _s(value: str) -> str:
    """Строковое значение из ячейки, без пробелов по краям (защита от ручного ввода)."""
    return str(value).strip()


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in _TRUE_VALUES


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except (ValueError, AttributeError):
        return default


def _rows_to_dicts(rows: list[list[str]]) -> list[dict[str, str]]:
    """
    Первая строка — заголовок; сопоставляет остальные строки по имени
    колонки. Имена колонок нормализуются (strip) — реальные таблицы часто
    получают случайные пробелы при ручном вводе/копипасте, и без этого
    колонка "price_try " (с пробелом) не совпадёт с ожидаемым "price_try".
    """
    if not rows:
        return []
    header, *data_rows = rows
    header = [h.strip() for h in header]
    result = []
    for row in data_rows:
        padded = row + [""] * (len(header) - len(row))
        result.append(dict(zip(header, padded)))
    return result


def _find_row_number(
    values: list[list[str]], key_column: str, key_value: str
) -> tuple[list[str], int] | None:
    """
    Ищет первую строку данных, где значение колонки key_column равно
    key_value. Возвращает (нормализованный заголовок, номер строки —
    1-indexed, как в A1-нотации Google Sheets, с учётом строки заголовка)
    либо None, если не найдено. Чистая функция без I/O — используется в
    update_courier_on_shift()/update_order_fields()/get_order() перед
    точечной записью/чтением конкретной ячейки (Day 5).
    """
    if not values:
        return None
    header = [h.strip() for h in values[0]]
    if key_column not in header:
        return None
    key_idx = header.index(key_column)
    for row_idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(header) - len(row))
        if padded[key_idx].strip() == key_value:
            return header, row_idx
    return None


def _column_index_to_letter(index: int) -> str:
    """0 → 'A', 25 → 'Z', 26 → 'AA' ... (0-indexed колонка → буква A1-нотации)."""
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


# ---------------------------------------------------------------------- #
# Чистые функции фильтрации (без I/O — легко покрываются юнит-тестами)
# ---------------------------------------------------------------------- #


def get_available_categories(merchants: list[Merchant]) -> list[str]:
    """
    Категории, где есть хотя бы один видимый мерчант (is_active И
    today_confirmed), в каноническом порядке ТЗ §4.1 / §7.1.
    """
    present = {m.category for m in merchants if m.is_visible}
    return [code for code in CATEGORY_ORDER if code in present]


def merchants_in_category(merchants: list[Merchant], category: str) -> list[Merchant]:
    """Видимые мерчанты выбранной категории (ТЗ §7.2)."""
    return [m for m in merchants if m.category == category and m.is_visible]


def items_for_merchant(items: list[MenuItem], merchant_id: str) -> list[MenuItem]:
    """
    Активные позиции меню мерчанта. is_available НЕ фильтрует — недоступные
    позиции по-прежнему показываются, но помечаются в UI (ТЗ §7.3.1).
    """
    return [i for i in items if i.merchant_id == merchant_id and i.is_active]


# ---------------------------------------------------------------------- #
# Клиент Google Sheets
# ---------------------------------------------------------------------- #


class SheetsClient:
    """
    Тонкая обёртка над Google Sheets API v4 для документов ТЗ §4.1/§4.2.

    Конструктор принимает уже собранный `service` (googleapiclient Resource)
    напрямую — это специально сделано для тестируемости (в тестах можно
    подставить Mock вместо реального Google API клиента). Для реальной
    инициализации из service account JSON используйте
    `SheetsClient.from_service_account_info(...)`.
    """

    def __init__(
        self,
        service: Any,
        menu_spreadsheet_id: str,
        orders_spreadsheet_id: str,
        cache_ttl_seconds: int = 300,
        http_timeout_seconds: float = 15.0,
        credentials: Credentials | None = None,
    ) -> None:
        self._service = service
        self._menu_spreadsheet_id = menu_spreadsheet_id
        self._orders_spreadsheet_id = orders_spreadsheet_id
        self._cache_ttl_seconds = cache_ttl_seconds
        # Жёсткий потолок на уровне asyncio.wait_for — не полагаемся на
        # таймаут самого HTTP-транспорта (в одном из прогонов он не сработал
        # вовремя на практике, см. CHECKLIST.md, Day 1 hotfix #2).
        self._http_timeout_seconds = http_timeout_seconds

        self._merchants_cache: tuple[float, list[Merchant]] | None = None
        self._items_cache: tuple[float, list[MenuItem]] | None = None
        self._merchants_lock = asyncio.Lock()
        self._items_lock = asyncio.Lock()
        # Day 5: отдельный кеш для курьеров — важно вызывать get_couriers()
        # с force_refresh=True из диспетчеризации (см. services/dispatch.py),
        # иначе курьер, только что нажавший /shift_on, не попадёт в
        # рассылку следующие CACHE_TTL_SECONDS.
        self._couriers_cache: tuple[float, list[Courier]] | None = None
        self._couriers_lock = asyncio.Lock()

        # ЖИВОЙ БАГ №1 (сегфолт процесса): self._service изначально построен
        # на ОДНОМ общем httplib2.Http, который не потокобезопасен для
        # одновременных запросов из разных потоков asyncio.to_thread —
        # конкурентные чтение/запись на общем сокете при сетевом сбое
        # роняли интерпретатор целиком (не ловимое исключение).
        #
        # ЖИВОЙ БАГ №2 (обнаружен после первого фикса): "решение в лоб" —
        # один общий asyncio.Lock на ВСЕ сетевые вызовы — устранило крэш,
        # но при нескольких параллельных операциях (курьер жмёт «Взял» в
        # то же время, что клиент жмёт «Подтвердить», плюс периодический
        # health-check) все они выстраивались в одну очередь по 15 сек
        # таймаута каждая — обработка одного апдейта растягивалась на
        # 60+ секунд и ложно срабатывал offer_timeout.
        #
        # ПРАВИЛЬНОЕ РЕШЕНИЕ: не сериализовать вызовы, а убрать само
        # разделяемое состояние. httplib2.Http нельзя использовать из
        # нескольких потоков ОДНОВРЕМЕННО, но ничто не мешает каждому
        # вызову строить СВОЙ ОДНОРАЗОВЫЙ Http-транспорт — тогда потокам
        # нечего делить, и они спокойно выполняются параллельно, как и
        # было задумано изначально. credentials из google-auth сами по
        # себе безопасны для параллельного использования (внутренний лок
        # на обновление токена уже есть в самой библиотеке) — делить
        # можно смело, в отличие от httplib2.Http.
        #
        # credentials может быть None в тестах, где service — это Mock:
        # там строить реальный HTTP не из чего и не нужно (Mock не делает
        # настоящих сетевых вызовов), поэтому в этом случае используем
        # старый self._service как есть — тестам это не мешает.
        self._credentials = credentials

        # ОТДЕЛЬНАЯ причина для лока, не связанная с потокобезопасностью
        # httplib2 (та решена через _fresh_http() выше): несколько
        # конкурентных append() к ОДНОМУ листу могут «наступить друг другу
        # на пятки» уже на уровне самого Google Sheets API — сервис сам
        # решает, в какую строку вставлять на основе текущего состояния
        # листа на момент запроса, и два одновременных append не гарантируют
        # разные строки (живой баг из тестирования, замечен ещё до истории
        # с сегфолтом). Поэтому append остаётся строго последовательным,
        # а обычные чтения (get_items/get_couriers/get_order) и точечные
        # batchUpdate — нет, им сериализация не нужна ни для потоко-
        # безопасности (см. _fresh_http), ни для корректности данных.
        self._append_lock = asyncio.Lock()

    def _fresh_http(self) -> Any | None:
        """Изолированный HTTP-транспорт на один вызов — см. комментарий
        в __init__ про причину отказа от общего self._service_lock.
        Возвращает None, если credentials не переданы (тестовый Mock-путь) —
        в этом случае вызывающий код должен использовать .execute() без
        аргумента http, как раньше."""
        if self._credentials is None:
            return None
        return google_auth_httplib2.AuthorizedHttp(
            self._credentials, http=httplib2.Http(timeout=self._http_timeout_seconds)
        )

    async def _execute_with_retry(
        self,
        request: Any,
        *,
        error_cls: type[Exception],
        timeout_message: str,
        error_message: Callable[[str], str],
    ) -> Any:
        """Единая точка выполнения запроса к Google Sheets API с ретраем
        на транзиентные сетевые сбои (живой баг из тестирования: частые
        ssl.SSLError("record layer failure") / ConnectionResetError на
        нестабильной сети — см. константы _NETWORK_RETRY_* в начале файла).

        request — уже построенный googleapiclient HttpRequest (safe для
        повторного .execute() — сам объект не мутируется при выполнении).
        error_cls — SheetsHealthCheckError либо SheetsWriteError, в
        зависимости от вызывающего кода. timeout_message/error_message —
        готовые тексты ошибок (разные у каждого вызывающего метода).

        НЕ ретраит asyncio.TimeoutError (уже полное ожидание
        self._http_timeout_seconds — повтор удвоил бы задержку) и НЕ
        ретраит HttpError/прочие исключения от самого Google API (ответ
        с конкретным кодом — не временная сетевая проблема).
        """
        last_exc: OSError | None = None
        for attempt in range(1, _NETWORK_RETRY_ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(request.execute, http=self._fresh_http()),
                    timeout=self._http_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise error_cls(timeout_message) from exc
            except OSError as exc:
                last_exc = exc
                if attempt < _NETWORK_RETRY_ATTEMPTS:
                    logger.warning(
                        "Транзиентная сетевая ошибка (попытка %d/%d), "
                        "повтор через %.1fс: %s",
                        attempt,
                        _NETWORK_RETRY_ATTEMPTS,
                        _NETWORK_RETRY_DELAY_SECONDS,
                        exc,
                    )
                    await asyncio.sleep(_NETWORK_RETRY_DELAY_SECONDS)
                    continue
                raise error_cls(error_message(str(exc))) from exc
            except Exception as exc:  # noqa: BLE001 — прочие ошибки не ретраим
                raise error_cls(error_message(str(exc))) from exc
        # Недостижимо (цикл либо возвращает, либо кидает исключение выше),
        # но нужно для статических анализаторов типов.
        raise error_cls(error_message(str(last_exc)))

    @classmethod
    def from_service_account_info(
        cls,
        service_account_info: dict[str, Any],
        menu_spreadsheet_id: str,
        orders_spreadsheet_id: str,
        cache_ttl_seconds: int = 300,
        http_timeout_seconds: float = 15.0,
    ) -> "SheetsClient":
        """Фабрика для реального использования (см. wiring в bot.py)."""
        credentials = Credentials.from_service_account_info(
            service_account_info, scopes=_SCOPES
        )
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return cls(
            service,
            menu_spreadsheet_id,
            orders_spreadsheet_id,
            cache_ttl_seconds,
            http_timeout_seconds=http_timeout_seconds,
            credentials=credentials,
        )

    # ------------------------------------------------------------------ #
    # Health-check (Day 0)
    # ------------------------------------------------------------------ #

    async def health_check(self) -> None:
        """
        Проверяет, что сервисный аккаунт может прочитать метаданные обоих
        документов (Меню и Заказы). Кидает SheetsHealthCheckError при сбое.

        ВАЖНО: проверки идут ПОСЛЕДОВАТЕЛЬНО, а не через asyncio.gather.
        Оба вызова используют один и тот же self._service, построенный на
        httplib2.Http — а httplib2.Http не потокобезопасен для одновременных
        запросов с одного инстанса через asyncio.to_thread. Параллельный
        запуск двух проверок мог приводить к тому, что один запрос проходит,
        а второй зависает (наблюдалось как "таймаут на Orders", хотя доступ
        реально был корректным — см. verify_sheets_live.py, где проверка
        идёт последовательно и проходит).
        """
        await self._check_spreadsheet_accessible(self._menu_spreadsheet_id, "Меню")
        await self._check_spreadsheet_accessible(self._orders_spreadsheet_id, "Заказы")

    async def _check_spreadsheet_accessible(self, spreadsheet_id: str, label: str) -> None:
        # См. _fresh_http() / комментарий в __init__: каждый вызов получает
        # свой изолированный HTTP-транспорт вместо общего self._service —
        # так вызовы могут спокойно идти параллельно (в т.ч. с периодическим
        # health-check из планировщика) без риска гонки на общем сокете.
        request = self._service.spreadsheets().get(
            spreadsheetId=spreadsheet_id, fields="spreadsheetId"
        )
        await self._execute_with_retry(
            request,
            error_cls=SheetsHealthCheckError,
            timeout_message=(
                f'Документ "{label}" (spreadsheetId={spreadsheet_id}) не ответил за '
                f"{self._http_timeout_seconds:.0f} сек. Похоже на сетевую проблему "
                f"на пути к Google API (DNS/proxy/файрвол), а не на права доступа — "
                f"тот же сервисный аккаунт с тем же доступом не должен вести себя "
                f"иначе для разных документов на уровне прав."
            ),
            error_message=lambda detail: (
                f'Документ "{label}" (spreadsheetId={spreadsheet_id}) недоступен: '
                f"{detail}. Проверьте SHEETS_*_ID и что сервисный аккаунт добавлен "
                f"в доступ к документу."
            ),
        )

    # ------------------------------------------------------------------ #
    # Чтение каталога (Day 1)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_suspicious_empty_refresh(fresh: list, previous_cache: tuple | None) -> bool:
        """
        Живой баг из тестирования: при кратковременной сетевой
        нестабильности (см. CHECKLIST.md, Day 5) обновление кеша может
        "успешно" вернуться с ПУСТЫМ списком вместо явной ошибки —
        библиотека googleapiclient не всегда бросает исключение на
        деградировавшем соединении, иногда просто отдаёт пустой ответ.
        Раньше это молча перезаписывало хороший кеш пустым — "меню
        опустело" у ВСЕХ мерчантов сразу на весь CACHE_TTL_SECONDS,
        пока не истечёт TTL или не случится ручной рестарт.

        Эвристика: если СВЕЖИЙ результат пуст, а ПРЕДЫДУЩИЙ кеш был
        непустым — это подозрительно (реальное "опустение" листа
        сотрудником — редкое и единичное событие, а не то, что
        происходит на КАЖДОМ обновлении кеша). В этом случае не
        перезаписываем кеш, отдаём последний известный хороший список.
        Цена ложного срабатывания невелика (максимум на один TTL
        задержится действительно опустевший лист), а цена НЕсрабатывания
        (то, что уже случилось в реальности) — заметно хуже.
        """
        return not fresh and previous_cache is not None and bool(previous_cache[1])

    async def get_merchants(self, *, force_refresh: bool = False) -> list[Merchant]:
        """Лист «Мерчанты», с кешем на CACHE_TTL_SECONDS."""
        async with self._merchants_lock:
            if not force_refresh and self._merchants_cache is not None:
                fetched_at, cached = self._merchants_cache
                if time.monotonic() - fetched_at < self._cache_ttl_seconds:
                    return cached

            values = await self._fetch_sheet_values("Мерчанты")
            merchants = [
                Merchant(
                    merchant_id=_s(row["merchant_id"]),
                    category=_s(row.get("category", "")),
                    name=_s(row.get("name", "")),
                    description=_s(row.get("description", "")),
                    is_active=_to_bool(row.get("is_active", "")),
                    today_confirmed=_to_bool(row.get("today_confirmed", "")),
                    working_hours=_s(row.get("working_hours", "")),
                )
                for row in _rows_to_dicts(values)
                if row.get("merchant_id", "").strip()
            ]
            if self._is_suspicious_empty_refresh(merchants, self._merchants_cache):
                logger.warning(
                    "get_merchants() вернул пустой список при непустом предыдущем "
                    "кеше — подозрение на сетевой сбой, отдаю прошлый кеш"
                )
                return self._merchants_cache[1]  # type: ignore[index]
            self._merchants_cache = (time.monotonic(), merchants)
            logger.debug("Обновлён кеш мерчантов: %d записей", len(merchants))
            return merchants

    async def get_items(self, *, force_refresh: bool = False) -> list[MenuItem]:
        """Лист «Позиции», с кешем на CACHE_TTL_SECONDS."""
        async with self._items_lock:
            if not force_refresh and self._items_cache is not None:
                fetched_at, cached = self._items_cache
                if time.monotonic() - fetched_at < self._cache_ttl_seconds:
                    return cached

            values = await self._fetch_sheet_values("Позиции")
            items = [
                MenuItem(
                    item_id=_s(row["item_id"]),
                    merchant_id=_s(row.get("merchant_id", "")),
                    name=_s(row.get("name", "")),
                    description=_s(row.get("description", "")),
                    price_try=_to_float(row.get("price_try", "0")),
                    photo_url=_s(row.get("photo_url", "")),
                    is_active=_to_bool(row.get("is_active", "")),
                    is_available=_to_bool(row.get("is_available", "")),
                )
                for row in _rows_to_dicts(values)
                if row.get("item_id", "").strip()
            ]
            if self._is_suspicious_empty_refresh(items, self._items_cache):
                logger.warning(
                    "get_items() вернул пустой список при непустом предыдущем "
                    "кеше — подозрение на сетевой сбой, отдаю прошлый кеш"
                )
                return self._items_cache[1]  # type: ignore[index]
            self._items_cache = (time.monotonic(), items)
            logger.debug("Обновлён кеш позиций: %d записей", len(items))
            return items

    async def get_couriers(self, *, force_refresh: bool = False) -> list[Courier]:
        """
        Лист «Курьеры» — ВАЖНО: живёт в документе «Заказы»
        (self._orders_spreadsheet_id), не в «Меню» — см. docstring модуля.
        Диспетчеризация должна вызывать с force_refresh=True.
        """
        async with self._couriers_lock:
            if not force_refresh and self._couriers_cache is not None:
                fetched_at, cached = self._couriers_cache
                if time.monotonic() - fetched_at < self._cache_ttl_seconds:
                    return cached

            values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "Курьеры")
            couriers = [
                Courier(
                    courier_id=_s(row.get("courier_id", "")),
                    name=_s(row.get("name", "")),
                    phone=_s(row.get("phone", "")),
                    telegram_id=_s(row.get("telegram_id", "")),
                    is_registered_legal=_to_bool(row.get("is_registered_legal", "")),
                    on_shift=_to_bool(row.get("on_shift", "")),
                    is_active=_to_bool(row.get("is_active", "")),
                )
                for row in _rows_to_dicts(values)
                if row.get("courier_id", "").strip()
            ]
            if self._is_suspicious_empty_refresh(couriers, self._couriers_cache):
                logger.warning(
                    "get_couriers() вернул пустой список при непустом предыдущем "
                    "кеше — подозрение на сетевой сбой, отдаю прошлый кеш"
                )
                return self._couriers_cache[1]  # type: ignore[index]
            self._couriers_cache = (time.monotonic(), couriers)
            logger.debug("Обновлён кеш курьеров: %d записей", len(couriers))
            return couriers

    async def _fetch_sheet_values(self, sheet_name: str) -> list[list[str]]:
        """Чтение из документа «Меню» (мерчанты/позиции) — read-only."""
        return await self._fetch_sheet_values_from(self._menu_spreadsheet_id, sheet_name)

    async def _fetch_sheet_values_from(
        self, spreadsheet_id: str, sheet_name: str
    ) -> list[list[str]]:
        # См. _fresh_http() в __init__ — каждый вызов на своём HTTP-
        # транспорте, поэтому чтения могут спокойно идти параллельно друг
        # с другом и с записью, не деля один непотокобезопасный сокет.
        request = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_name)
        )
        response = await self._execute_with_retry(
            request,
            error_cls=SheetsHealthCheckError,
            timeout_message=(
                f'Лист "{sheet_name}" не ответил за {self._http_timeout_seconds:.0f} сек.'
            ),
            error_message=lambda detail: f'Лист "{sheet_name}" недоступен: {detail}',
        )
        return response.get("values", [])

    # ------------------------------------------------------------------ #
    # Запись (Day 4, ТЗ §4.2) — документ «Заказы»
    # ------------------------------------------------------------------ #

    async def _append_row(self, sheet_name: str, row: list[str]) -> None:
        """Низкоуровневая запись строки в документ «Заказы». Кидает
        SheetsWriteError при любой ошибке — вызывающий код сам решает,
        критично ли это (см. append_event — best-effort, vs
        append_order — обязана дойти до вызывающего кода).

        Под self._append_lock — см. комментарий в __init__: две почти
        одновременные записи могут «наступить друг другу на пятки» уже на
        уровне самого Google Sheets API (не связано с потокобезопасностью
        транспорта — та решена через _fresh_http()).
        """
        async with self._append_lock:
            request = self._service.spreadsheets().values().append(
                spreadsheetId=self._orders_spreadsheet_id,
                range=sheet_name,
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            )
            await self._execute_with_retry(
                request,
                error_cls=SheetsWriteError,
                timeout_message=(
                    f'Запись в лист "{sheet_name}" не завершилась за '
                    f"{self._http_timeout_seconds:.0f} сек."
                ),
                error_message=lambda detail: (
                    f'Не удалось записать строку в лист "{sheet_name}": {detail}'
                ),
            )

    async def append_event(
        self,
        event_type: str,
        actor_role: str,
        actor_id: str,
        order_id: str = "",
        details: str = "",
    ) -> None:
        """
        Событие для аналитики (ТЗ §4.2, лист «События»). Best-effort —
        ошибка записи ЛОГИРУЕТСЯ, но не пробрасывается вызывающему коду:
        сбой аналитики не должен ломать пользовательский флоу (в отличие
        от append_order — см. ниже).
        """
        event_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()
        row = [event_id, timestamp, actor_role, actor_id, order_id, event_type, details]
        try:
            await self._append_row("События", row)
        except SheetsWriteError as exc:
            logger.warning("append_event(%s) не удался: %s", event_type, exc)

    async def append_order(self, order_fields: dict[str, Any]) -> None:
        """
        Запись заказа в лист «Заказы» (ТЗ §4.2, §5 — статус "new"). В
        отличие от append_event — ошибка ПРОБРАСЫВАЕТСЯ (SheetsWriteError):
        если заказ не удалось сохранить, пользователь должен узнать об
        этом, а не увидеть ложное "Заказ принят" (см. handlers/order.py).

        order_fields — словарь с ключами из ORDERS_SHEET_COLUMNS;
        отсутствующие ключи (например, p2p_*/pickup_*/dropoff_* для
        обычного заказа) заполняются пустой строкой.

        ВАЖНО (живой баг из тестирования): order_id вида "011" при
        valueInputOption=USER_ENTERED Google Sheets интерпретирует как
        ЧИСЛО 11, съедая ведущий ноль — дальнейший поиск строки по
        order_id="011" (update_order_fields/get_order — все этапы
        курьера, "Забрал"/"Доставил"/"Проблема") молча не находит
        строку, т.к. в таблице реально хранится "11". Ведущий апостроф —
        стандартный приём Google Sheets, форсирующий текстовый тип
        ячейки; сам апостроф в итоговом значении ячейки не сохраняется
        (это just format hint, не часть данных).
        """
        row = []
        for col in ORDERS_SHEET_COLUMNS:
            value = str(order_fields.get(col, ""))
            if col == "order_id" and value:
                value = f"'{value}"
            row.append(value)
        await self._append_row("Заказы", row)

    # ------------------------------------------------------------------ #
    # Точечное обновление ячеек (Day 5, ТЗ §8) — переходы статуса заказа,
    # смена смены курьера. В отличие от append_* (всегда в конец листа)
    # эти методы находят СУЩЕСТВУЮЩУЮ строку по ключу и правят только
    # указанные ячейки, не трогая остальную строку.
    # ------------------------------------------------------------------ #

    async def _batch_update_cells(
        self, data: list[dict[str, Any]], *, spreadsheet_id: str | None = None
    ) -> None:
        """data — список {"range": "Лист!A1", "values": [[значение]]}.
        Один HTTP-вызов на несколько ячеек сразу (batchUpdate) вместо
        последовательных update() по одной ячейке — быстрее и не оставляет
        строку в частично обновлённом состоянии при сетевом сбое посреди
        серии вызовов.

        spreadsheet_id — по умолчанию документ «Заказы» (исторически
        единственный, куда писал бот до Day 8); Day 8 добавил точечную
        запись и в документ «Меню» (Мерчанты/Позиции, ТЗ §10.1, §10.3) —
        явный параметр вместо жёсткой привязки к self._orders_spreadsheet_id.

        Без сериализации — в отличие от _append_row, здесь ячейки уже
        адресуются точным диапазоном (конкретная строка/колонка конкретного
        заказа или курьера), поэтому конкурентные batchUpdate к разным
        заказам не пересекаются и им нечего делить; изолированный
        HTTP-транспорт на вызов (_fresh_http()) достаточен для безопасности."""
        target_spreadsheet_id = spreadsheet_id or self._orders_spreadsheet_id
        request = self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=target_spreadsheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        )
        await self._execute_with_retry(
            request,
            error_cls=SheetsWriteError,
            timeout_message=f"batchUpdate не завершился за {self._http_timeout_seconds:.0f} сек.",
            error_message=lambda detail: f"Не удалось выполнить batchUpdate: {detail}",
        )

    async def update_courier_on_shift(self, telegram_id: str, on_shift: bool) -> bool:
        """
        /shift_on, /shift_off (ТЗ §8.1). Возвращает False, если курьер с
        таким telegram_id не найден в листе «Курьеры» — вызывающий код
        (handlers/courier.py) уже должен был проверить регистрацию через
        get_couriers()/find_courier_by_telegram_id() до вызова этого
        метода; False здесь — защитный случай (гонка/устаревшие данные),
        не основной путь проверки.
        """
        values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "Курьеры")
        found = _find_row_number(values, "telegram_id", telegram_id)
        if found is None:
            return False
        header, row_number = found
        col_letter = _column_index_to_letter(header.index("on_shift"))
        await self._batch_update_cells(
            [
                {
                    "range": f"Курьеры!{col_letter}{row_number}",
                    "values": [["TRUE" if on_shift else "FALSE"]],
                }
            ]
        )
        return True

    async def update_order_fields(self, order_id: str, fields: dict[str, str]) -> bool:
        """
        Точечно обновляет указанные поля строки заказа (переходы статуса —
        offered/assigned/picked_up/delivered/no_courier/merchant_problem,
        ТЗ §5, §8). fields — {имя_колонки: новое_значение}; ключи не из
        ORDERS_SHEET_COLUMNS молча игнорируются. Возвращает False, если
        заказ с таким order_id не найден.
        """
        values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "Заказы")
        found = _find_row_number(values, "order_id", order_id)
        if found is None:
            return False
        header, row_number = found

        data = [
            {
                "range": f"Заказы!{_column_index_to_letter(header.index(field_name))}{row_number}",
                "values": [[value]],
            }
            for field_name, value in fields.items()
            if field_name in header
        ]
        if data:
            await self._batch_update_cells(data)
        return True

    async def get_order(self, order_id: str) -> dict[str, str] | None:
        """
        Одна строка листа «Заказы» по order_id, как словарь {колонка:
        значение}. Без кеша (в отличие от get_merchants/get_items/
        get_couriers) — вызывается точечно для уведомлений (например,
        нужен свежий user_id клиента сразу после смены статуса), не в
        каждом апдейте, лишний параметр кеширования тут не оправдан.
        """
        values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "Заказы")
        found = _find_row_number(values, "order_id", order_id)
        if found is None:
            return None
        header, row_number = found
        row = values[row_number - 1]  # values — 0-indexed список, row_number — 1-indexed (A1)
        padded = row + [""] * (len(header) - len(row))
        return dict(zip(header, padded))

    # ------------------------------------------------------------------ #
    # Day 8 (ТЗ §11) — админ-команды: точечная запись в «Мерчанты»/
    # «Позиции» (документ «Меню»), «Курьеры» по courier_id, чтение
    # целиком «Заказы»/«События» для /orders, /stats, /broadcast.
    # ------------------------------------------------------------------ #

    async def update_merchant_fields(self, merchant_id: str, fields: dict[str, str]) -> bool:
        """/day_start, /enable_merchant_[id], /toggle_merchant_[id] (ТЗ
        §10.1, §11) — точечное обновление листа «Мерчанты» (документ
        «Меню», не «Заказы» — см. _fetch_sheet_values в __init__).
        Инвалидирует кеш мерчантов сразу — иначе изменение не будет
        видно пользователям до истечения CACHE_TTL_SECONDS (для команд,
        которые ДОЛЖНЫ подействовать мгновенно, это неприемлемая
        задержка)."""
        values = await self._fetch_sheet_values_from(self._menu_spreadsheet_id, "Мерчанты")
        found = _find_row_number(values, "merchant_id", merchant_id)
        if found is None:
            return False
        header, row_number = found
        data = [
            {
                "range": f"Мерчанты!{_column_index_to_letter(header.index(field_name))}{row_number}",
                "values": [[value]],
            }
            for field_name, value in fields.items()
            if field_name in header
        ]
        if data:
            await self._batch_update_cells(data, spreadsheet_id=self._menu_spreadsheet_id)
        async with self._merchants_lock:
            self._merchants_cache = None
        return True

    async def update_item_fields(self, item_id: str, fields: dict[str, str]) -> bool:
        """/toggle_item_[item_id] (ТЗ §10.3) — точечное обновление листа
        «Позиции» (документ «Меню»). Инвалидирует кеш позиций сразу —
        то же обоснование, что у update_merchant_fields выше (ТЗ §10.3
        прямо требует "позиция мгновенно исчезает из меню")."""
        values = await self._fetch_sheet_values_from(self._menu_spreadsheet_id, "Позиции")
        found = _find_row_number(values, "item_id", item_id)
        if found is None:
            return False
        header, row_number = found
        data = [
            {
                "range": f"Позиции!{_column_index_to_letter(header.index(field_name))}{row_number}",
                "values": [[value]],
            }
            for field_name, value in fields.items()
            if field_name in header
        ]
        if data:
            await self._batch_update_cells(data, spreadsheet_id=self._menu_spreadsheet_id)
        async with self._items_lock:
            self._items_cache = None
        return True

    async def update_courier_fields(self, courier_id: str, fields: dict[str, str]) -> bool:
        """/toggle_courier_[id] (ТЗ §11) — точечное обновление листа
        «Курьеры» ПО courier_id. Отдельный метод от update_courier_on_shift
        (который ищет по telegram_id) намеренно: тот вызывается из
        контекста самого курьера в handlers/courier.py, где известен
        только его собственный telegram_id; этот — из контекста Админа
        в handlers/admin.py, который оперирует courier_id (тем же
        идентификатором, что во всех остальных админ-командах —
        /order_042, /toggle_item_[id] и т.д.)."""
        values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "Курьеры")
        found = _find_row_number(values, "courier_id", courier_id)
        if found is None:
            return False
        header, row_number = found
        data = [
            {
                "range": f"Курьеры!{_column_index_to_letter(header.index(field_name))}{row_number}",
                "values": [[value]],
            }
            for field_name, value in fields.items()
            if field_name in header
        ]
        if data:
            await self._batch_update_cells(data)
        async with self._couriers_lock:
            self._couriers_cache = None
        return True

    async def add_courier(
        self,
        *,
        courier_id: str,
        name: str,
        phone: str,
        telegram_id: str,
        is_registered_legal: bool,
        on_shift: bool,
        is_active: bool,
    ) -> None:
        """/add_courier (ТЗ §11) — новая строка в лист «Курьеры» (документ
        «Заказы», см. docstring get_couriers()). Схема курьера значительно
        проще заказов (7 колонок, ТЗ §4.2, все значения известны заранее
        целиком) — отдельная константа-кортеж вроде ORDERS_SHEET_COLUMNS
        не заведена специально: порядок параметров здесь И ЕСТЬ контракт,
        должен совпадать с реальным порядком колонок листа «Курьеры»."""
        row = [
            courier_id,
            name,
            phone,
            telegram_id,
            "TRUE" if is_registered_legal else "FALSE",
            "TRUE" if on_shift else "FALSE",
            "TRUE" if is_active else "FALSE",
        ]
        await self._append_row("Курьеры", row)
        async with self._couriers_lock:
            self._couriers_cache = None

    async def get_all_orders(self) -> list[dict[str, str]]:
        """Все строки листа «Заказы» как список словарей {колонка:
        значение} — для /orders (активные) и /stats (сводка за сегодня).
        Без кеша — та же причина, что у get_order() (точечный вызов по
        явной команде Админа, не в каждом апдейте)."""
        values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "Заказы")
        return _rows_to_dicts(values)

    async def get_all_events(self) -> list[dict[str, str]]:
        """Все строки листа «События» — для /broadcast (список клиентов,
        см. handlers/admin.py: собираются actor_id из событий bot_start
        с actor_role="client", а не только те, кто успел заказать —
        "всем клиентам" по ТЗ §11 читается как "все, кто хоть раз начал
        диалог с ботом", а не только состоявшиеся покупатели)."""
        values = await self._fetch_sheet_values_from(self._orders_spreadsheet_id, "События")
        return _rows_to_dicts(values)
