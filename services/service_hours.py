"""
Логика часов работы сервиса, last-order, ручная остановка /service_stop.

Day 4 (ТЗ §6, §10.5): три временных окна (10:00-20:15 / 20:15-21:00 /
21:00-10:00) + приоритет /service_stop над расписанием.

ВАЖНО (архитектурное решение): часы работы блокируют именно ОФОРМЛЕНИЕ
заказа (кнопку чекаута в handlers/order.py), а не показ каталога — ТЗ §6
прямо говорит "Меню можно смотреть" даже когда сервис закрыт. /start и
просмотр категорий/меню НЕ используют этот модуль вообще.

Флаг /service_stop хранится в Redis напрямую (не через FSMContext —
это ГЛОБАЛЬНЫЙ флаг для всех пользователей, а не данные конкретного
пользователя), под ключом SERVICE_STOP_REDIS_KEY.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from config import Settings

# Глобальный (не per-user) ключ в Redis — намеренно не через FSMContext.
SERVICE_STOP_REDIS_KEY = "service:manually_stopped"


class ServiceStatus:
    """Простой набор строковых констант статуса (не Enum — не нужен для 4 значений)."""

    OPEN = "open"
    LAST_ORDER_PASSED = "last_order_passed"
    CLOSED = "closed"
    MANUALLY_STOPPED = "manually_stopped"


async def is_manually_stopped(redis: Redis) -> bool:
    value = await redis.get(SERVICE_STOP_REDIS_KEY)
    # redis_client создан с decode_responses=False (см. bot.py) — значения
    # приходят как bytes, поэтому сравниваем именно с b"1".
    return value == b"1"


async def set_manually_stopped(redis: Redis, stopped: bool) -> None:
    if stopped:
        await redis.set(SERVICE_STOP_REDIS_KEY, "1")
    else:
        await redis.delete(SERVICE_STOP_REDIS_KEY)


def _now_local(settings: Settings) -> dt_time:
    tz = ZoneInfo(settings.TIMEZONE)
    return datetime.now(tz).time()


async def get_service_status(
    settings: Settings, redis: Redis, *, now: dt_time | None = None
) -> str:
    """
    Приоритет /service_stop над расписанием (ТЗ §6, §10.5) — проверяем
    его первым. Если не остановлен вручную — смотрим на текущее время
    относительно SERVICE_OPEN/LAST_ORDER/SERVICE_CLOSE из конфига.

    Параметр `now` — только для тестов (детерминированное время вместо
    реальных часов); в проде вызывающий код его не передаёт.
    """
    if await is_manually_stopped(redis):
        return ServiceStatus.MANUALLY_STOPPED

    current = now if now is not None else _now_local(settings)
    open_time = settings.service_open_time
    last_order_time = settings.last_order_time
    close_time = settings.service_close_time

    if open_time <= current < last_order_time:
        return ServiceStatus.OPEN
    if last_order_time <= current < close_time:
        return ServiceStatus.LAST_ORDER_PASSED
    return ServiceStatus.CLOSED
