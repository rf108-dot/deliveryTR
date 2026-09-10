"""
Русские тексты бота (ключ → строка), см. ТЗ §14 «Мультиязычность — заглушка».

Day 0: только текст для минимального /start. Оферта (ТЗ §7.0.1), меню,
корзина и весь остальной пользовательский флоу — TODO следующих дней.
При добавлении EN/TR сюда должны быть скопированы те же ключи в
texts/en.py, texts/tr.py.
"""

from __future__ import annotations

START_GREETING = (
    "👋 Привет! Это бот доставки в Анталии.\n\n"
    "Мы дорабатываем сервис — полный сценарий заказа появится "
    "в ближайшие дни. Спасибо, что заглянули!"
)

# --- Day 1: категории / мерчанты / меню (ТЗ §7.1-7.3.1) ---------------------

CATEGORY_LABELS: dict[str, str] = {
    "restaurant": "🍽 Рестораны",
    "grocery": "🛒 Магазины",
    "pharmacy": "💊 Аптеки",
    "vet": "🐾 Зооклиники",
    "water": "💧 Вода",
    "hardware": "🔧 Хозтовары",
}

P2P_BUTTON_TEXT = "📦 Доставить что-угодно (P2P)"
P2P_STUB_ALERT = "🚧 P2P-доставка появится на Day 7."

CATALOG_CHOOSE_CATEGORY = "Выберите категорию:"
CATALOG_NO_CATEGORIES = (
    "Сейчас нет доступных категорий с открытыми заведениями. Загляните позже!"
)
CATALOG_NO_MERCHANTS_IN_CATEGORY = (
    "Пока нет доступных заведений в этой категории. Загляните позже!"
)
CATALOG_CHOOSE_MERCHANT = "Выберите заведение:"
CATALOG_MERCHANT_UNAVAILABLE = "Это заведение больше недоступно, выберите другое."
CATALOG_EMPTY_MENU_TEMPLATE = "В «{name}» пока нет позиций в меню. Загляните позже!"

ITEM_NAV_BACK = "← Назад"
ITEM_NAV_FORWARD = "Вперёд →"
ITEM_ADD_BUTTON = "➕ Добавить"
ITEM_UNAVAILABLE_BUTTON = "🚫 Недоступно сейчас"
ITEM_UNAVAILABLE_ALERT = "Эта позиция сейчас недоступна."
ITEM_QTY_DEC_BUTTON = "−"
ITEM_QTY_INC_BUTTON = "+"
ITEM_QTY_COUNT_TEMPLATE = "В заказе: {qty} шт."

# --- Day 2: корзина (ТЗ §7.3.2-7.3.3) ---------------------------------------

CART_BUTTON_TEMPLATE = "🛒 Корзина ({count}) · {price}"
CART_EMPTY_ALERT = "Ваша корзина пуста."
CART_ITEM_LIMIT_ALERT_TEMPLATE = "Максимум {max} шт. одной позиции в заказе."

CART_SWITCH_WARNING_TEMPLATE = (
    "В корзине {count} тов. из другого заведения.\n"
    "Продолжить — корзина будет очищена."
)
CART_SWITCH_CONFIRM_BUTTON = "Да, очистить и продолжить"
CART_SWITCH_CANCEL_BUTTON = "Отмена"

CART_VIEW_HEADER = "🛒 Ваша корзина:"
CART_TOTAL_TEMPLATE = "Итого за товары: {total}"
CART_VIEW_NOTE = "Вознаграждение курьеру согласуется отдельно."
CART_EDIT_BUTTON = "✏️ Изменить"
CART_BACK_TO_CATEGORIES_BUTTON = "🏠 К ресторанам"
CART_CHECKOUT_BUTTON = "✅ Оформить заказ"

# --- Day 3: оформление заказа (ТЗ §7.4, §3A) --------------------------------

# Шаг 1: геолокация (7.4.1)
ORDER_LOCATION_PROMPT = (
    "📍 Поделитесь геолокацией — заполним адрес автоматически.\n\n"
    "На телефоне: нажмите кнопку ниже.\n"
    "На компьютере: кнопка может не сработать — нажмите 📎 (скрепка) → "
    "«Местоположение».\n\n"
    "Либо сразу выберите «Ввести адрес вручную»."
)
ORDER_LOCATION_BUTTON = "📍 Отправить геолокацию"
ORDER_MANUAL_ADDRESS_BUTTON = "✏️ Ввести адрес вручную"
ORDER_UNEXPECTED_INPUT_IN_LOCATION_STEP = (
    "Нажмите одну из кнопок ниже — «Отправить геолокацию» или "
    "«Ввести адрес вручную»."
)

# Зона доставки (§3A) — общее сообщение, не зависящее от способа ввода адреса
ORDER_ZONE_OUTSIDE_MESSAGE = (
    "😔 Этот адрес вне зоны доставки. Пока не можем принять заказ по нему."
)
# Тот же случай, но адрес был найден неточно (Google не смог точно
# сопоставить введённый текст) — важно объяснить причину, а не просто
# сказать "вне зоны", раз сама точка приблизительная.
ORDER_ZONE_OUTSIDE_UNCERTAIN_MESSAGE = (
    "😔 Google нашёл только приблизительное совпадение для этого адреса "
    "(не точное) — и получившаяся точка оказалась вне зоны доставки. "
    "Уточните улицу и район и попробуйте ещё раз."
)

