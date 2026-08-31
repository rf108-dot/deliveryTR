# Antalya Delivery Bot

Telegram-бот доставки по Антальи (MVP). Правовая модель — «поручение»
(vekalet): платформа не участвует в денежных расчётах, см. ТЗ §3.

**Статус:** Day 6 реализован — окружение (Day 0) + каталог с корзиной
(Day 1-2) + оформление заказа (Day 3) + оферта/часы работы/заглушка
оплаты (Day 4) + курьерский модуль (Day 5) + тайм-ауты застрявших
заказов через APScheduler + периодический health-check (Day 6). Полный
план разработки — раздел [«План по дням»](#план-по-дням) ниже и
`CHECKLIST.md`.

## Стек

- Python 3.11+
- aiogram 3.x (long polling)
- RedisStorage (FSM), обязателен с Day 0 — MemoryStorage не используется
- Google Sheets API (документ «Меню» — чтение, документ «Заказы» — запись)
- Google Maps Geocoding API (reverse + forward geocoding)
- APScheduler (таймеры застрявших заказов — с Day 6)
- Хостинг: Railway (Hobby-план), Redis — как add-on Railway

## Структура проекта

```
antalya-bot/
├── bot.py                 # Точка входа: только wiring, без бизнес-логики
├── config.py               # Settings (Pydantic Settings) — валидация env
├── scheduler.py            # Каркас APScheduler (задачи — Day 6)
├── handlers/                # Роутеры aiogram, по одному на функциональный блок
│   ├── start.py             # /start — реализован (Day 0)
│   ├── catalog.py            # Категории/мерчанты/меню — реализован (Day 1)
│   ├── cart.py                # Корзина — реализован (Day 2)
│   ├── order.py               # Оформление заказа — реализован (Day 3)
│   ├── custom_order.py        # Нестандартный заказ — TODO Day 7
│   ├── p2p.py                  # P2P-доставка — TODO Day 7
│   ├── courier.py              # Курьерский модуль — реализован (Day 5)
│   ├── support.py               # Поддержка клиентов — TODO Day 7
│   ├── payment.py                # Заглушка оплаты — реализован (Day 4)
│   └── admin.py                   # /service_stop, /service_resume (Day 4); остальное — TODO Day 8
├── services/
│   ├── sheets.py             # Google Sheets: клиент + health-check (Day 0)
│   ├── geocoding.py           # Google Maps: reverse+forward geocode (Day 3)
│   ├── zone.py                 # Геозона: гаверсинус (Day 3)
│   ├── dispatch.py              # Offer-based диспетчеризация — реализован (Day 5)
│   ├── notifications.py          # notify_admins() — реализован (Day 5)
│   └── service_hours.py           # Часы работы/service_stop — реализован (Day 4)
├── states/
│   └── user_states.py         # OrderStates — реализован (Day 3)
├── texts/ru.py               # Русские тексты (ключ → строка)
├── utils/                     # formatters.py — реализован; validators.py — TODO
├── tests/                      # pytest
├── requirements.txt
├── requirements-dev.txt        # доп. файл: pytest/fakeredis, не входит в ТЗ §16
├── .env.example
├── Makefile                     # доп. файл: удобные команды разработки
└── README.md
```

## Локальный запуск

### 1. Требования

- Python 3.11+
- Redis (локально через Docker или системный пакет)
- Service account Google Cloud с доступом к Sheets API и Maps Geocoding API
  включённым в проекте (billing должен быть подключён к проекту GCP)

### 2. Установка зависимостей

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# для тестов и разработки:
pip install -r requirements-dev.txt
```

### 3. Redis

Быстрый запуск через Docker:

```bash
make redis-up      # docker run -d --name antalya-bot-redis -p 6379:6379 redis:7-alpine
# ...
make redis-down     # остановить и удалить контейнер
```

Либо локально установленный Redis (`redis-server`) на порту 6379.

### 4. Настройка `.env`

```bash
cp .env.example .env
```

Заполните обязательные переменные (полный список и комментарии — в
`.env.example`, полное описание — ТЗ §13):

| Переменная | Как получить |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ADMIN_TELEGRAM_IDS` | Ваш Telegram ID через [@userinfobot](https://t.me/userinfobot), через запятую если админов несколько |
| `REDIS_URL` | `redis://localhost:6379/0` для локального Redis из шага 3 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud Console → IAM → Service Accounts → Keys → Create key (JSON) → `base64 -w0 service-account.json` |
| `SHEETS_MENU_ID` | ID из URL документа «Меню» (после `/d/` и до `/edit`); документ должен быть расшарен на email сервисного аккаунта |
| `SHEETS_ORDERS_ID` | Аналогично для документа «Заказы» (доступ на редактирование) |
| `GOOGLE_MAPS_API_KEY` | Google Cloud Console → APIs & Services → Credentials, при включённом Geocoding API |

Остальные переменные (часы работы, тайм-ауты, геозона и т.д.) имеют
рабочие значения по умолчанию в `.env.example` — их можно не менять
на Day 0.

### 5. Запуск бота

```bash
make run
# эквивалентно: python bot.py
```

При успешном старте в логах будет:

```
Health-check OK: Redis
Health-check OK: Google Sheets (Меню + Заказы)
Health-check OK: Google Maps adapter инициализирован
Startup health-check пройден полностью
Бот запущен, начат long polling
```

Отправьте боту `/start` в Telegram — должен прийти текст приветствия
(и баннер, если задан `WELCOME_IMAGE_URL`).

**Если `.env` невалиден** (отсутствует обязательная переменная, битый
Redis URL, некорректный service account JSON и т.д.) — бот упадёт при
старте с понятным сообщением об ошибке и не запустит polling.

### 6. Тесты

```bash
make test
# эквивалентно: pytest -v
```

Покрыто на Day 0: валидация `config.py` (обязательные поля, форматы,
кросс-полевые проверки) и хендлер `/start`. Health-check к реальным
Google Sheets/Maps в юнит-тестах не вызывается (это сетевые вызовы —
проверяется вручную при запуске бота).

## Настройка в Railway

1. Создайте проект в Railway, подключите репозиторий.
2. Добавьте **Redis** как add-on (Railway сам создаст `REDIS_URL` — либо
   скопируйте connection string в переменную окружения сервиса бота).
3. В настройках сервиса (Variables) задайте все переменные из
   `.env.example` (значения — как в шаге 4 выше).
4. Start command: `python bot.py` (или через `Procfile`/`railway.json`,
   если используется отдельный build).
5. Railway (Hobby-план) держит процесс запущенным постоянно — это
   соответствует модели long polling, используемой ботом на Day 0.
   Webhook-режим в текущей архитектуре не используется.
6. После деплоя проверьте логи Railway — health-check должен пройти
   так же, как локально (см. пункт 5 выше).

## Здоровье приложения (health-check)

При каждом старте (`bot.py`) проверяются:

- **Redis** — `PING`
- **Google Sheets** — доступность метаданных обоих документов (Меню и
  Заказы) под текущим service account
- **Google Maps adapter** — успешная инициализация клиента (без
  реального сетевого вызова к Geocoding API — квота не тратится на
  каждый рестарт, см. ТЗ, промпт Day 0)

При сбое любой проверки процесс завершается с ненулевым кодом выхода и
понятным сообщением в логах — это осознанное поведение (fail fast),
чтобы не эксплуатировать бота в частично рабочем состоянии.

Периодический health-check каждые 30 минут (ТЗ §12.2) — **TODO**, будет
добавлен вместе со scheduler.py на Day 6.

## Данные для Day 1: структура документа «Меню»

Чтобы каталог реально заработал на вашем `SHEETS_MENU_ID`, в документе
должны быть два листа с точными названиями и заголовками колонок (ТЗ §4.1):

**Лист «Мерчанты»** (порядок колонок не важен, названия — важны):
`merchant_id`, `category`, `name`, `description`, `is_active`,
`today_confirmed`, `working_hours`

**Лист «Позиции»**:
`item_id`, `merchant_id`, `name`, `description`, `price_try`,
`photo_url`, `is_active`, `is_available`

Допустимые категории (`category`): `restaurant`, `grocery`, `pharmacy`,
`vet`, `water`, `hardware`. Булевы поля — `TRUE`/`FALSE` (регистр не
важен). Мерчант показывается пользователю только если
`is_active=TRUE` И `today_confirmed=TRUE`; позиция — если `is_active=TRUE`
(независимо от `is_available` — просто будет помечена недоступной).

## Graceful shutdown

Бот перехватывает `SIGINT`/`SIGTERM`: останавливает long polling,
закрывает HTTP-сессию бота, закрывает соединение с Redis, останавливает
планировщик. Это важно для Railway, который посылает `SIGTERM` при
редеплое/рестарте.

## План по дням

Полный план — ТЗ §17. Кратко:

| День | Задача | Статус |
|---|---|---|
| 0 | Окружение, структура репо, рабочий `/start` | ✅ done |
| 1 | Категории → мерчанты → меню из Sheets, кеш | ✅ done |
| 2 | Корзина | ✅ done |
| 3 | Оформление заказа, геолокация, геозона | ✅ done |
| 4 | Оферта, часы работы, заглушка оплаты | ✅ done |
| 5 | Курьерский модуль | ✅ done |
| 6 | Статусная модель + тайм-ауты + уведомления | ✅ done |
| 7 | Поддержка + нестандартный заказ + P2P | ⏳ TODO |
| 8 | Админ-команды, деплой, smoke test | ⏳ TODO |

См. также `CHECKLIST.md` для более детального чек-листа по дням.

## Что реализовано на Day 0

- ✅ Структура репозитория строго по ТЗ §16 (+ `requirements-dev.txt` и
  `Makefile` как дополнительные файлы для удобства разработки)
- ✅ `config.py`: полная валидация всех env-переменных из ТЗ §13 через
  Pydantic Settings (обязательные поля, форматы, кросс-полевые проверки
  вроде порядка часов работы и условной обязательности провайдера оплаты)
- ✅ `.env.example` со всеми переменными ТЗ §13
- ✅ `bot.py`: wiring aiogram + RedisStorage, startup health-check (Redis,
  оба Google Sheets документа, инициализация Google Maps adapter),
  graceful shutdown по SIGINT/SIGTERM, логирование в stdout
- ✅ Минимальный рабочий `/start` (`handlers/start.py`)
- ✅ Каркасы всех остальных роутеров (`handlers/`) и сервисов
  (`services/zone.py`, `dispatch.py`, `notifications.py`,
  `service_hours.py`) — без бизнес-логики, только структура + TODO
- ✅ `scheduler.py`: каркас APScheduler (без задач)
- ✅ pytest: валидация конфига + `/start`

## Что реализовано на Day 1

- ✅ `services/sheets.py`: модели `Merchant`/`MenuItem`, `get_merchants()`
  и `get_items()` с кешем на `CACHE_TTL_SECONDS` (потокобезопасный
  `asyncio.Lock`), чистые функции фильтрации по категориям/мерчанту
- ✅ `SheetsClient` рефакторен для тестируемости: `from_service_account_info()`
  — фабрика для прод-инициализации, конструктор принимает готовый `service`
- ✅ `handlers/catalog.py`: категории (inline-клавиатура, только видимые),
  мерчанты (список / авто-выбор при одном), карточки позиций меню с
  фото и пагинацией (← N/M →)
- ✅ Кнопка входа в P2P на экране категорий (заглушка-алерт, реальный
  флоу — Day 7)
- ✅ `/start` теперь ведёт сразу в каталог (временно, до оферты Day 4)
- ✅ `utils/formatters.py`: `format_price_try()`, `format_item_caption()`
- ✅ pytest: `test_sheets_client.py`, `test_catalog_handlers.py`,
  `test_formatters.py`; `test_start_handler.py` обновлён

Известные упрощения Day 1 (не баги, осознанные решения) — см.
`CHECKLIST.md`, раздел «Известные упрощения Day 1» (логирование событий
пока только в stdout, фоллбэк на пересоздание сообщения при смене типа
контента фото↔текст между позициями).

## Что реализовано на Day 2

- ✅ `handlers/cart.py`: add/inc/dec, лимит `MAX_ITEMS_PER_TYPE`, просмотр
  корзины, кнопка «Изменить» (назад к меню), кнопка «К ресторанам»
- ✅ Корзина хранится в `FSMContext.data` (переиспользуем Redis из Day 0,
  без нового клиента) — `{"merchant_id": ..., "items": {item_id: qty}}`
- ✅ `handlers/catalog.py`: карточка позиции стала cart-aware — показывает
  `[➕ Добавить]` / `[− N +]` / `[🚫 Недоступно]` и кнопку
  `[🛒 Корзина (N) · X ₺]`; обновление через `edit_message_reply_markup`
  (не новое сообщение — точно по ТЗ §7.3.2)
- ✅ Предупреждение с подтверждением при смене мерчанта с непустой
  корзиной другого мерчанта — «Отмена» реально возвращает к корзине
- ✅ `utils/formatters.py`: `format_cart_summary()`
- ✅ pytest: `test_cart_handlers.py`; `test_catalog_handlers.py` дополнен
  тестами cart-aware клавиатуры

Архитектурная заметка: чтобы избежать циклического импорта между
`catalog.py` и `cart.py`, все callback-классы и построение клавиатуры
позиции остались в `catalog.py` (однонаправленная зависимость
`cart → catalog`); подробности — `CHECKLIST.md`.

## Что реализовано на Day 3

- ✅ `services/zone.py`: гаверсинус + `is_point_in_zone()` (радиус, ТЗ §3A)
- ✅ `services/geocoding.py`: `reverse_geocode()` (координаты → адрес) и
  `geocode()` (адрес → координаты — нужен для проверки зоны при ручном
  вводе адреса, см. «Архитектурное решение» ниже), оба с жёстким
  `asyncio.wait_for`-таймаутом
- ✅ `states/user_states.py`: `OrderStates` — первое реальное
  использование FSM *состояний* в проекте (не только данных)
- ✅ `handlers/order.py`: полный флоу оформления заказа —
  геолокация/ручной адрес → проверка зоны → reverse geocoding →
  подтверждение адреса → контакт (@username автоматически, иначе запрос
  телефона) → перепроверка наличия (bypass кеша Sheets) → итоговое
  подтверждение → «заказ принят» (текст зависит от
  `address_manually_edited`, ТЗ §7.4.3)
- ✅ `handlers/cart.py`: кнопка «Оформить заказ» запускает реальный флоу;
  `render_cart_view()` вынесена в публичную функцию для переиспользования
- ✅ pytest: `test_zone.py`, `test_geocoding.py`, `test_order_handlers.py`
  (полное покрытие флоу — зона внутри/снаружи, geocoding успех/провал,
  username vs телефон, перепроверка наличия блокирует заказ)

**Архитектурное решение:** ТЗ §7.4.1 описывает только reverse geocoding
(после отправки геолокации), но §3A требует проверять геозону для точки
клиента всегда — при ручном вводе адреса координат иначе не было бы
вообще. Решение — форвард-геокодирование введённого текстом адреса
специально для проверки зоны. Подробнее — `CHECKLIST.md`.

**Известное упрощение:** хендлеры Day 1-2 не имеют `StateFilter` и
формально сработают, даже если пользователь сейчас в процессе
оформления заказа (редкий edge case, посчитан не критичным для MVP) —
подробнее в `CHECKLIST.md`.

## Что реализовано на Day 4

- ✅ Оферта (`handlers/start.py`, ТЗ §7.0.1) — один раз при первом
  `/start` (флаг в `FSMContext.data`, переживает рестарт через Redis),
  [✅ Принимаю] / [📄 Полный текст] / [← Назад]
- ✅ `services/service_hours.py`: три временных окна + `/service_stop`
  (глобальный флаг в Redis, приоритет над расписанием, ТЗ §6, §10.5)
- ✅ `handlers/admin.py`: `/service_stop`, `/service_resume` — только эти
  две команды (остальная админ-панель — Day 8)
- ✅ Заглушка оплаты — `PAYMENT_ENABLED=False` пропускает шаг; при
  `True` — явный отказ, а не тихий пропуск (см. «Архитектурное решение»)
- ✅ **Первая настоящая запись в Google Sheets** — `append_event()`
  (best-effort) и `append_order()` (ошибка пробрасывается) в
  `services/sheets.py`; `order_id` — атомарный счётчик в Redis
- ✅ Заодно закрыты все `TODO Day 4+` из Day 1-3 — события каталога и
  корзины (`category_selected`, `item_added` и т.д.) теперь реально
  пишутся в лист «События»
- ✅ pytest: `test_service_hours.py`, `test_admin.py`, `test_start_handler.py`
  переписан под оферту, `test_order_handlers.py` и `test_sheets_client.py`
  дополнены

**Архитектурное решение (часы работы):** проверка стоит в
`handlers/order.py` (кнопка «Оформить заказ» + повторно на
«Подтвердить»), а не в `/start` — ТЗ §6 явно говорит «Меню можно
смотреть» даже когда сервис закрыт, значит блокировать нужно именно
оформление, а не показ каталога. Подробнее — `CHECKLIST.md`.

**Архитектурное решение (оплата):** `PAYMENT_ENABLED=True` без
доп. разработки Papara/Iyzico-обработки (требует HTTP-сервера, которого
у бота нет — только long polling) даёт явный, понятный отказ, а не
тихо создаёт заказ так, будто оплата прошла.

## Что реализовано на Day 5

- ✅ Лист «Курьеры» — обнаружен важный нюанс ТЗ §4.2: он в документе
  «Заказы», не в «Меню» (курьеров и читаем, и пишем — `on_shift`)
- ✅ `services/sheets.py`: `Courier`, `get_couriers()`, точечная запись/
  чтение — `update_courier_on_shift()`, `update_order_fields()`,
  `get_order()` (правят конкретные ячейки по ключу, не трогая остальную
  строку — новый паттерн, отдельный от `append_*`)
- ✅ `services/dispatch.py`: offer-based диспетчеризация (ТЗ §8.2) —
  атомарный захват заказа через `Redis SET NX`, синхронная эскалация
  Админу при 0 курьеров на смене
- ✅ `services/notifications.py`: `notify_admins()` — общая точка рассылки
  всем админам (устраняет дублирование в `dispatch.py`/`courier.py`)
- ✅ `handlers/courier.py`: `/shift_on`/`/shift_off` + персистентная
  кнопка, приём «Взять», три кнопки этапов с уведомлениями
- ✅ `handlers/start.py`: роутинг по роли (ТЗ §2) — зарегистрированный
  курьер видит курьерское приветствие вместо оферты/каталога
- ✅ `handlers/order.py`: реальный запуск диспетчеризации после создания
  заказа (был `TODO Day 5`)
- ✅ pytest: `test_dispatch.py`, `test_courier_handlers.py`,
  `test_notifications.py` (новые), остальные дополнены — **205 тестов**

**Архитектурное решение:** сообщение курьеру о новом заказе НЕ включает
дистанцию и «рекомендованное вознаграждение» из мокапа ТЗ §8.2 — в
текущей схеме листа «Мерчанты» нет координат мерчанта (нечем честно
посчитать расстояние), а вознаграждение для обычных заказов explicitly
«вне бота» (ТЗ §15). Подробнее — `CHECKLIST.md`.

**Явно НЕ входит в Day 5** (см. план: Day 6 = «тайм-ауты + APScheduler»):
автоматический перевод `offered → no_courier`, если никто не принял
заказ за `OFFER_TIMEOUT_SEC` — нужен персистентный планировщик, которого
пока нет. До Day 6 непринятый заказ может провисеть в `offered` бессрочно.

### Настройка листа «Курьеры» (одноразово, вручную)

В документе `DeliveryTR_orders` создайте вкладку **«Курьеры»** с шапкой:

```
courier_id	name	phone	telegram_id	is_registered_legal	on_shift	is_active
```

## Что осознанно НЕ реализовано (TODO)

- Автоматические тайм-ауты (`OFFER_TIMEOUT_SEC` → `no_courier`,
  `PICKUP_TIMEOUT_MIN`/`DELIVERY_TIMEOUT_MIN` → эскалации) — Day 6,
  требуют `scheduler.py` (пока каркас без задач)
- Полная статусная модель + уведомления по всем переходам — Day 6
- Поддержка клиентов, нестандартный заказ, P2P (реальный флоу) — Day 7
- Полная админ-панель (заказы, мерчанты, курьеры и т.д. из ТЗ §11) — Day 8
- Периодический health-check раз в 30 минут (ТЗ §12.2) — Day 6, вместе со
  scheduler.py
- Реальная обработка Papara/Iyzico (требует HTTP-сервера параллельно с
  long polling) — вне плана 9 дней MVP, только при включении монетизации
- Мультиязычность EN/TR (архитектурная заглушка готова: `texts/ru.py`,
  раздел §14) — вне плана 9 дней MVP
