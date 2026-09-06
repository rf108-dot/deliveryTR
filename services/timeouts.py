"""
Тайм-ауты «застрявших заказов» и авто-эскалация (ТЗ §9).

Планировщик (APScheduler + Redis-backed job store, см. scheduler.py)
ставит таймер на каждом критичном этапе заказа. Если заказ вовремя
переходит на следующий статус — таймер отменяется. Если нет — по
истечении порога (из конфига) заказ эскалируется Админу, событие
логируется как stuck_order_escalated (единое имя события для ВСЕХ
эскалаций этого раздела, как того требует ТЗ).

Три таймера:
  offer_timeout      OFFER_TIMEOUT_SEC     offered → no_courier, если
                                            никто не нажал «Взять»
  pickup_timeout      PICKUP_TIMEOUT_MIN    курьер взял (assigned), но
                                            не нажал «Забрал» вовремя
  delivery_timeout    DELIVERY_TIMEOUT_MIN  курьер забрал (picked_up),
                                            но не нажал «Доставил» вовремя

ВАЖНО про архитектуру: APScheduler с RedisJobStore ХРАНИТ задания в
Redis через pickle — сама функция задания и её АРГУМЕНТЫ должны быть
picklable. Объекты Bot/SheetsClient/Settings НЕ picklable (внутри —
aiohttp-сессии, SSL-контексты и т.п.), поэтому передавать их напрямую
как аргументы задания нельзя. Вместо этого — простой модульный реестр
(register_dependencies), заполняемый ОДИН РАЗ при старте бота; сами
функции заданий принимают только простые строковые аргументы (order_id)
и берут bot/sheets/settings из реестра В МОМЕНТ ВЫПОЛНЕНИЯ, а не при
постановке в очередь. Это стандартный паттерн для APScheduler с
персистентными job store и объектами, которые не переживают pickle.
Живой баг из тестирования: postановка таймера использовала "наивный"
datetime.now() (без явной привязки к часовому поясу) — планировщик
интерпретировал это время как время в поясе settings.TIMEZONE
("Europe/Istanbul"), но РЕАЛЬНЫЕ системные часы компьютера, где запущен
бот, могут быть выставлены на ДРУГОЙ пояс (например, у тестировщика
локально было +05:00, а не +03:00 Стамбула) — расхождение в 2 часа
означало, что таймер не "не срабатывал", а тихо откладывался почти на
2 часа позже задуманного, что на практике неотличимо от "вообще не
сработал" в рамках короткого живого теста. Фикс: везде используется
datetime.now(timezone.utc) — однозначная метка времени независимо от
того, в каком поясе выставлены системные часы конкретной машины.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis

from config import Settings
from services.notifications import notify_admins
from services.sheets import SheetsClient
from texts.ru import (
    ADMIN_DELIVERY_TIMEOUT_TEMPLATE,
    ADMIN_NO_ONE_ACCEPTED_TEMPLATE,
    ADMIN_P2P_REVIEW_TIMEOUT_REMINDER_TEMPLATE,
    ADMIN_PICKUP_TIMEOUT_TEMPLATE,
)

logger = logging.getLogger(__name__)

# Реестр зависимостей для заданий планировщика — см. docstring модуля.
# Заполняется register_dependencies() один раз при старте бота (bot.py).
_registry: dict[str, object] = {}


def register_dependencies(bot, sheets: SheetsClient, settings: Settings, redis: Redis) -> None:
    """Вызывается один раз при старте бота (bot.py) — до scheduler.start(),
    чтобы задания, восстановленные из Redis сразу при старте (если их
    run_date уже прошёл, misfire_grace_time позволит им выполниться),
    имели доступ к живым bot/sheets/settings/redis.

    redis добавлен вместе с _check_offer_timeout — для очистки кнопки
    «Взять» у всех курьеров, когда никто не успел (живой баг из
    тестирования, см. clear_expired_offer_for_all_couriers в
    services/dispatch.py)."""
    _registry["bot"] = bot
    _registry["sheets"] = sheets
    _registry["settings"] = settings
    _registry["redis"] = redis


def _get_bot():
    return _registry["bot"]


def _get_sheets() -> SheetsClient:
    return _registry["sheets"]  # type: ignore[return-value]


def _get_settings() -> Settings:
    return _registry["settings"]  # type: ignore[return-value]


def _get_redis() -> Redis:
    return _registry["redis"]  # type: ignore[return-value]


def _job_id(kind: str, order_id: str) -> str:
    return f"{kind}:{order_id}"


def _cancel(scheduler: AsyncIOScheduler, kind: str, order_id: str) -> None:
    """Отменяет ранее поставленный таймер. Безопасно вызывать, даже если
    задания уже нет (выполнилось/никогда не ставилось) — это не ошибка,
    а нормальный путь (например, cancel_pickup_timeout вызывается и из
    on_picked_up при своевременном переходе, и превентивно из on_problem,
    где задания может и не быть, если "Проблема" нажата ДО "Забрал")."""
    try:
        scheduler.remove_job(_job_id(kind, order_id))
    except JobLookupError:
        pass


# ------------------------------------------------------------------ #
# offer_timeout: offered → no_courier, если никто не взял за
# OFFER_TIMEOUT_SEC (ТЗ §9, §8.2)
# ------------------------------------------------------------------ #


def schedule_offer_timeout(
    scheduler: AsyncIOScheduler, order_id: str, offer_timeout_sec: int
) -> None:
    scheduler.add_job(
        _check_offer_timeout,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=offer_timeout_sec),
        args=[order_id],
        id=_job_id("offer_timeout", order_id),
        replace_existing=True,
    )


def cancel_offer_timeout(scheduler: AsyncIOScheduler, order_id: str) -> None:
    _cancel(scheduler, "offer_timeout", order_id)


async def _check_offer_timeout(order_id: str) -> None:
    sheets = _get_sheets()
    order = await sheets.get_order(order_id)
    if order is None or order.get("status") != "offered":
        # Заказ уже сдвинулся дальше (взяли) или не найден — таймер не
        # успел отмениться вовремя (гонка на границе), но действовать
        # уже поздно/не нужно.
        return

    await sheets.update_order_fields(order_id, {"status": "no_courier"})
    logger.info("stuck_order_escalated: order_id=%s reason=offer_timeout", order_id)
    await sheets.append_event(
        event_type="stuck_order_escalated",
        actor_role="system",
        actor_id="scheduler",
        order_id=order_id,
        details="offer_timeout",
    )
    await notify_admins(
        _get_bot(), _get_settings(), ADMIN_NO_ONE_ACCEPTED_TEMPLATE.format(order_id=order_id)
    )

    # Локальный импорт — избегаем циклической зависимости: dispatch.py
    # уже импортирует schedule_offer_timeout ИЗ этого модуля (timeouts.py),
    # поэтому импорт dispatch.py на уровне модуля здесь создал бы цикл.
    from services.dispatch import clear_expired_offer_for_all_couriers

    await clear_expired_offer_for_all_couriers(_get_bot(), _get_redis(), order_id)


# ------------------------------------------------------------------ #
# pickup_timeout: курьер взял (assigned), но не нажал «Забрал» за
# PICKUP_TIMEOUT_MIN (ТЗ §9)
# ------------------------------------------------------------------ #


def schedule_pickup_timeout(
    scheduler: AsyncIOScheduler, order_id: str, pickup_timeout_min: int
) -> None:
    scheduler.add_job(
        _check_pickup_timeout,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=pickup_timeout_min),
        args=[order_id],
        id=_job_id("pickup_timeout", order_id),
        replace_existing=True,
    )


def cancel_pickup_timeout(scheduler: AsyncIOScheduler, order_id: str) -> None:
    _cancel(scheduler, "pickup_timeout", order_id)


async def _check_pickup_timeout(order_id: str) -> None:
    sheets = _get_sheets()
    settings = _get_settings()
    order = await sheets.get_order(order_id)
    if order is None or order.get("status") != "assigned":
        return

    logger.info("stuck_order_escalated: order_id=%s reason=pickup_timeout", order_id)
    await sheets.append_event(
        event_type="stuck_order_escalated",
        actor_role="system",
        actor_id="scheduler",
        order_id=order_id,
        details="pickup_timeout",
    )
    await notify_admins(
        _get_bot(),
        settings,
        ADMIN_PICKUP_TIMEOUT_TEMPLATE.format(
            order_id=order_id,
            courier_name=order.get("courier_name", ""),
            pickup_timeout_min=settings.PICKUP_TIMEOUT_MIN,
        ),
    )


# ------------------------------------------------------------------ #
# delivery_timeout: курьер забрал (picked_up), но не нажал «Доставил»
# за DELIVERY_TIMEOUT_MIN (ТЗ §9)
# ------------------------------------------------------------------ #


def schedule_delivery_timeout(
    scheduler: AsyncIOScheduler, order_id: str, delivery_timeout_min: int
) -> None:
    scheduler.add_job(
        _check_delivery_timeout,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=delivery_timeout_min),
        args=[order_id],
        id=_job_id("delivery_timeout", order_id),
        replace_existing=True,
    )


def cancel_delivery_timeout(scheduler: AsyncIOScheduler, order_id: str) -> None:
    _cancel(scheduler, "delivery_timeout", order_id)


async def _check_delivery_timeout(order_id: str) -> None:
    sheets = _get_sheets()
    settings = _get_settings()
    order = await sheets.get_order(order_id)
    if order is None or order.get("status") != "picked_up":
        return

    logger.info("stuck_order_escalated: order_id=%s reason=delivery_timeout", order_id)
    await sheets.append_event(
        event_type="stuck_order_escalated",
        actor_role="system",
        actor_id="scheduler",
        order_id=order_id,
        details="delivery_timeout",
    )
    await notify_admins(
        _get_bot(),
        settings,
        ADMIN_DELIVERY_TIMEOUT_TEMPLATE.format(
            order_id=order_id, delivery_timeout_min=settings.DELIVERY_TIMEOUT_MIN
        ),
    )


# ------------------------------------------------------------------ #
# p2p_review_reminder: P2P-заказ ждёт решения Админа дольше
# P2P_REVIEW_TIMEOUT_MIN (ТЗ §7.6.7)
#
# ВАЖНО, чем этот таймер отличается от трёх выше: он НЕ эскалирует и НЕ
# меняет статус заказа автоматически — только НАПОМИНАЕТ Админу. Цитата
# из ТЗ: "P2P может требовать раздумий, в отличие от нестандартного
# заказа у мерчанта" (там, в handlers/custom_order.py, по такому же
# таймауту происходит авто-отклонение). Поэтому здесь нет перехода в
# no_courier/аналог — только повторный пуш тех же двух команд Админу.
# ------------------------------------------------------------------ #


def schedule_p2p_review_reminder(
    scheduler: AsyncIOScheduler, order_id: str, review_timeout_min: int
) -> None:
    scheduler.add_job(
        _check_p2p_review_reminder,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=review_timeout_min),
        args=[order_id],
        id=_job_id("p2p_review_reminder", order_id),
        replace_existing=True,
    )


def cancel_p2p_review_reminder(scheduler: AsyncIOScheduler, order_id: str) -> None:
    _cancel(scheduler, "p2p_review_reminder", order_id)


async def _check_p2p_review_reminder(order_id: str) -> None:
    sheets = _get_sheets()
    settings = _get_settings()
    order = await sheets.get_order(order_id)
    if order is None or order.get("status") != "pending_review":
        # Уже одобрен/отклонён (handlers/p2p.py отменяет этот таймер при
        # обеих командах — см. cmd_approve_p2p/cmd_reject_p2p) — но
        # держим и эту проверку как страховку на случай гонки/сбоя отмены.
        return

    await notify_admins(
        _get_bot(),
        settings,
        ADMIN_P2P_REVIEW_TIMEOUT_REMINDER_TEMPLATE.format(
            order_id=order_id, timeout_min=settings.P2P_REVIEW_TIMEOUT_MIN
        ),
    )