# Reverse geocoding (адрес по геолокации)
ORDER_ADDRESS_CONFIRM_TEMPLATE = "📍 Ваш адрес:\n{address}\n\nВсё верно?"
ORDER_ADDRESS_CONFIRM_BUTTON = "✅ Верно"
ORDER_ADDRESS_EDIT_BUTTON = "✏️ Изменить"
ORDER_LOCATION_RECEIVED_ACK = "Получили геолокацию, проверяем адрес..."

# Ручной ввод адреса
ORDER_MANUAL_ADDRESS_PROMPT = (
    "Введите адрес доставки текстом:\n\n"
    "Если не получается — попробуйте написать название улицы латиницей "
    "или по-турецки (например, Çağlayan вместо Чаглаян)."
)
ORDER_ADDRESS_EDIT_PROMPT_TEMPLATE = (
    "Текущий адрес (можно скопировать и поправить нужную часть — "
    "например, только номер дома):\n\n{address}\n\nВведите исправленный адрес текстом:"
)
ORDER_ADDRESS_NOT_FOUND = (
    "Не удалось распознать этот адрес. Попробуйте описать его иначе "
    "(улица, район, ориентир), а если писали кириллицей — попробуйте "
    "латиницей или по-турецки (например, Çağlayan вместо Чаглаян)."
)
ORDER_ADDRESS_PARTIAL_MATCH_WARNING = (
    "⚠️ Не до конца уверены в точности этого адреса — детали лучше "
    "уточнить с курьером после подтверждения заказа."
)

# Шаг 2: контакт для курьера (7.4.2)
ORDER_CONTACT_PROMPT = (
    "Отправьте, пожалуйста, номер телефона — так курьеру и поддержке "
    "будет проще с вами связаться."
)
ORDER_CONTACT_BUTTON = "📞 Отправить номер"
ORDER_CONTACT_RECEIVED_ACK = "Спасибо! Формируем итог заказа..."
ORDER_CONTACT_INVALID_FORMAT_MESSAGE = (
    "Это не похоже на номер телефона. Введите номер цифрами, например "
    "+905551234567, или нажмите кнопку выше."
)
ORDER_PHONE_CONFIRM_TEMPLATE = (
    "📞 Номер {phone} корректный?\n"
    "Если нет, курьер не сможет дозвониться."
)

# Шаг 3: итоговое подтверждение (7.4.3)
ORDER_SUMMARY_HEADER = "📋 Проверьте заказ:"
ORDER_SUMMARY_MERCHANT_LINE = "Заведение: {name}"
ORDER_SUMMARY_ADDRESS_LINE = "Адрес: {address}"
ORDER_SUMMARY_CONTACT_USERNAME_LINE = "Контакт: @{username}"
ORDER_SUMMARY_CONTACT_PHONE_LINE = "Контакт: {phone}"
ORDER_SUMMARY_TOTAL_LINE = "Сумма за товары: {total}"
ORDER_SUMMARY_PAYMENT_NOTE = (
    "Оплата товара и вознаграждение курьеру — наличными напрямую."
)
ORDER_CONFIRM_BUTTON = "✅ Подтвердить"
ORDER_EDIT_BUTTON = "✏️ Изменить"

ORDER_ITEMS_UNAVAILABLE = (
    "😔 Пока вы оформляли заказ, некоторые позиции из корзины стали "
    "недоступны. Обновите корзину и попробуйте снова."
)

# Финальные подтверждающие сообщения (зависят от address_manually_edited)
ORDER_ACCEPTED_MANUAL_ADDRESS_TEMPLATE = (
    "✅ Заказ принят. Курьер приедет по адресу: {address}. Будьте на связи!"
)
ORDER_ACCEPTED_DEFAULT = "✅ Заказ принят. Ищем курьера, ожидайте!"
ORDER_AGAIN_BUTTON = "🔄 Заказать ещё раз"

# --- Day 5 hotfix: /help — единая инструкция для обеих ролей ---------------
# (живой фидбэк: не только "молчание" бота сбивало с толку, но и то, что
# ничего не объясняло разницу между клиентским /start и курьерским
# /shift_on — теперь есть явная команда с объяснением обоих путей)
HELP_MESSAGE = (
    "🍽 Хотите заказать доставку — отправьте /start.\n"
    "🛵 Вы курьер — отправьте /shift_on, чтобы выйти на смену."
)

# --- Day 4 hotfix: универсальный ответ на нераспознанное сообщение ----------
# (живой фидбэк: пользователь без подсказки ≡/Start не знает, что делать —
# бот не должен молчать в ответ на любое сообщение вне активного флоу)
FALLBACK_HELP_MESSAGE = (
    "Не совсем понял 🤔\n\n"
    "🍽 Хотите заказать доставку — нажмите кнопку ниже или отправьте /start.\n"
    "🛵 Вы курьер — отправьте /shift_on."
)
FALLBACK_CATEGORIES_BUTTON = "🍽 Оформить доставку"

# TODO Day 5-6: тексты статусной модели заказа для клиента/курьера (ТЗ §5)

# --- Day 7: нестандартный заказ у мерчанта (ТЗ §7.3.4) -----------------------

