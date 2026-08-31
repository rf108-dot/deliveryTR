"""
Периодический health-check (ТЗ §12.2) — раз в 30 минут проверяет
доступность Redis, Google Sheets, Google Maps. В отличие от
run_startup_health_checks (bot.py, роняет процесс при старте, если
что-то недоступно) — здесь бот уже работает, поэтому сбой НЕ должен
ронять процесс, только уведомлять Админа.

Тот же паттерн реестра зависимостей, что в services/timeouts.py —
задание планировщика (RedisJobStore) должно ссылаться на picklable
функцию верхнего уровня, а Bot/SheetsClient/GeocodingAdapter/Settings
не picklable; регистрируются один раз при старте, читаются в момент
выполнения задания.
"""

from __future__ import annotations

import logging

from services.geocoding import GeocodingAdapter
from services.notifications import notify_admins
from services.sheets import SheetsClient

logger = logging.getLogger(__name__)

PERIODIC_HEALTH_CHECK_JOB_ID = "periodic_health_check"

_registry: dict[str, object] = {}


def register_dependencies(
    bot, redis_client, sheets: SheetsClient, geocoding: GeocodingAdapter, settings
) -> None:
    _registry["bot"] = bot
    _registry["redis"] = redis_client
    _registry["sheets"] = sheets
    _registry["geocoding"] = geocoding
    _registry["settings"] = settings


async def run_periodic_health_check() -> None:
    bot = _registry["bot"]
    redis_client = _registry["redis"]
    sheets: SheetsClient = _registry["sheets"]  # type: ignore[assignment]
    geocoding: GeocodingAdapter = _registry["geocoding"]  # type: ignore[assignment]
    settings = _registry["settings"]

    errors: list[str] = []

    try:
        pong = await redis_client.ping()
        if not pong:
            errors.append("Redis: PING не вернул True")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Redis: {exc!r}")

    try:
        await sheets.health_check()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Google Sheets: {exc!r}")

    try:
        geocoding.health_check()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Google Maps: {exc!r}")

    if errors:
        details = "\n".join(errors)
        logger.error("Периодический health-check провалился:\n%s", details)
        await notify_admins(bot, settings, f"🚨 Health-check провалился:\n{details}")
    else:
        logger.info("Периодический health-check: всё ОК")
