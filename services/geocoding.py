"""
Адаптер Google Maps Geocoding API.

Day 0: инициализация клиента, health-check (без сетевого вызова).
Day 3 (ТЗ §7.4.1): reverse_geocode() — координаты → адрес (после
получения геолокации клиента). geocode() (прямое геокодирование) — не
описано явно в ТЗ, но нужно как решение архитектурного пробела: ТЗ §3A
требует проверять геозону для точки клиента, а при ручном вводе адреса
(без отправки геолокации) координат нет вообще — поэтому текстовый адрес
геокодируется в координаты специально для проверки зоны. Явно
зафиксировано как решение в CHECKLIST.md.

geocode() также возвращает флаг partial_match (Google Geocoding API
"откатывается" на ближайший распознанный уровень, если введённый адрес
не найден точно — например, несуществующая улица распознаётся как
существующий район) — вызывающий код (handlers/order.py) использует его,
чтобы предупредить клиента, а не молча принять неточный адрес.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import googlemaps

logger = logging.getLogger(__name__)


class GeocodingAdapterError(RuntimeError):
    """Google Maps adapter не удалось инициализировать."""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    partial_match: bool


class GeocodingAdapter:
    """Тонкая обёртка над googlemaps.Client."""

    def __init__(self, api_key: str, request_timeout_seconds: float = 15.0) -> None:
        try:
            self._client = googlemaps.Client(key=api_key)
        except Exception as exc:  # noqa: BLE001 — оборачиваем ошибку валидации ключа
            raise GeocodingAdapterError(
                f"Не удалось инициализировать Google Maps adapter: {exc}. "
                f"Проверьте формат GOOGLE_MAPS_API_KEY."
            ) from exc
        # Тот же урок Day 1 (hotfix #2, CHECKLIST.md): не полагаемся на
        # таймаут стороннего транспорта, гарантируем свой через asyncio.wait_for.
        self._request_timeout_seconds = request_timeout_seconds

    def health_check(self) -> None:
        """
        Day 0: подтверждает, что клиент успешно создан (ключ синтаксически
        корректен). Реальный сетевой вызов к Geocoding API сознательно не
        делается при каждом старте, чтобы не расходовать квоту без нужды.
        """
        if self._client is None:  # pragma: no cover — защитный код
            raise GeocodingAdapterError("Google Maps client не инициализирован")
        logger.debug("Google Maps adapter инициализирован")

    async def reverse_geocode(self, lat: float, lon: float) -> str | None:
        """
        Координаты → человекочитаемый адрес (ТЗ §7.4.1). Возвращает None
        при сетевой ошибке/таймауте/отсутствии результата/неожиданной
        структуре ответа — вызывающий код должен предусмотреть fallback
        (например, предложить ввести адрес вручную), а не падать.
        """
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._client.reverse_geocode, (lat, lon)),
                timeout=self._request_timeout_seconds,
            )
            if not results:
                return None
            return results[0].get("formatted_address")
        except asyncio.TimeoutError:
            logger.warning("reverse_geocode: таймаут для (%s, %s)", lat, lon)
            return None
        except Exception as exc:  # noqa: BLE001 — сеть/квота/неожиданный формат ответа
            logger.warning("reverse_geocode: ошибка для (%s, %s): %s", lat, lon, exc)
            return None

    async def geocode(self, address: str) -> GeocodeResult | None:
        """
        Адрес (текст) → координаты + флаг partial_match. Нужно для
        проверки геозоны, когда пользователь ввёл адрес вручную, не
        поделившись геолокацией — см. docstring модуля. Возвращает None
        при ошибке/ненайденном адресе/неожиданной структуре ответа.
        """
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._client.geocode, address),
                timeout=self._request_timeout_seconds,
            )
            if not results:
                return None
            result = results[0]
            location = result["geometry"]["location"]
            return GeocodeResult(
                lat=location["lat"],
                lon=location["lng"],
                partial_match=bool(result.get("partial_match", False)),
            )
        except asyncio.TimeoutError:
            logger.warning("geocode: таймаут для %r", address)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("geocode: ошибка для %r: %s", address, exc)
            return None