CUSTOM_ORDER_BUTTON = "📝 Нет нужного? Написать запрос"
CUSTOM_ORDER_PROMPT = (
    "📝 Опишите текстом, что хотите заказать у «{merchant_name}».\n\n"
    "При желании прикрепите фото."
)
CUSTOM_ORDER_CANCEL_BUTTON = "← Отмена"
CUSTOM_ORDER_EMPTY_TEXT_MESSAGE = "Опишите, пожалуйста, текстом, что хотите заказать."
CUSTOM_ORDER_CANCELLED_ACK = "Запрос отменён."
CUSTOM_ORDER_SUBMITTED_ACK = "⏳ Ожидайте подтверждения (обычно это занимает до 10 минут)."

# Показывается клиенту при подтверждении Админом — переход к оформлению (7.4)
CUSTOM_ORDER_CONFIRMED_MESSAGE = (
    "✅ Ваш запрос подтверждён! Теперь оформим доставку — укажите адрес и контакт."
)
CUSTOM_ORDER_REJECTED_TEMPLATE = "К сожалению, сейчас не сможем вам помочь.{reason_suffix}"
CUSTOM_ORDER_REJECTED_REASON_SUFFIX_TEMPLATE = " Причина: {reason}"
CUSTOM_ORDER_TIMEOUT_CLIENT_MESSAGE = (
    "К сожалению, мы не успели обработать ваш запрос вовремя. "
    "Попробуйте оформить его ещё раз."
)

ADMIN_CUSTOM_ORDER_REVIEW_TEMPLATE = (
    "📝 НЕСТАНДАРТНЫЙ ЗАПРОС — {merchant_name}\n\n"
    "Клиент: {contact}\n"
    "Запрос: {description}\n\n"
    "/confirm_custom_{user_id}   ← подтвердить\n"
    "/reject_custom_{user_id} [причина]   ← отклонить"
)
ADMIN_CUSTOM_ORDER_TIMEOUT_TEMPLATE = (
    "⏰ Нестандартный запрос от {contact} не обработан за {timeout_min} мин — "
    "автоматически отклонён."
)
ADMIN_CONFIRM_CUSTOM_NOT_FOUND_MESSAGE = (
    "Не нашёл такой запрос — возможно, он уже обработан или истёк."
)
ADMIN_CUSTOM_ORDER_CONFIRMED_ACK = "✅ Запрос подтверждён, клиент оформляет заказ."
ADMIN_CUSTOM_ORDER_REJECTED_ACK = "Запрос отклонён."
# Живой фидбэк из тестирования: раньше отказ без причины сразу уходил
# клиенту шаблонным текстом без объяснения — это ощущалось грубым.
# Теперь голая "/reject_custom_[id]" без причины ЗАПРАШИВАЕТ у Админа
# текст объяснения (тот же паттерн "запомнить и подождать следующее
# сообщение", что и у /reply_ в handlers/support.py), а не проезжает
# молча с пустой причиной.
ADMIN_REJECT_CUSTOM_REASON_PROMPT = "Опишите причину отказа для клиента:"

# Итоговое подтверждение чекаута (7.4.3) для order_kind=custom — переиспользует
# ORDER_SUMMARY_HEADER/MERCHANT_LINE/ADDRESS_LINE/CONTACT_*_LINE/PAYMENT_NOTE
# из блока Day 3 выше, только строка состава другая (нет каталожных позиций).
ORDER_SUMMARY_CUSTOM_DESCRIPTION_LINE = "Запрос: {description}"

# Карточка предложения курьеру для нестандартного заказа — отдельная от
# COURIER_OFFER_TEMPLATE (Day 5), т.к. нет catalog-суммы total_try, зато
# есть свободный текст запроса, который курьеру обязательно нужно видеть.
COURIER_OFFER_CUSTOM_TEMPLATE = (
    "🆕 Новый заказ #{order_id} (нестандартный)\n\n"
    "Забрать: {merchant_name}\n"
    "{merchant_description}\n\n"
    "Что взять: {custom_description}\n\n"
    "Доставить: {delivery_address}\n\n"
    "⏱ Успейте принять в течение {offer_timeout_sec} сек."
)

# --- Day 7: P2P-доставка (ТЗ §7.6) -------------------------------------------

P2P_DESCRIPTION_PROMPT = (
    "📦 P2P-доставка\n\n"
    "Опишите текстом, что нужно доставить.\n"
    "При желании прикрепите фото.\n\n"
    "Например: «Забрать пакет документов у консьержа и отвезти по адресу»."
)
P2P_EMPTY_TEXT_MESSAGE = "Опишите, пожалуйста, текстом, что нужно доставить."
P2P_CANCEL_BUTTON = "← Отмена"
P2P_CANCELLED_ACK = "Заказ отменён."

P2P_LOCATION_BUTTON = "📍 Геолокация"
P2P_MANUAL_ADDRESS_BUTTON = "✏️ Ввести адрес"
P2P_PICKUP_PROMPT = "📍 Откуда забрать? (точка А)\nПоделитесь геолокацией или введите адрес."
P2P_DROPOFF_PROMPT = "📍 Куда доставить? (точка Б)\nПоделитесь геолокацией или введите адрес."
P2P_UNEXPECTED_INPUT_IN_LOCATION_STEP = (
    "Нажмите одну из кнопок ниже — «Геолокация» или «Ввести адрес»."
)
P2P_LOCATION_RECEIVED_ACK = "Получили геолокацию, проверяем..."

