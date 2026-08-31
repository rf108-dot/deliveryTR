"""
Централизованная конфигурация бота.

Все настройки читаются из переменных окружения (см. `.env.example` и
раздел 13 ТЗ «Конфигурация (переменные окружения)»). Валидация выполняется
через Pydantic Settings: при отсутствии обязательной переменной или при
некорректном значении приложение обязано упасть при старте с понятным
сообщением об ошибке (см. Day 0, критерий готовности).

Использование:
    from config import get_settings
    settings = get_settings()
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import time
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_hhmm(value: str, field_name: str) -> time:
    """Парсит строку "HH:MM" в datetime.time, иначе кидает ValueError."""
    try:
        hours_str, minutes_str = value.strip().split(":")
        parsed = time(hour=int(hours_str), minute=int(minutes_str))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f'{field_name}="{value}" должно быть временем в формате "HH:MM", '
            f'например "10:00"'
        ) from exc
    return parsed


class Settings(BaseSettings):
    """Схема и валидация переменных окружения. См. ТЗ, раздел 13."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Telegram --------------------------------------------------------
    BOT_TOKEN: str = Field(..., min_length=1, description="Токен бота из BotFather")
    ADMIN_TELEGRAM_IDS: str = Field(
        ..., description="Telegram ID администраторов через запятую, напр. 111,222"
    )

    # --- Redis (FSM storage + таймеры) -----------------------------------
    REDIS_URL: str = Field(..., description="Redis для FSM и таймеров")

    # --- Google Sheets -----------------------------------------------------
    GOOGLE_SERVICE_ACCOUNT_JSON: str = Field(
        ..., description="Service account JSON, закодированный в base64"
    )
    SHEETS_MENU_ID: str = Field(..., min_length=1)
    SHEETS_ORDERS_ID: str = Field(..., min_length=1)

    # --- Google Maps ---------------------------------------------------
    GOOGLE_MAPS_API_KEY: str = Field(..., min_length=1)

    # --- Внешний вид ---------------------------------------------------
    WELCOME_IMAGE_URL: Optional[str] = Field(
        default=None, description="URL приветственного баннера для /start"
    )

    # --- Часовой пояс и часы работы --------------------------------------
    TIMEZONE: str = Field(default="Europe/Istanbul")
    SERVICE_OPEN: str = Field(default="10:00")
    SERVICE_CLOSE: str = Field(default="21:00")
    LAST_ORDER: str = Field(default="20:15")

    # --- Тайм-ауты (сек/мин) --------------------------------------------
    OFFER_TIMEOUT_SEC: int = Field(default=60, gt=0)
    PICKUP_TIMEOUT_MIN: int = Field(default=15, gt=0)
    DELIVERY_TIMEOUT_MIN: int = Field(default=40, gt=0)
    CUSTOM_ORDER_TIMEOUT_MIN: int = Field(default=15, gt=0)

    # --- Оплата (заглушка на MVP, см. ТЗ §3 и §5) ------------------------
    PAYMENT_ENABLED: bool = Field(default=False)
    PAYMENT_TIMEOUT_MIN: int = Field(default=15, gt=0)
    PAPARA_BUSINESS_ID: Optional[str] = Field(default=None)
    IYZICO_API_KEY: Optional[str] = Field(default=None)

    # --- Поддержка (антифлуд) -------------------------------------------
    SUPPORT_FLOOD_WINDOW_SEC: int = Field(default=600, gt=0)
    SUPPORT_FLOOD_MAX_MESSAGES: int = Field(default=3, gt=0)

    # --- P2P --------------------------------------------------------------
    P2P_REWARD_MULTIPLIER: float = Field(default=2, gt=0)
    # Таймаут модерации P2P-заказа Админом (ТЗ §7.6.7) — В ОТЛИЧИЕ от
    # CUSTOM_ORDER_TIMEOUT_MIN (нестандартный заказ у мерчанта), по
    # истечении НЕ авто-отклоняет заказ, а только напоминает Админу —
    # P2P может требовать раздумий (см. ТЗ: "P2P может требовать
    # раздумий, в отличие от нестандартного заказа у мерчанта").
    P2P_REVIEW_TIMEOUT_MIN: int = Field(default=15, gt=0)

    # --- Геозона (ТЗ §3A) --------------------------------------------------
    ZONE_CENTER_LAT: float = Field(default=36.8841, ge=-90, le=90)
    ZONE_CENTER_LON: float = Field(default=30.7056, ge=-180, le=180)
    ZONE_RADIUS_KM: float = Field(default=15, gt=0)

    # --- Прочее -------------------------------------------------------
    CACHE_TTL_SECONDS: int = Field(default=300, gt=0)
    MAX_ITEMS_PER_TYPE: int = Field(default=10, gt=0)

    # -- Log level (не описано в ТЗ явно, но нужно для logging в stdout) --
    LOG_LEVEL: str = Field(default="INFO")

    # ---------------------------------------------------------------- #
    # Валидаторы полей
    # ---------------------------------------------------------------- #

    @field_validator("ADMIN_TELEGRAM_IDS")
    @classmethod
    def _validate_admin_ids_raw(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "ADMIN_TELEGRAM_IDS не может быть пустым: нужен хотя бы один "
                "администратор для критичных уведомлений"
            )
        for chunk in value.split(","):
            chunk = chunk.strip()
            if not chunk.lstrip("-").isdigit():
                raise ValueError(
                    f'ADMIN_TELEGRAM_IDS содержит нечисловой ID: "{chunk}". '
                    f"Ожидается список Telegram ID через запятую, напр. 111,222"
                )
        return value

    @field_validator("REDIS_URL")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError(
                f'REDIS_URL="{value}" должен начинаться с "redis://", '
                f'"rediss://" или "unix://"'
            )
        return value

    @field_validator("GOOGLE_SERVICE_ACCOUNT_JSON")
    @classmethod
    def _validate_service_account_json(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON должен быть валидной base64-строкой "
                "(закодированный service account JSON)"
            ) from exc
        try:
            info = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON после base64-декодирования "
                "не является валидным JSON"
            ) from exc
        required_keys = {"client_email", "private_key", "token_uri"}
        missing = required_keys - info.keys()
        if missing:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON: в service account JSON отсутствуют "
                f"обязательные поля: {sorted(missing)}"
            )
        return value

    @field_validator("WELCOME_IMAGE_URL")
    @classmethod
    def _validate_welcome_image_url(cls, value: Optional[str]) -> Optional[str]:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(
                f'WELCOME_IMAGE_URL="{value}" должен быть http(s) URL'
            )
        return value

    @field_validator("TIMEZONE")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f'TIMEZONE="{value}" неизвестна для системы (IANA tz)') from exc
        return value

    # ---------------------------------------------------------------- #
    # Кросс-полевые проверки
    # ---------------------------------------------------------------- #

    @model_validator(mode="after")
    def _validate_service_hours_order(self) -> "Settings":
        opens = _parse_hhmm(self.SERVICE_OPEN, "SERVICE_OPEN")
        last_order = _parse_hhmm(self.LAST_ORDER, "LAST_ORDER")
        closes = _parse_hhmm(self.SERVICE_CLOSE, "SERVICE_CLOSE")
        if not (opens < last_order < closes):
            raise ValueError(
                "Часы работы некорректны: ожидается SERVICE_OPEN < LAST_ORDER < "
                f"SERVICE_CLOSE, получено {self.SERVICE_OPEN} < {self.LAST_ORDER} "
                f"< {self.SERVICE_CLOSE}"
            )
        return self

    @model_validator(mode="after")
    def _validate_payment_provider_when_enabled(self) -> "Settings":
        if self.PAYMENT_ENABLED and not (self.IYZICO_API_KEY or self.PAPARA_BUSINESS_ID):
            raise ValueError(
                "PAYMENT_ENABLED=True требует хотя бы одного из: IYZICO_API_KEY "
                "(приоритетно) или PAPARA_BUSINESS_ID"
            )
        return self

    # ---------------------------------------------------------------- #
    # Вычисляемые свойства для удобства использования в остальном коде
    # ---------------------------------------------------------------- #

    @property
    def admin_ids(self) -> list[int]:
        """Список Telegram ID администраторов."""
        return [int(chunk.strip()) for chunk in self.ADMIN_TELEGRAM_IDS.split(",")]

    @property
    def service_open_time(self) -> time:
        return _parse_hhmm(self.SERVICE_OPEN, "SERVICE_OPEN")

    @property
    def service_close_time(self) -> time:
        return _parse_hhmm(self.SERVICE_CLOSE, "SERVICE_CLOSE")

    @property
    def last_order_time(self) -> time:
        return _parse_hhmm(self.LAST_ORDER, "LAST_ORDER")

    @property
    def google_service_account_info(self) -> dict:
        """Декодированный service account JSON (dict), готовый для google-auth."""
        decoded = base64.b64decode(self.GOOGLE_SERVICE_ACCOUNT_JSON)
        return json.loads(decoded)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Возвращает закешированный экземпляр Settings.

    Использует lru_cache, чтобы .env читался и валидировался один раз за
    процесс. В тестах кеш нужно сбрасывать через get_settings.cache_clear().
    """
    return Settings()
