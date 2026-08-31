"""
Проверка геозоны доставки (радиус/полигон, формула гаверсинуса).

Day 3 (ТЗ §3A): вариант А — радиус от центральной точки
(ZONE_CENTER_LAT, ZONE_CENTER_LON) + ZONE_RADIUS_KM, без внешних API.
Используется и для обычных заказов (точка клиента), и для P2P (обе
точки, Day 7).
"""

from __future__ import annotations

import math

_EARTH_RADIUS_KM = 6371.0088  # средний радиус Земли (IUGG)


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по прямой между двумя точками на сфере, в километрах."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_KM * c


def is_point_in_zone(
    lat: float,
    lon: float,
    center_lat: float,
    center_lon: float,
    radius_km: float,
) -> bool:
    """Точка внутри зоны, если расстояние от центра ≤ радиуса (ТЗ §3A, вариант А)."""
    return haversine_distance_km(lat, lon, center_lat, center_lon) <= radius_km


# TODO (на будущее, вне плана 9 дней MVP): вариант Б — полигон вдоль
# побережья Анталии, is_point_in_polygon(lat, lon, polygon) — см. ТЗ §3A.
