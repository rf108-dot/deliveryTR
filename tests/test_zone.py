from __future__ import annotations

from services.zone import haversine_distance_km, is_point_in_zone

# Координаты центра Антальи (примерно) — используются как ZONE_CENTER по
# умолчанию в config.py.
ANTALYA_CENTER_LAT = 36.8841
ANTALYA_CENTER_LON = 30.7056


def test_haversine_distance_zero_for_identical_points():
    assert haversine_distance_km(ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON, ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON) == 0.0


def test_haversine_distance_known_pair_istanbul_ankara():
    """
    Стамбул (41.0082, 28.9784) — Анкара (39.9334, 32.8597): реальное
    расстояние по прямой ~ 350 км. Проверяем с разумным допуском —
    формула не должна давать грубо неверный результат (например, спутав
    градусы с радианами).
    """
    distance = haversine_distance_km(41.0082, 28.9784, 39.9334, 32.8597)
    assert 340 < distance < 360


def test_haversine_distance_is_symmetric():
    d1 = haversine_distance_km(36.90, 30.70, 36.85, 30.75)
    d2 = haversine_distance_km(36.85, 30.75, 36.90, 30.70)
    assert abs(d1 - d2) < 1e-9


def test_point_at_center_is_in_zone():
    assert is_point_in_zone(
        ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON, ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON, radius_km=15
    )


def test_point_just_inside_radius_is_in_zone():
    # ~0.05 градуса широты ≈ 5.5 км — заведомо меньше радиуса 15 км
    assert is_point_in_zone(
        ANTALYA_CENTER_LAT + 0.05, ANTALYA_CENTER_LON, ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON, radius_km=15
    )


def test_point_far_outside_radius_is_not_in_zone():
    # Анкара — заведомо далеко за пределами 15-километровой зоны Антальи
    assert not is_point_in_zone(39.9334, 32.8597, ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON, radius_km=15)


def test_point_exactly_at_radius_boundary_is_in_zone():
    """Проверка границы: расстояние == radius_km должно проходить (<=, не <)."""
    # Двигаемся строго на север на известное расстояние (по долготе не смещаемся)
    # 1 градус широты ≈ 111.32 км -> для 15 км нужно ~0.13471 градуса
    delta_lat = 15.0 / 111.32
    lat = ANTALYA_CENTER_LAT + delta_lat
    distance = haversine_distance_km(lat, ANTALYA_CENTER_LON, ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON)
    # используем ИМЕННО вычисленную дистанцию как радиус — граничный случай
    assert is_point_in_zone(lat, ANTALYA_CENTER_LON, ANTALYA_CENTER_LAT, ANTALYA_CENTER_LON, radius_km=distance)
