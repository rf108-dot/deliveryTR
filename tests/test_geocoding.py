from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.geocoding import GeocodeResult, GeocodingAdapter

# Правдоподобный формат ключа (начинается с "AIza"), чтобы конструктор
# googlemaps.Client не споткнулся на валидации формата.
_FAKE_API_KEY = "AIzaSyFAKEKEYFORTESTS0000000000000"


@pytest.fixture
def adapter() -> GeocodingAdapter:
    instance = GeocodingAdapter(api_key=_FAKE_API_KEY, request_timeout_seconds=5.0)
    instance._client = MagicMock()  # подменяем реальный googlemaps.Client
    return instance


@pytest.mark.asyncio
async def test_reverse_geocode_returns_formatted_address(adapter):
    adapter._client.reverse_geocode.return_value = [
        {"formatted_address": "Lara Cad. 12, Antalya, Turkey"}
    ]

    address = await adapter.reverse_geocode(36.88, 30.70)

    assert address == "Lara Cad. 12, Antalya, Turkey"
    adapter._client.reverse_geocode.assert_called_once_with((36.88, 30.70))


@pytest.mark.asyncio
async def test_reverse_geocode_returns_none_on_empty_results(adapter):
    adapter._client.reverse_geocode.return_value = []

    address = await adapter.reverse_geocode(36.88, 30.70)

    assert address is None


@pytest.mark.asyncio
async def test_reverse_geocode_returns_none_on_exception(adapter):
    adapter._client.reverse_geocode.side_effect = Exception("boom")

    address = await adapter.reverse_geocode(36.88, 30.70)

    assert address is None


@pytest.mark.asyncio
async def test_geocode_returns_coordinates(adapter):
    adapter._client.geocode.return_value = [
        {"geometry": {"location": {"lat": 36.9, "lng": 30.8}}}
    ]

    result = await adapter.geocode("Lara Cad. 12, Antalya")

    assert result == GeocodeResult(lat=36.9, lon=30.8, partial_match=False)
    adapter._client.geocode.assert_called_once_with("Lara Cad. 12, Antalya")


@pytest.mark.asyncio
async def test_geocode_flags_partial_match(adapter):
    """
    Регресс-тест на живой фидбэк: несуществующий адрес ("Liman 1234
    sokak 34") Google "откатывает" на реальный распознанный район и
    помечает результат partial_match=True — это должно долетать до
    вызывающего кода, а не молча теряться.
    """
    adapter._client.geocode.return_value = [
        {
            "geometry": {"location": {"lat": 36.9, "lng": 30.8}},
            "partial_match": True,
        }
    ]

    result = await adapter.geocode("Liman 1234 sokak 34, Antalya")

    assert result.partial_match is True


@pytest.mark.asyncio
async def test_geocode_returns_none_on_empty_results(adapter):
    adapter._client.geocode.return_value = []

    result = await adapter.geocode("не существующий адрес")

    assert result is None


@pytest.mark.asyncio
async def test_geocode_returns_none_on_exception(adapter):
    adapter._client.geocode.side_effect = Exception("boom")

    result = await adapter.geocode("Lara Cad. 12, Antalya")

    assert result is None
