"""
Точка входа бота.

Отвечает только за wiring: сборка конфигурации, клиентов внешних сервисов,
роутеров, health-check при старте и graceful shutdown. Бизнес-логика живёт
в handlers/ и services/ — см. ТЗ §16 «Структура репозитория» и требование
Day 0 "не размещай бизнес-логику в bot.py".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from redis.asyncio import Redis

from config import get_settings
from handlers import (
    admin,
    cart,
    catalog,
    courier,
    custom_order,
    fallback,
    order,
    p2p,
    payment,
    start,
    support,
)
from scheduler import create_scheduler
from services.geocoding import GeocodingAdapter
from services.health_monitor import PERIODIC_HEALTH_CHECK_JOB_ID
from services.health_monitor import register_dependencies as register_health_check_dependencies
from services.health_monitor import run_periodic_health_check
from services.sheets import SheetsClient
from services.timeouts import register_dependencies as register_timeout_dependencies
from handlers.custom_order import register_dependencies as register_custom_order_dependencies

logger = logging.getLogger(__name__)


class StartupHealthCheckError(RuntimeError):
    """Одна или несколько внешних зависимостей недоступны при старте."""


def setup_logging(level: str) -> None:
    """Логирование в stdout (см. требование Day 0)."""
    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # Библиотеки третьих сторон обычно излишне многословны на DEBUG/INFO уровне.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def run_startup_health_checks(
    redis_client: Redis,
    sheets_client: SheetsClient,
    geocoding_adapter: GeocodingAdapter,
) -> None:
    """
    Проверяет доступность Redis и Google Sheets, инициализацию Google Maps
    adapter. При любой ошибке кидает StartupHealthCheckError с деталями по
    каждой упавшей проверке — приложение должно упасть при старте (см.
    критерий готовности Day 0), а не запуститься в частично рабочем виде.
    """
    logger.info("Запуск startup health-check...")
    errors: list[str] = []

    try:
        pong = await redis_client.ping()
        if not pong:
            errors.append("Redis: PING не вернул True")
        else:
            logger.info("Health-check OK: Redis")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Redis: {exc!r}")

    try:
        await sheets_client.health_check()
        logger.info("Health-check OK: Google Sheets (Меню + Заказы)")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Google Sheets: {exc!r}")

    try:
        geocoding_adapter.health_check()
        logger.info("Health-check OK: Google Maps adapter инициализирован")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Google Maps: {exc!r}")

    if errors:
        details = "\n  - ".join(errors)
        raise StartupHealthCheckError(
            f"Startup health-check провалился:\n  - {details}"
        )

    logger.info("Startup health-check пройден полностью")


def build_dispatcher(storage: RedisStorage) -> Dispatcher:
    """Собирает Dispatcher и подключает все роутеры (см. ТЗ §16)."""
    dp = Dispatcher(storage=storage)

    # Реализованы: start, catalog, cart, order (Day 0-3).
    dp.include_router(start.router)
    dp.include_router(catalog.router)
    dp.include_router(cart.router)
    dp.include_router(order.router)

    # Каркасы роутеров без хендлеров — будут наполнены в следующие дни.
    dp.include_router(custom_order.router)
    dp.include_router(p2p.router)
    dp.include_router(courier.router)
    dp.include_router(support.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)

    # ВАЖНО: строго последним — "страховочный" ответ на любое сообщение,
    # не попавшее ни в один хендлер выше (Day 4 hotfix, живой фидбэк).
    # Если переставить его раньше — он перехватит сообщения, которые
    # должны были достаться более специфичным хендлерам.
    dp.include_router(fallback.router)

    return dp


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    logger.info("Инициализация клиентов внешних сервисов...")
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Дружелюбное описание команд в меню (значок ≡ рядом с полем ввода
    # или автодополнение при наборе "/") — по запросу пользователя:
    # сам текст кнопки "Start" при первом открытии бота задаётся Telegram
    # и не настраивается нами, но описание команд в меню — да.
    # Day 5 hotfix: добавлены /shift_on и /help — живой фидбэк показал,
    # что без явной подсказки курьер не понимал, с чего начать (путал
    # /start с курьерским входом).
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="🍽 Заказать доставку"),
            BotCommand(command="shift_on", description="🛵 Я курьер, выйти на смену"),
            BotCommand(command="help", description="❓ Как пользоваться ботом"),
        ]
    )
    # Описание бота — показывается на самом первом (ещё пустом) экране
    # чата, до какого-либо взаимодействия, прямо над автоматической
    # кнопкой "Start" — по живому фидбэку: не все сразу понимают, что
    # нужно сделать, чтобы начать заказ, И что курьер входит иначе
    # (/shift_on, не /start).
    await bot.set_my_description(
        description=(
            "👋 Бот доставки в Анталии.\n\n"
            "🍽 Хотите заказать — нажмите «Start» ниже (или кнопку ≡ рядом "
            "с полем ввода).\n"
            "🛵 Вы курьер — отправьте /shift_on."
        )
    )
    await bot.set_my_short_description(short_description="🍽 Доставка еды и не только — Анталия")
    redis_client: Redis = Redis.from_url(settings.REDIS_URL, decode_responses=False)
    storage = RedisStorage(redis=redis_client)
    sheets_client = SheetsClient.from_service_account_info(
        service_account_info=settings.google_service_account_info,
        menu_spreadsheet_id=settings.SHEETS_MENU_ID,
        orders_spreadsheet_id=settings.SHEETS_ORDERS_ID,
        cache_ttl_seconds=settings.CACHE_TTL_SECONDS,
    )
    geocoding_adapter = GeocodingAdapter(api_key=settings.GOOGLE_MAPS_API_KEY)
    task_scheduler = create_scheduler(settings)

    # Требование: при ошибке env или внешней зависимости — падать при
    # старте с понятной ошибкой, а не запускать polling частично рабочим.
    await run_startup_health_checks(redis_client, sheets_client, geocoding_adapter)

    # Day 6: реестры зависимостей для заданий планировщика — задания
    # RedisJobStore хранятся через pickle, а Bot/SheetsClient/Settings не
    # picklable, поэтому сами объекты передаются не как аргументы задания,
    # а регистрируются один раз здесь и читаются в момент выполнения (см.
    # services/timeouts.py, services/health_monitor.py).
    register_timeout_dependencies(bot, sheets_client, settings)
    register_health_check_dependencies(
        bot, redis_client, sheets_client, geocoding_adapter, settings
    )
    register_custom_order_dependencies(bot, settings, redis_client)

    dp = build_dispatcher(storage)
    task_scheduler.add_job(
        run_periodic_health_check,
        trigger="interval",
        minutes=30,
        id=PERIODIC_HEALTH_CHECK_JOB_ID,
        replace_existing=True,
    )
    task_scheduler.start()
    logger.info("Планировщик запущен: тайм-ауты застрявших заказов + health-check каждые 30 мин")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler для части сигналов.
            signal.signal(sig, lambda *_args: stop_event.set())

    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            settings=settings,
            sheets=sheets_client,
            geocoding=geocoding_adapter,
            redis=redis_client,
            scheduler=task_scheduler,
        ),
        name="bot-polling",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal-wait")

    logger.info("Бот запущен, начат long polling")
    done, _pending = await asyncio.wait(
        {polling_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if stop_task in done and not polling_task.done():
        logger.info("Получен сигнал остановки, завершаю polling...")
        polling_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling_task
    elif polling_task in done:
        # Polling завершился сам (например, из-за неотловленного исключения).
        exc = polling_task.exception()
        if exc is not None:
            logger.error("Polling завершился с ошибкой: %r", exc)

    logger.info("Graceful shutdown: закрываю соединения...")
    await bot.session.close()
    await redis_client.aclose()
    task_scheduler.shutdown(wait=False)
    logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except StartupHealthCheckError as exc:
        logging.getLogger(__name__).critical(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        pass
