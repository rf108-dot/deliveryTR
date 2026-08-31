from __future__ import annotations

import base64
import json
from typing import Any

import pytest


def _valid_service_account_b64() -> str:
    """Минимальный валидный (по форме) service account JSON, base64."""
    payload = {
        "type": "service_account",
        "client_email": "bot@antalya-bot.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


@pytest.fixture
def valid_settings_kwargs() -> dict[str, Any]:
    """
    Полный набор валидных значений для Settings — как если бы все
    обязательные и часть необязательных env-переменных были корректно
    заполнены. Тесты переопределяют/удаляют отдельные ключи для проверки
    конкретных сценариев валидации.
    """
    return {
        "_env_file": None,  # не читать .env файл при тестах — изоляция
        "BOT_TOKEN": "123456:FAKE-TOKEN-FOR-TESTS",
        "ADMIN_TELEGRAM_IDS": "111111111,222222222",
        "REDIS_URL": "redis://localhost:6379/0",
        "GOOGLE_SERVICE_ACCOUNT_JSON": _valid_service_account_b64(),
        "SHEETS_MENU_ID": "menu-spreadsheet-id",
        "SHEETS_ORDERS_ID": "orders-spreadsheet-id",
        "GOOGLE_MAPS_API_KEY": "fake-maps-api-key",
        "WELCOME_IMAGE_URL": "https://example.com/banner.png",
        "TIMEZONE": "Europe/Istanbul",
        "SERVICE_OPEN": "10:00",
        "SERVICE_CLOSE": "21:00",
        "LAST_ORDER": "20:15",
        "OFFER_TIMEOUT_SEC": 60,
        "PICKUP_TIMEOUT_MIN": 15,
        "DELIVERY_TIMEOUT_MIN": 40,
        "CUSTOM_ORDER_TIMEOUT_MIN": 15,
        "PAYMENT_ENABLED": False,
        "PAYMENT_TIMEOUT_MIN": 15,
        "SUPPORT_FLOOD_WINDOW_SEC": 600,
        "SUPPORT_FLOOD_MAX_MESSAGES": 3,
        "P2P_REWARD_MULTIPLIER": 2,
        "P2P_REVIEW_TIMEOUT_MIN": 15,
        "ZONE_CENTER_LAT": 36.8841,
        "ZONE_CENTER_LON": 30.7056,
        "ZONE_RADIUS_KM": 15,
        "CACHE_TTL_SECONDS": 300,
        "MAX_ITEMS_PER_TYPE": 10,
        "LOG_LEVEL": "INFO",
    }
