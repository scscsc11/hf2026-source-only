"""Helpers shared across the example (geometry)."""
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
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def elevation_deg(distance_m: float, alt_diff_m: float) -> float:
    if distance_m <= 1e-6:
        return 0.0
    return math.degrees(math.atan2(alt_diff_m, distance_m))


def los_angles(
    uav_lat: float,
    uav_lon: float,
    uav_alt: float,
    uav_yaw: float,
    tgt_lat: float,
    tgt_lon: float,
    tgt_alt: float,
) -> tuple[float, float]:
    """Compute body-frame (pan, tilt) for the gimbal to point at the target.

    pan is the relative bearing (target azimuth - host yaw), normalized to
    [-180, 180]. tilt is the elevation angle (negative = looking down)."""
    brg = bearing_deg(uav_lat, uav_lon, tgt_lat, tgt_lon)
    d_h = haversine_m(uav_lat, uav_lon, tgt_lat, tgt_lon)
    elv = elevation_deg(d_h, tgt_alt - uav_alt)
    pan = ((brg - uav_yaw + 540.0) % 360.0) - 180.0
    return pan, elv
