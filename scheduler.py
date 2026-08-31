"""
Планировщик тайм-аутов застрявших заказов и авто-эскалаций (ТЗ §9).

Day 6: RedisJobStore — задания планировщика хранятся в Redis, а не в
памяти процесса, поэтому переживают рестарт бота (ТЗ §18 "Перезапуск
бота с активными таймерами"). При старте APScheduler сам подгружает
все несработавшие задания из Redis и продолжает их отсчёт (а для тех,
чьё время уже прошло, пока бот был выключен — сработает почти сразу,
в пределах misfire_grace_time, см. ниже).

Сами задания (offer/pickup/delivery timeout) регистрируются НЕ здесь, а
точечно — вызовами schedule_*_timeout() из services/timeouts.py прямо в
хендлерах, в момент соответствующего перехода статуса (см.
services/dispatch.py send_offer_to_couriers, handlers/courier.py
on_take_order/on_picked_up/on_delivered). Этот модуль отвечает только
за инфраструктуру планировщика — создание, job store, периодический
health-check (ТЗ §12.2).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from apscheduler.jobstores.redis import RedisJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Settings

logger = logging.getLogger(__name__)

# Щедрый запас на случай долгого простоя бота (ноутбук выключен на
# ночь, деплой и т.п.) — лучше эскалировать поздно, чем никогда не
# эскалировать вовсе. См. живой фидбэк Day 5 про сетевые сбои после
# выхода из сна — тот же класс "бот был недоступен какое-то время".
_MISFIRE_GRACE_TIME_SECONDS = 6 * 60 * 60  # 6 часов


def _redis_connect_kwargs(redis_url: str) -> dict:
    """RedisJobStore (APScheduler) использует синхронный redis-py под
    капотом и принимает параметры подключения по отдельности, не как
    единую строку URL — разбираем REDIS_URL вручную."""
    parsed = urlparse(redis_url)
    db = 0
    path = parsed.path.lstrip("/") if parsed.path else ""
    if path:
        try:
            db = int(path)
        except ValueError:
            db = 0
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 6379,
        "db": db,
        "password": parsed.password,
    }


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Создаёт (но не запускает) планировщик с Redis-backed job store и
    таймзоной из конфига."""
    jobstore = RedisJobStore(**_redis_connect_kwargs(settings.REDIS_URL))
    scheduler = AsyncIOScheduler(
        timezone=settings.TIMEZONE,
        jobstores={"default": jobstore},
        job_defaults={"misfire_grace_time": _MISFIRE_GRACE_TIME_SECONDS},
    )
    return scheduler