P2P_OUT_OF_ZONE_MESSAGE_TEMPLATE = (
    "😔 Пока не можем доставлять вне зоны работы наших курьеров "
    "(точка «{point_label}» вне зоны)."
)
P2P_BACK_TO_START_BUTTON = "← В начало"

P2P_CONTACT_PROMPT = (
    "Отправьте, пожалуйста, номер телефона — так курьеру будет проще с вами связаться."
)

P2P_SUMMARY_HEADER = "📋 Проверьте P2P-заказ"
P2P_SUMMARY_WHAT_LINE = "Что доставить: {description}"
P2P_SUMMARY_PICKUP_LINE = "Откуда (А): {pickup_address}"
P2P_SUMMARY_DROPOFF_LINE = "Куда (Б): {dropoff_address}"
P2P_SUMMARY_CONTACT_USERNAME_LINE = "Контакт: @{username}"
P2P_SUMMARY_CONTACT_PHONE_LINE = "Контакт: {phone}"
# ТЗ §7.6.5 ссылается на "courier_reward_suggested" ×2 как на готовую
# величину, но расчёт вознаграждения явно вне бота (ТЗ §15 — "модель
# поручения") и нигде не реализован даже для обычных заказов — поэтому
# конкретной цифры не показываем нигде в P2P-экранах, только общее
# примечание (решение подтверждено в разговоре).
P2P_SUMMARY_REWARD_NOTE = (
    "Вознаграждение курьеру согласуется с ним напрямую — для P2P обычно "
    "выше, чем за обычный заказ."
)
P2P_SUBMIT_BUTTON = "✅ Отправить на проверку"
P2P_EDIT_BUTTON = "✏️ Изменить"

P2P_SUBMITTED_ACK = (
    "⏳ Ваш P2P-заказ отправлен на проверку. Обычно это занимает несколько минут."
)
# Живой баг из тестирования: если клиент прикрепил фото ОТДЕЛЬНЫМ
# сообщением уже ПОСЛЕ текстового описания, а не одним сообщением
# вместе с текстом — фото раньше молча терялось (см. handlers/p2p.py::
# on_p2p_late_photo_attached).
P2P_LATE_PHOTO_ATTACHED_ACK = "📎 Фото добавлено к описанию."

ADMIN_P2P_REVIEW_TEMPLATE = (
    "🆕 P2P-ЗАКАЗ #{order_id} — НА ПРОВЕРКУ\n\n"
    "Что: {description}\n"
    "Откуда (А): {pickup_address}\n"
    "Куда (Б): {dropoff_address}\n"
    "Расстояние А→Б: ~{distance_km:.1f} км (по прямой)\n"
    "Клиент: {contact}\n\n"
    "/approve_p2p_{order_id}   ← одобрить, запустить поиск курьера\n"
    "/reject_p2p_{order_id} [причина]   ← отклонить"
)
ADMIN_P2P_REVIEW_TIMEOUT_REMINDER_TEMPLATE = (
    "⏰ Напоминание: P2P-заказ #{order_id} всё ещё ждёт решения "
    "({timeout_min} мин без ответа). /approve_p2p_{order_id} или "
    "/reject_p2p_{order_id} [причина]"
)
ADMIN_P2P_NOT_FOUND_MESSAGE = (
    "Не нашёл такой P2P-заказ на проверке — возможно, уже обработан."
)
ADMIN_P2P_APPROVED_ACK = "✅ Заказ одобрен, ищем курьера."
# Живой баг из тестирования: два почти одновременных /approve_p2p_[id]
# (тот же класс гонки состояний, что и дублирование заказов утром)
# могли ОБА пройти проверку "статус ещё pending_review" и запустить
# диспетчеризацию курьерам дважды — см. P2P_ADMIN_ACTION_LOCK_KEY_TEMPLATE
# в handlers/p2p.py. Второй (заблокированный) вызов получает это
# сообщение вместо молчания — чтобы Админ не подумал, что бот вообще
# не отреагировал на команду.
ADMIN_P2P_ACTION_IN_PROGRESS_MESSAGE = "⏳ Уже обрабатывается — подождите пару секунд."
ADMIN_P2P_REJECTED_ACK = "Заказ отклонён."
# Тот же живой фидбэк, что и ADMIN_REJECT_CUSTOM_REASON_PROMPT в
# custom_order.py — голая "/reject_p2p_[id]" без причины теперь
# запрашивает текст, а не уходит клиенту шаблоном без объяснения.
ADMIN_REJECT_P2P_REASON_PROMPT = "Опишите причину отказа для клиента:"

P2P_APPROVED_CLIENT_MESSAGE = "✅ Заказ одобрен, ищем курьера."
P2P_REJECTED_CLIENT_TEMPLATE = "К сожалению, сейчас не сможем вам помочь.{reason_suffix}"
P2P_REJECTED_REASON_SUFFIX_TEMPLATE = " {reason}"

COURIER_OFFER_P2P_TEMPLATE = (
    "🆕 P2P-заказ #{order_id}\n"
    "Забрать (А): {pickup_address}\n"
    "Доставить (Б): {dropoff_address}\n"
    "Что: {description}\n\n"
    "⏱ Успейте принять в течение {offer_timeout_sec} сек."
)

