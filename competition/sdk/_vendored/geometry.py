"""Vendored geometry helpers (from examples/uav_search_track_car/search_track/geometry.py).

Kept here so the competition SDK is self-contained for release — it does
not import anything from ``examples/``.
"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing_deg_: float,
                distance_m: float) -> tuple[float, float]:
    """Great-circle destination point.

    Given a start (lat, lon), an initial bearing in degrees, and a
    distance in metres, return the (lat, lon) of the destination point.
    Spherical great-circle approximation; the C++ geo::GeoUtils::destination
    uses a WGS84 ENU tangent-plane offset, which agrees to sub-metre for the
    short (<km) extrapolations used by predict_target_position.
    """
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing_deg_)
    delta = distance_m / EARTH_RADIUS_M  # angular distance

    phi2 = math.asin(math.sin(phi1) * math.cos(delta)
                     + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2))

    return (math.degrees(phi2),
            (math.degrees(lam2) + 540.0) % 360.0 - 180.0)  # normalize lon
