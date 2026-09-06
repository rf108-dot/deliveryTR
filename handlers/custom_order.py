"""
Нестандартный заказ у мерчанта (order_kind="custom", ТЗ §7.3.4).

Флоу: кнопка [📝 Нет нужного? Написать запрос] на карточке позиции меню
(handlers/catalog.py) → пользователь описывает текстом (+опционально
фото), что хочет заказать → заявка уходит на модерацию Админу
(/confirm_custom_[user_id] / /reject_custom_[user_id] [причина]) →
при подтверждении клиент проходит СТАНДАРТНЫЙ чекаут (handlers/order.py,
§7.4: геолокация → адрес → контакт → итог) с order_kind="custom"; при
отклонении или таймауте CUSTOM_ORDER_TIMEOUT_MIN — уведомление клиенту,
заявка снимается.

Хранение заявки на модерации — Redis, НЕ Sheets: до решения Админа
заказ ещё не существует как сущность (нет order_id, нет адреса/
контакта, нет строки в листе «Заказы» — она появится только после
успешного чекаута, см. handlers/order.py::_confirm_custom_order). Ключ
на user_id — один клиент может иметь только одну активную заявку
одновременно (упрощение MVP: повторная отправка тем же клиентом, пока
первая не решена, тихо перезаписывает первую и её таймер).

Реестр зависимостей планировщика — тот же паттерн, что в
services/timeouts.py (см. его подробный docstring про pickle/
RedisJobStore): свой отдельный маленький реестр, а не общий с
timeouts.py, чтобы модули оставались независимыми друг от друга (тот же
принцип самодостаточности, что и у handlers/courier.py). В отличие от
timeouts.py, сюда ДОПОЛНИТЕЛЬНО нужен redis (не только bot/settings) —
заявка на модерации хранится в Redis, а не в Sheets, и таймаут-джобе
нужен доступ к нему в момент выполнения, а не только при постановке.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis

from config import Settings
from handlers.catalog import CustomOrderEntryCallback
from handlers.order import start_checkout_for_user
from services.sheets import SheetsClient
from states.user_states import CustomOrderStates
from texts.ru import (
    ADMIN_CONFIRM_CUSTOM_NOT_FOUND_MESSAGE,
    ADMIN_CUSTOM_ORDER_CONFIRMED_ACK,
    ADMIN_CUSTOM_ORDER_REJECTED_ACK,
    ADMIN_CUSTOM_ORDER_REVIEW_TEMPLATE,
    ADMIN_CUSTOM_ORDER_TIMEOUT_TEMPLATE,
    ADMIN_REJECT_CUSTOM_REASON_PROMPT,
    CUSTOM_ORDER_CANCEL_BUTTON,
    CUSTOM_ORDER_CANCELLED_ACK,
    CUSTOM_ORDER_CONFIRMED_MESSAGE,
    CUSTOM_ORDER_EMPTY_TEXT_MESSAGE,
    CUSTOM_ORDER_PROMPT,
    CUSTOM_ORDER_REJECTED_REASON_SUFFIX_TEMPLATE,
    CUSTOM_ORDER_REJECTED_TEMPLATE,
    CUSTOM_ORDER_SUBMITTED_ACK,
    CUSTOM_ORDER_TIMEOUT_CLIENT_MESSAGE,
)

logger = logging.getLogger(__name__)

router = Router(name=__name__)

CUSTOM_ORDER_CANCEL_CALLBACK_DATA = "custom_order_cancel"

_PENDING_KEY_TEMPLATE = "custom_order_pending:{user_id}"
# TTL с запасом поверх CUSTOM_ORDER_TIMEOUT_MIN — защитный бэкстоп (RedisJobStore
# переживает рестарт процесса, но не полагаемся ТОЛЬКО на него). Основной
# механизм авто-отклонения — само запланированное задание
# _check_custom_order_timeout ниже, а не истечение этого TTL.
_PENDING_TTL_BUFFER_SEC = 300

# Реестр зависимостей планировщика — см. docstring модуля.
_registry: dict[str, object] = {}


def register_dependencies(bot: Bot, settings: Settings, redis: Redis) -> None:
    """Вызывается один раз при старте бота (bot.py), см. register_dependencies
    в services/timeouts.py — тот же паттерн для отдельного набора заданий."""
    _registry["bot"] = bot
    _registry["settings"] = settings
    _registry["redis"] = redis


def _get_bot() -> Bot:
    return _registry["bot"]  # type: ignore[return-value]


def _get_settings() -> Settings:
    return _registry["settings"]  # type: ignore[return-value]


def _get_redis() -> Redis:
    return _registry["redis"]  # type: ignore[return-value]


def _pending_key(user_id: int | str) -> str:
    return _PENDING_KEY_TEMPLATE.format(user_id=user_id)


def _job_id(user_id: int | str) -> str:
    return f"custom_order_timeout:{user_id}"


def _contact_label(username: str | None, user_id: int) -> str:
    return f"@{username}" if username else f"ID {user_id}"


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CUSTOM_ORDER_CANCEL_BUTTON, callback_data=CUSTOM_ORDER_CANCEL_CALLBACK_DATA
                )
            ]
        ]
    )


async def _notify_admins_of_request(
    bot: Bot, settings: Settings, text: str, photo_file_id: str
) -> None:
    """
    Локальный аналог services.notifications.notify_admins — тот не
    поддерживает фото (при заявке текст+фото важно показать Админу оба
    сразу, чтобы решение о подтверждении/отклонении было информированным).
    Та же философия best-effort: сбой доставки одному админу не должен
    прерывать рассылку остальным.
    """
    for admin_id in settings.admin_ids:
        try:
            if photo_file_id:
                await bot.send_photo(admin_id, photo_file_id, caption=text)
            else:
                await bot.send_message(admin_id, text)
        except TelegramAPIError as exc:
            logger.warning("Не удалось отправить заявку на модерацию админу %s: %s", admin_id, exc)


# ---------------------------------------------------------------------- #
# Вход: кнопка на карточке позиции меню (handlers/catalog.py)
# ---------------------------------------------------------------------- #


@router.callback_query(CustomOrderEntryCallback.filter())
async def on_custom_order_entry(
    query: CallbackQuery,
    callback_data: CustomOrderEntryCallback,
    sheets: SheetsClient,
    state: FSMContext,
) -> None:
    merchants = await sheets.get_merchants()
    merchant = next((m for m in merchants if m.merchant_id == callback_data.merchant_id), None)
    merchant_name = merchant.name if merchant else callback_data.merchant_id

    await state.update_data(
        custom_order_merchant_id=callback_data.merchant_id,
        custom_order_merchant_name=merchant_name,
    )
    await state.set_state(CustomOrderStates.waiting_for_description)
    await query.message.answer(
        CUSTOM_ORDER_PROMPT.format(merchant_name=merchant_name),
        reply_markup=_cancel_keyboard(),
    )
    await query.answer()


@router.callback_query(
    StateFilter(CustomOrderStates.waiting_for_description),
    F.data == CUSTOM_ORDER_CANCEL_CALLBACK_DATA,
)
async def on_custom_order_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(custom_order_merchant_id=None, custom_order_merchant_name=None)
    await query.message.edit_text(CUSTOM_ORDER_CANCELLED_ACK)
    await query.answer()


@router.message(StateFilter(CustomOrderStates.waiting_for_description))
async def on_custom_order_description_received(
    message: Message,
    state: FSMContext,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
) -> None:
    text = (message.text or message.caption or "").strip()
    photo_file_id = message.photo[-1].file_id if message.photo else ""

    if not text:
        await message.answer(CUSTOM_ORDER_EMPTY_TEXT_MESSAGE)
        return

    data = await state.get_data()
    merchant_id = data.get("custom_order_merchant_id")
    merchant_name = data.get("custom_order_merchant_name") or merchant_id
    user = message.from_user
    user_id = user.id if user else None
    if merchant_id is None or user_id is None:
        # Защитный случай (не должен наступать — состояние ставится
        # только вместе с merchant_id в on_custom_order_entry) — не
        # падаем молча, просто сбрасываем состояние.
        await state.set_state(None)
        return

    pending = {
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "description": text,
        "photo_file_id": photo_file_id,
        "username": user.username or "",
    }
    ttl_seconds = settings.CUSTOM_ORDER_TIMEOUT_MIN * 60 + _PENDING_TTL_BUFFER_SEC
    await redis.set(_pending_key(user_id), json.dumps(pending, ensure_ascii=False), ex=ttl_seconds)

    scheduler.add_job(
        _check_custom_order_timeout,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(minutes=settings.CUSTOM_ORDER_TIMEOUT_MIN),
        args=[str(user_id)],
        id=_job_id(user_id),
        replace_existing=True,
    )

    await state.set_state(None)
    await state.update_data(custom_order_merchant_id=None, custom_order_merchant_name=None)
    await message.answer(CUSTOM_ORDER_SUBMITTED_ACK)

    logger.info("custom_order_submitted: user_id=%s merchant_id=%s", user_id, merchant_id)

    contact = _contact_label(user.username, user_id)
    admin_text = ADMIN_CUSTOM_ORDER_REVIEW_TEMPLATE.format(
        merchant_name=merchant_name, contact=contact, description=text, user_id=user_id
    )
    await _notify_admins_of_request(bot, settings, admin_text, photo_file_id)


# ---------------------------------------------------------------------- #
# Таймаут ожидания решения Админа (CUSTOM_ORDER_TIMEOUT_MIN)
# ---------------------------------------------------------------------- #


async def _check_custom_order_timeout(user_id_str: str) -> None:
    """
    Вызывается планировщиком через CUSTOM_ORDER_TIMEOUT_MIN после
    подачи заявки. Если заявка всё ещё висит в Redis (значит, Админ не
    успел ни подтвердить, ни отклонить) — авто-отклонение + уведомление
    и клиенту, и Админу. Если заявки уже нет (обработана раньше) —
    идемпотентный no-op, как и в _check_offer_timeout из services/timeouts.py.
    """
    bot = _get_bot()
    settings = _get_settings()
    redis = _get_redis()

    pending_raw = await redis.get(_pending_key(user_id_str))
    if pending_raw is None:
        return

    pending = json.loads(pending_raw)
    await redis.delete(_pending_key(user_id_str))

    contact = _contact_label(pending.get("username") or None, int(user_id_str))

    try:
        await bot.send_message(int(user_id_str), CUSTOM_ORDER_TIMEOUT_CLIENT_MESSAGE)
    except TelegramAPIError as exc:
        logger.warning("Не удалось уведомить клиента %s о таймауте заявки: %s", user_id_str, exc)

    await _notify_admins_of_request(
        bot,
        settings,
        ADMIN_CUSTOM_ORDER_TIMEOUT_TEMPLATE.format(
            contact=contact, timeout_min=settings.CUSTOM_ORDER_TIMEOUT_MIN
        ),
        photo_file_id="",
    )
    logger.info("custom_order_timeout: user_id=%s", user_id_str)


# ---------------------------------------------------------------------- #
# Команды Админа: /confirm_custom_[user_id], /reject_custom_[user_id]
# ---------------------------------------------------------------------- #


def _parse_target_user_id(command_prefix: str, text: str) -> tuple[int, str] | None:
    """
    Разбирает "/confirm_custom_301746349" или
    "/reject_custom_301746349 не работаем с этим районом" →
    (301746349, "не работаем с этим районом"). None, если формат не
    распознан (не падаем на кривом вводе Админа — просто игнорируем).
    """
    if not text.startswith(command_prefix):
        return None
    rest = text[len(command_prefix) :]
    parts = rest.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return None
    user_id = int(parts[0])
    reason = parts[1].strip() if len(parts) > 1 else ""
    return user_id, reason


@router.message(F.text.startswith("/confirm_custom_"))
async def cmd_confirm_custom(
    message: Message,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    dispatcher: Dispatcher,
) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    parsed = _parse_target_user_id("/confirm_custom_", message.text or "")
    if parsed is None:
        return
    target_user_id, _reason = parsed

    pending_raw = await redis.get(_pending_key(target_user_id))
    if pending_raw is None:
        await message.answer(ADMIN_CONFIRM_CUSTOM_NOT_FOUND_MESSAGE)
        return
    pending = json.loads(pending_raw)

    try:
        scheduler.remove_job(_job_id(target_user_id))
    except JobLookupError:
        pass
    await redis.delete(_pending_key(target_user_id))

    # Апдейт от Админа — целевое FSM-состояние (клиента, не Админа)
    # собирается вручную через storage/key, а не через инжектируемый
    # `state`, который здесь принадлежал бы Админу. Это документированный
    # способ в aiogram обратиться к состоянию произвольного пользователя
    # вне контекста его собственного апдейта (тут — из админской команды).
    # chat_id совпадает с user_id — бот работает только в приватных чатах.
    target_state = FSMContext(
        storage=dispatcher.storage,
        key=StorageKey(bot_id=bot.id, chat_id=target_user_id, user_id=target_user_id),
    )

    try:
        await bot.send_message(target_user_id, CUSTOM_ORDER_CONFIRMED_MESSAGE)
    except TelegramAPIError as exc:
        logger.warning(
            "Не удалось уведомить клиента %s о подтверждении заявки: %s", target_user_id, exc
        )

    await start_checkout_for_user(
        bot,
        target_user_id,
        target_state,
        order_kind="custom",
        merchant_id=pending["merchant_id"],
        merchant_name=pending["merchant_name"],
        custom_description=pending["description"],
        custom_photo_file_id=pending.get("photo_file_id", ""),
    )

    logger.info(
        "custom_order_confirmed: user_id=%s admin_id=%s", target_user_id, message.from_user.id
    )
    await message.answer(ADMIN_CUSTOM_ORDER_CONFIRMED_ACK)


async def _reject_custom_order(
    message: Message,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    target_user_id: int,
    reason: str,
) -> None:
    """Общая логика отклонения заявки — используется и из cmd_reject_custom
    (причина сразу в команде), и из on_reject_reason_received (причина
    отдельным сообщением после голой команды)."""
    try:
        scheduler.remove_job(_job_id(target_user_id))
    except JobLookupError:
        pass
    await redis.delete(_pending_key(target_user_id))

    reason_suffix = (
        CUSTOM_ORDER_REJECTED_REASON_SUFFIX_TEMPLATE.format(reason=reason) if reason else ""
    )
    try:
        await bot.send_message(
            target_user_id, CUSTOM_ORDER_REJECTED_TEMPLATE.format(reason_suffix=reason_suffix)
        )
    except TelegramAPIError as exc:
        logger.warning(
            "Не удалось уведомить клиента %s об отклонении заявки: %s", target_user_id, exc
        )

    logger.info("custom_order_rejected: user_id=%s reason=%r", target_user_id, reason)
    await message.answer(ADMIN_CUSTOM_ORDER_REJECTED_ACK)


@router.message(F.text.startswith("/reject_custom_"))
async def cmd_reject_custom(
    message: Message,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    state: FSMContext,
) -> None:
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    parsed = _parse_target_user_id("/reject_custom_", message.text or "")
    if parsed is None:
        return
    target_user_id, reason = parsed

    pending_raw = await redis.get(_pending_key(target_user_id))
    if pending_raw is None:
        await message.answer(ADMIN_CONFIRM_CUSTOM_NOT_FOUND_MESSAGE)
        return

    if not reason:
        # Живой фидбэк из тестирования: раньше отказ без причины сразу
        # уходил клиенту шаблонным текстом — ощущалось грубым. Теперь
        # запрашиваем причину и ждём её следующим сообщением (тот же
        # паттерн, что у /reply_ в handlers/support.py).
        await state.update_data(custom_order_reject_target_user_id=target_user_id)
        await state.set_state(CustomOrderStates.waiting_for_reject_reason)
        await message.answer(ADMIN_REJECT_CUSTOM_REASON_PROMPT)
        return

    await state.set_state(None)
    await _reject_custom_order(message, redis, bot, scheduler, target_user_id, reason)


@router.message(StateFilter(CustomOrderStates.waiting_for_reject_reason), F.text)
async def on_reject_reason_received(
    message: Message,
    settings: Settings,
    redis: Redis,
    bot: Bot,
    scheduler: AsyncIOScheduler,
    state: FSMContext,
) -> None:
    """Продолжение cmd_reject_custom — см. docstring
    CustomOrderStates.waiting_for_reject_reason.

    Регистрация ПОСЛЕ cmd_confirm_custom/cmd_reject_custom в этом же
    роутере важна (как и в handlers/support.py): если Админ, ожидая
    причину для одного клиента, вместо этого пришлёт НОВУЮ команду
    "/confirm_custom_[id2]" или "/reject_custom_[id2] ..." — она должна
    попасть в соответствующий cmd_*, а не быть проглочена этим
    хендлером как текст причины для старого target_user_id."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    data = await state.get_data()
    target_user_id = data.get("custom_order_reject_target_user_id")
    if target_user_id is None:
        await state.set_state(None)
        return

    reason = message.text.strip()
    if not reason:
        await message.answer(ADMIN_REJECT_CUSTOM_REASON_PROMPT)
        return

    await state.set_state(None)
    await state.update_data(custom_order_reject_target_user_id=None)
    await _reject_custom_order(message, redis, bot, scheduler, target_user_id, reason)