# --- Day 4: оферта (ТЗ §7.0.1) -----------------------------------------------

OFFER_TEXT = (
    "📋 Как работает сервис\n\n"
    "Это некоммерческая информационная платформа. Мы помогаем вам найти "
    "человека (курьера), готового выполнить ваше поручение — съездить в "
    "выбранное заведение и привезти заказ, а также посетить заданную "
    "точку, забрать и доставить по указанному адресу то, что требуется "
    "доставить.\n\n"
    "Нажимая «Принимаю», вы подтверждаете, что:\n"
    "1. Вы даёте поручение курьеру самостоятельно\n"
    "2. Оплату за товар и вознаграждение курьеру вы передаёте ему напрямую "
    "(наличными)\n"
    "3. Платформа не является стороной расчётов и предоставляется бесплатно\n"
    "4. Платформа оставляет за собой право отказать в доставке произвольных "
    "вещей (нелегальные, крупногабаритные и иные — на усмотрение Платформы)"
)
OFFER_ACCEPT_BUTTON = "✅ Принимаю"
OFFER_FULL_TEXT_BUTTON = "📄 Полный текст"
OFFER_BACK_BUTTON = "← Назад"
# Полный (развёрнутый) текст — ТЗ не даёт готовой формулировки, только
# краткую версию выше; это разумное разворачивание тех же трёх пунктов
# в более формальный вид, см. CHECKLIST.md.
OFFER_FULL_TEXT = (
    "📄 Полный текст оферты\n\n"
    "Настоящий текст является предложением использования Telegram-бота "
    "«DeliveryTR» (далее — Платформа).\n\n"
    "1. Модель работы\n"
    "Платформа — некоммерческая информационная площадка, которая помогает "
    "пользователю найти независимого исполнителя (курьера), готового по "
    "поручению пользователя посетить выбранное заведение, приобрести и "
    "доставить заказанные товары, а также посетить заданную точку, "
    "забрать и доставить по указанному адресу иные вещи по поручению "
    "пользователя.\n\n"
    "2. Правовая природа отношений\n"
    "Оформляя заказ через Платформу, пользователь лично поручает курьеру "
    "совершить указанные действия (модель «поручения»). Платформа не "
    "является продавцом товаров, не оказывает услуг доставки "
    "самостоятельно и не выступает стороной в отношениях между "
    "пользователем и курьером.\n\n"
    "3. Расчёты\n"
    "Оплата стоимости товара и вознаграждение курьеру передаются "
    "пользователем курьеру напрямую, наличными, в момент получения "
    "заказа. Платформа не участвует в денежных расчётах и не получает "
    "вознаграждения ни от пользователя, ни от курьера.\n\n"
    "4. Ограничения на доставку\n"
    "Платформа оставляет за собой право отказать в приёме поручения на "
    "доставку произвольных вещей — в частности, нелегальных, "
    "крупногабаритных или иных, отказ в приёме которых Платформа сочтёт "
    "обоснованным по своему усмотрению.\n\n"
    "5. Бесплатность\n"
    "Использование Платформы для пользователя бесплатно.\n\n"
    "6. Согласие\n"
    "Нажимая «Принимаю», пользователь подтверждает, что ознакомился с "
    "условиями настоящей оферты и согласен с ними."
)

# --- Day 4: часы работы сервиса (ТЗ §6, §10.5) -------------------------------
# Блокируют именно ОФОРМЛЕНИЕ (кнопку чекаута), не показ каталога — см.
# docstring services/service_hours.py.

SERVICE_LAST_ORDER_PASSED_MESSAGE = (
    "Приём заказов на сегодня завершён в {last_order}. Уже оформленные "
    "заказы выполняются."
)
SERVICE_CLOSED_MESSAGE = (
    "🌙 Сервис работает с {open} до {close}. Возвращайтесь утром!"
)
SERVICE_MANUALLY_STOPPED_MESSAGE = (
    "🛑 Сервис временно приостановлен, приносим извинения. Попробуйте позже."
)

# --- Day 4: заглушка оплаты (ТЗ §3, §5, §13: PAYMENT_ENABLED) ----------------

PAYMENT_NOT_IMPLEMENTED_MESSAGE = (
    "⚠️ Онлайн-оплата временно недоступна. Пожалуйста, попробуйте позже "
    "или свяжитесь с поддержкой."
)

# --- Day 4: /service_stop, /service_resume (админ, ТЗ §10.5, §11) -----------

ADMIN_SERVICE_STOPPED_ACK = "🛑 Сервис остановлен. Новые заказы не принимаются."
ADMIN_SERVICE_RESUMED_ACK = "✅ Сервис возобновлён, работает по расписанию."

# --- Day 4: ошибка записи заказа ---------------------------------------------

ORDER_SAVE_FAILED_MESSAGE = (
    "😔 Не удалось сохранить заказ — техническая проблема на нашей стороне. "
    "Попробуйте нажать «Подтвердить» ещё раз."
)

# --- Day 5: курьерский модуль (ТЗ §8) ----------------------------------------

