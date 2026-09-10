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

import asyncio
import logging

from services.geocoding import GeocodingAdapter
from services.notifications import notify_admins
from services.sheets import SheetsClient

logger = logging.getLogger(__name__)

PERIODIC_HEALTH_CHECK_JOB_ID = "periodic_health_check"

# Живой баг из прод-эксплуатации: единичный тайм-аут Google Sheets API
# (15 сек, без ретрая — см. services/sheets.py::_execute_with_retry)
# дал ложный алерт Админу при полностью здоровой системе — за неделю
# непрерывной работы такое произошло один раз, похоже на случайный
# сетевой затык между Railway и Google API, а не на системную
# проблему. Здесь, В ОТЛИЧИЕ от интерактивных пользовательских
# сценариев (там лишние секунды ожидания — реальная UX-проблема), одна
# повторная попытка через паузу не критична: это фоновая проверка раз
# в 30 минут, несколько лишних секунд никто не заметит, зато отсеивают
# именно такие разовые затыки, оставляя алерты Админу только для
# устойчивых, повторяющихся проблем.
_RETRY_DELAY_SECONDS = 5

_registry: dict[str, object] = {}


def register_dependencies(
    bot, redis_client, sheets: SheetsClient, geocoding: GeocodingAdapter, settings
) -> None:
    _registry["bot"] = bot
    _registry["redis"] = redis_client
    _registry["sheets"] = sheets
    _registry["geocoding"] = geocoding
    _registry["settings"] = settings


async def _run_with_one_retry(check_coro_factory):
    """
    Возвращает результат первой удачной попытки; если первая попытка
    упала с исключением — одна повторная попытка после паузы (см.
    комментарий у _RETRY_DELAY_SECONDS выше). Если и повторная попытка
    упадёт — исключение уходит вызывающему коду как есть, без дальнейших
    попыток (сознательно: одна лишняя попытка отсеивает разовый затык,
    но не должна маскировать по-настоящему устойчивую проблему).
    """
    try:
        return await check_coro_factory()
    except Exception:  # noqa: BLE001 — любая ошибка достойна одной повторной попытки
        logger.debug("Health-check: первая попытка не удалась, повтор через %sс", _RETRY_DELAY_SECONDS)
        await asyncio.sleep(_RETRY_DELAY_SECONDS)
        return await check_coro_factory()


async def run_periodic_health_check() -> None:
    bot = _registry["bot"]
    redis_client = _registry["redis"]
    sheets: SheetsClient = _registry["sheets"]  # type: ignore[assignment]
    geocoding: GeocodingAdapter = _registry["geocoding"]  # type: ignore[assignment]
    settings = _registry["settings"]

    errors: list[str] = []

    try:
        pong = await _run_with_one_retry(redis_client.ping)
        if not pong:
            errors.append("Redis: PING не вернул True")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Redis: {exc!r}")

    try:
        await _run_with_one_retry(sheets.health_check)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Google Sheets: {exc!r}")

    try:
        # Синхронная и чисто локальная проверка (не делает сетевых
        # вызовов — см. GeocodingAdapter.health_check) — ретраить нечего,
        # сетевого затыка тут в принципе не может случиться.
        geocoding.health_check()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Google Maps: {exc!r}")

    if errors:
        details = "\n".join(errors)
        logger.error("Периодический health-check провалился:\n%s", details)
        await notify_admins(bot, settings, f"🚨 Health-check провалился:\n{details}")
    else:
        logger.info("Периодический health-check: всё ОК")
