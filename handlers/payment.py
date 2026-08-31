"""
Заглушка awaiting_payment; Papara webhook / Iyzico callback
(активируется флагом PAYMENT_ENABLED).

Day 4 (ТЗ §3, §5, §13): PAYMENT_ENABLED=False в MVP по умолчанию —
статус awaiting_payment полностью пропускается (см. ТЗ §5: "при
PAYMENT_ENABLED=False пропустить шаг оплаты"). Сама проверка флага
живёт в handlers/order.py (on_order_confirmed) — этому модулю на Day 4
регистрировать нечего.

Если PAYMENT_ENABLED включат без дополнительной разработки —
handlers/order.py явно откажет пользователю понятным сообщением
(PAYMENT_NOT_IMPLEMENTED_MESSAGE в texts/ru.py), а не тихо пропустит
оплату мимо.

Реальная обработка оплаты (Papara webhook / Iyzico callback) требует
HTTP-эндпоинта — у бота его нет, только long polling (см. bot.py).
Добавление HTTP-сервера (aiohttp/FastAPI) параллельно с polling — вне
плана 9 дней MVP, потребуется отдельная работа при включении монетизации.
"""

from __future__ import annotations

from aiogram import Router

router = Router(name=__name__)

# TODO (вне плана 9 дней MVP): Papara webhook / Iyzico callback потребуют
# HTTP-сервера параллельно с long polling ботом.