# Оферта курьера (по аналогии с клиентской, ТЗ §7.0.1, но своя — ТЗ не даёт
# готовой формулировки для курьеров, это адаптация той же модели
# «поручения» под роль курьера, согласовано с пользователем в чате)
COURIER_OFFER_TEXT = (
    "📋 Условия работы курьером\n\n"
    "Вы регистрируетесь как независимый исполнитель (курьер) на платформе "
    "«DeliveryTR».\n\n"
    "Работая через бота, вы подтверждаете, что:\n"
    "1. Вы принимаете заказы добровольно, по своему усмотрению — нет "
    "обязательства брать каждое предложение\n"
    "2. Вы самостоятельно забираете заказ у заведения/точки и доставляете "
    "его по указанному адресу\n"
    "3. Оплату за товар и своё вознаграждение вы получаете напрямую от "
    "клиента, наличными, при передаче заказа\n"
    "4. Платформа не является вашим работодателем, не гарантирует "
    "минимальный доход и не несёт ответственности за качество товара, "
    "действия клиента или заведения\n"
    "5. Вы несёте ответственность за сохранность и своевременную доставку "
    "принятого заказа"
)

# /shift_on, /shift_off (ТЗ §8.1) — и команда, и персистентная
# reply-кнопка-переключатель дают один и тот же эффект
COURIER_NOT_REGISTERED_MESSAGE = (
    "Вы не зарегистрированы как курьер. Обратитесь к администратору и "
    "назовите этот ID: {telegram_id}"
)
COURIER_SHIFT_ON_ACK = "🟢 Вы на смене. Ждите заказы."
COURIER_SHIFT_OFF_ACK = "🔴 Смена завершена."
COURIER_SHIFT_ON_BUTTON = "🟢 Начать смену"
COURIER_SHIFT_OFF_BUTTON = "🔴 Завершить смену"
ADMIN_COURIER_ON_SHIFT_TEMPLATE = "🟢 {name} на смене."
ADMIN_COURIER_OFF_SHIFT_TEMPLATE = "🔴 {name} завершил смену."

# Диспетчеризация — предложение заказа курьеру (ТЗ §8.2). Без "Реком.
# вознаграждение" и дистанции — оба поля не заполняются ботом для
# order_kind=standard (расчёт вне бота, ТЗ §15; координат мерчанта нет
# в текущей схеме листа «Мерчанты» — только merchant_id/category/name/
# description/is_active/today_confirmed/working_hours). См. CHECKLIST.md.
COURIER_OFFER_TEMPLATE = (
    "🆕 Новый заказ #{order_id}\n\n"
    "Забрать: {merchant_name}\n"
    "{merchant_description}\n\n"
    "Доставить: {delivery_address}\n\n"
    "Сумма заказа: {total_try} ₺\n\n"
    "⏱ Успейте принять в течение {offer_timeout_sec} сек."
)
COURIER_TAKE_ORDER_BUTTON = "✅ Взять"
COURIER_ORDER_ALREADY_TAKEN_MESSAGE = "Этот заказ уже принят другим курьером."
# Живой баг из тестирования: раньше кнопка «Взять» оставалась визуально
# активной у ВСЕХ курьеров и после offer_timeout (никто не принял) —
# функционально не опасно (повторное нажатие корректно отклонялось),
# но вводило в заблуждение. См. clear_expired_offer_for_all_couriers.
COURIER_OFFER_EXPIRED_MESSAGE = "⏱ Время на принятие заказа истекло — предложение больше не активно."
COURIER_ORDER_TAKEN_BY_YOU_TEMPLATE = "✅ Вы взяли заказ #{order_id}!"

ADMIN_NO_COURIERS_ON_SHIFT_TEMPLATE = (
    "⚠️ Заказ #{order_id}: сейчас 0 курьеров на смене. Решите вручную."
)

# --- Day 6: тайм-ауты застрявших заказов (ТЗ §9, таблица порогов) ----------
# Тексты — дословно по таблице ТЗ, где она их даёт явно (никто не взял,
# взял-но-не-забрал, забрал-но-не-доставил).
ADMIN_NO_ONE_ACCEPTED_TEMPLATE = "⚠️ Никто не взял #{order_id}"
ADMIN_PICKUP_TIMEOUT_TEMPLATE = (
    "⏰ #{order_id}: курьер {courier_name} взял {pickup_timeout_min} мин назад, "
    "но не забрал у мерчанта. Проверьте"
)
ADMIN_DELIVERY_TIMEOUT_TEMPLATE = (
    "⏰ #{order_id}: заказ в пути {delivery_timeout_min} мин, ещё не доставлен. Проверьте"
)

ADMIN_COURIER_ASSIGNED_TEMPLATE = "🚚 #{order_id} взял курьер {courier_name}"
CLIENT_ORDER_ASSIGNED_TEMPLATE = (
    "🚚 Курьер {courier_name} принял ваш заказ «{items}» ({merchant_name})."
)

# Активный заказ курьера — управление этапами (ТЗ §8.3)
COURIER_ACTIVE_ORDER_TEMPLATE = (
    "📦 Активный заказ #{order_id} — {merchant_name} → {delivery_address}"
)
COURIER_PICKED_UP_BUTTON = "📦 Забрал заказ"
COURIER_DELIVERED_BUTTON = "✅ Доставил"
COURIER_PROBLEM_BUTTON = "⚠️ Проблема на точке"

COURIER_PICKED_UP_ACK = "Отмечено: забрали заказ."
COURIER_DELIVERED_ACK = "Отмечено: заказ доставлен. Спасибо за работу!"
COURIER_DELIVERY_THANKS_MESSAGE = (
    "🎉 Спасибо за доставку! Теперь вы можете брать новые заказы."
)
COURIER_PROBLEM_ACK = "Понял, поставил заказ на паузу. Администратор уведомлён."
COURIER_ORDER_PAUSED_MESSAGE = "⏸ Заказ на паузе, ожидайте решения администратора."
COURIER_ORDER_NOT_FOUND_MESSAGE = (
    "Не нашли этот заказ — похоже, кнопка устарела. Если заказ ещё "
    "актуален, откройте самую свежую карточку «Активный заказ»."
)
COURIER_TECHNICAL_ERROR_MESSAGE = (
    "Техническая заминка на нашей стороне — попробуйте нажать ещё раз "
    "через несколько секунд."
)
COURIER_MUST_PICK_UP_FIRST_MESSAGE = "Сначала нужно нажать «📦 Забрал заказ»."

ADMIN_ORDER_PICKED_UP_TEMPLATE = "📦 #{order_id} забран"
ADMIN_ORDER_DELIVERED_TEMPLATE = "🎉 #{order_id} доставлен"
ADMIN_MERCHANT_PROBLEM_TEMPLATE = (
    "🚨 #{order_id}: курьер сообщает о проблеме у мерчанта [{merchant_name}]. "
    "Курьер: {courier_name}, {courier_phone}. Заказ на паузе."
)

CLIENT_ORDER_PICKED_UP_TEMPLATE = "🛵 Курьер {courier_name} ({courier_phone}) везёт ваш заказ"
CLIENT_ORDER_DELIVERED_TEMPLATE = (
    "🎉 Спасибо, что выбрали наш сервис! Будем рады видеть вас снова."
)
CLIENT_ORDER_PROBLEM_TEMPLATE = (
    "😔 Возникла заминка с вашим заказом — разбираемся, скоро сообщим подробности."
)

# --- Day 7: поддержка (ТЗ §7.5, §11) -----------------------------------------

SUPPORT_BUTTON = "❓ Вопрос"
SUPPORT_QUESTION_PROMPT = "Опишите ваш вопрос — сообщение сразу уйдёт администратору."
SUPPORT_QUESTION_SUBMITTED_ACK = "✅ Вопрос передан администратору. Ответ придёт прямо сюда."
SUPPORT_FLOOD_LIMIT_MESSAGE_TEMPLATE = (
    "Вы уже отправили несколько вопросов подряд — подождите, пожалуйста, "
    "{minutes} мин, прежде чем написать ещё раз. Если это срочно, опишите "
    "всё в одном сообщении, администратор увидит его."
)

ADMIN_SUPPORT_QUESTION_TEMPLATE = (
    "❓ ВОПРОС от {contact}\n\n{question}\n\n/reply_{user_id} [ответ]   ← ответить"
)
ADMIN_REPLY_SENT_ACK = "✅ Ответ отправлен клиенту."
ADMIN_REPLY_EMPTY_MESSAGE = "Текст ответа не может быть пустым — /reply_{user_id} [ваш ответ]."
ADMIN_REPLY_DELIVERY_FAILED_MESSAGE = (
    "Не удалось доставить ответ клиенту {user_id} — возможно, он заблокировал бота."
)
CLIENT_SUPPORT_REPLY_TEMPLATE = "💬 Ответ от поддержки:\n\n{text}"

# --- Day 8: полный список админ-команд (ТЗ §11) ------------------------------

# /orders, /order_042
ADMIN_ORDERS_HEADER = "📋 Активные заказы:"
ADMIN_ORDERS_EMPTY = "Активных заказов нет."
ADMIN_ORDERS_ROW_TEMPLATE = "#{order_id} · {order_kind} · {status} · {summary}"
ADMIN_ORDER_NOT_FOUND_TEMPLATE = "Заказ #{order_id} не найден."
ADMIN_ORDER_CARD_HEADER_TEMPLATE = (
    "📦 Заказ #{order_id}\n\nТип: {order_kind}\nСтатус: {status}\nСоздан: {timestamp_created}"
)
ADMIN_ORDER_CARD_CLIENT_LINE_TEMPLATE = "Клиент: {contact}"
ADMIN_ORDER_CARD_MERCHANT_LINE_TEMPLATE = "Мерчант: {merchant_name}"
ADMIN_ORDER_CARD_ITEMS_LINE_TEMPLATE = "Состав: {admin_notes}"
ADMIN_ORDER_CARD_ADDRESS_LINE_TEMPLATE = "Адрес: {delivery_address}"
ADMIN_ORDER_CARD_P2P_LINE_TEMPLATE = "Откуда (А): {pickup_address}\nКуда (Б): {dropoff_address}"
ADMIN_ORDER_CARD_COURIER_LINE_TEMPLATE = "Курьер: {courier_name} ({courier_phone})"
ADMIN_ORDER_CARD_NO_COURIER_LINE = "Курьер: не назначен"

# /assign_042_[courier_id]
ADMIN_ASSIGN_COURIER_NOT_FOUND_TEMPLATE = "Курьер {courier_id} не найден."
ADMIN_ASSIGN_ACK_TEMPLATE = "✅ Заказ #{order_id} назначен курьеру {courier_name}."

# /cancel_042 [причина]
ADMIN_CANCEL_ACK_TEMPLATE = "Заказ #{order_id} отменён."
CLIENT_ORDER_CANCELLED_TEMPLATE = "К сожалению, заказ #{order_id} отменён.{reason_suffix}"
CANCELLED_REASON_SUFFIX_TEMPLATE = " Причина: {reason}"

# /deliver_042
ADMIN_DELIVER_ACK_TEMPLATE = "Заказ #{order_id} принудительно закрыт как доставленный."

# /day_start
ADMIN_DAY_START_HEADER = "☀️ Утреннее подтверждение мерчантов:"
ADMIN_DAY_START_EMPTY = "Нет активных мерчантов (is_active=TRUE) для подтверждения."
ADMIN_DAY_START_CONFIRM_BUTTON = "✓ Подтвердить"
ADMIN_DAY_START_DISABLE_BUTTON = "✗ Отключить на сегодня"
ADMIN_DAY_START_MERCHANT_CONFIRMED_TEMPLATE = "✅ {name} подтверждён на сегодня."
ADMIN_DAY_START_MERCHANT_DISABLED_TEMPLATE = "🚫 {name} отключён на сегодня."

# /enable_merchant_[id], /toggle_merchant_[id]
ADMIN_MERCHANT_NOT_FOUND_TEMPLATE = "Мерчант {merchant_id} не найден."
ADMIN_MERCHANT_ENABLED_TEMPLATE = "✅ {name} подтверждён на сегодня (today_confirmed = TRUE)."
ADMIN_MERCHANT_TOGGLED_ACTIVE_TEMPLATE = "{name}: is_active теперь {value}."

# /toggle_item_[item_id]
ADMIN_ITEM_NOT_FOUND_TEMPLATE = "Позиция {item_id} не найдена."
ADMIN_ITEM_TOGGLED_TEMPLATE = "{name}: is_available теперь {value}."

# /couriers
ADMIN_COURIERS_HEADER = "🛵 Курьеры:"
ADMIN_COURIERS_EMPTY = "Курьеров пока нет."
ADMIN_COURIERS_ROW_TEMPLATE = "{courier_id} · {name} · {shift_label} · {active_label}"
ADMIN_COURIER_SHIFT_ON_LABEL = "на смене"
ADMIN_COURIER_SHIFT_OFF_LABEL = "не на смене"
ADMIN_COURIER_ACTIVE_LABEL = "активен"
ADMIN_COURIER_INACTIVE_LABEL = "деактивирован"

# /add_courier (интерактивно)
ADMIN_ADD_COURIER_NAME_PROMPT = "Введите имя курьера:"
ADMIN_ADD_COURIER_PHONE_PROMPT = "Введите телефон курьера:"
ADMIN_ADD_COURIER_TELEGRAM_ID_PROMPT = "Введите Telegram ID курьера (число):"
ADMIN_ADD_COURIER_INVALID_TELEGRAM_ID = "Telegram ID должен быть числом. Попробуйте ещё раз:"
ADMIN_ADD_COURIER_DONE_TEMPLATE = "✅ Курьер {name} добавлен (courier_id={courier_id})."

# /toggle_courier_[id]
ADMIN_COURIER_NOT_FOUND_TEMPLATE = "Курьер {courier_id} не найден."
ADMIN_COURIER_TOGGLED_TEMPLATE = "{name}: is_active теперь {value}."

# /stats
ADMIN_STATS_HEADER_TEMPLATE = "📊 Сводка за сегодня ({date}):"
ADMIN_STATS_NO_ORDERS = "Сегодня заказов ещё не было."
ADMIN_STATS_STATUS_LINE_TEMPLATE = "{status}: {count}"
ADMIN_STATS_AVG_TIME_HEADER = "\nСреднее время этапов:"
ADMIN_STATS_AVG_ASSIGN_LINE_TEMPLATE = "Создан → назначен: {minutes:.1f} мин"
ADMIN_STATS_AVG_PICKUP_LINE_TEMPLATE = "Назначен → забрал: {minutes:.1f} мин"
ADMIN_STATS_AVG_DELIVERY_LINE_TEMPLATE = "Забрал → доставлен: {minutes:.1f} мин"
ADMIN_STATS_CONVERSION_LINE_TEMPLATE = "\nКонверсия (доставлено/создано): {percent:.0f}%"

# /broadcast [текст]
ADMIN_BROADCAST_EMPTY_MESSAGE = "Текст рассылки не может быть пустым — /broadcast [текст]."
ADMIN_BROADCAST_CONFIRM_TEMPLATE = "Разослать этот текст {count} клиентам?\n\n{text}"
ADMIN_BROADCAST_CONFIRM_BUTTON = "✅ Разослать"
ADMIN_BROADCAST_CANCEL_BUTTON = "Отмена"
ADMIN_BROADCAST_CANCELLED_ACK = "Рассылка отменена."
ADMIN_BROADCAST_DONE_TEMPLATE = "✅ Рассылка завершена: доставлено {sent}, не удалось {failed}."
CLIENT_BROADCAST_PREFIX = "📢 "
