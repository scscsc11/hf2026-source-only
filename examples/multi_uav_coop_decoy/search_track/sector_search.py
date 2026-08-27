"""Sector-divided search geometry for the 017 cooperative example.

Each UAV is assigned one angular sector of the search area and performs an
expanding scan *within* that sector. This fixes the "all UAVs spiral on top
of each other" problem seen with the single-centre spiral in 016's
``search_strategies.spiral_next_waypoint``: every UAV used its own (nearly
coincident) take-off point as the spiral centre with the same angular
speed, so the fleet collapsed onto a single curve.

Geometry (v2 — fast expanding scan)
------------------------------------
- A single ``base`` point anchors the whole search area (the area centre).
- The full circle [0, 360) is split into ``n_uavs`` contiguous sectors.
- UAV ``i`` owns sector ``[i*step, (i+1)*step)`` where ``step = 360/n_uavs``.
- **Phase 1 (initial expansion)**: radius grows quickly from
  ``initial_radius`` to ``search_radius`` over ``expand_time`` seconds.
  The UAV starts at its sector midpoint bearing and sweeps outward,
  covering ground fast. The bearing oscillates (triangle wave) within
  the sector at ``sector_angular_speed_dps``.
- **Phase 2 (full-radius sweep)**: once at ``search_radius``, the UAV
  continues sweeping its sector at full radius, ensuring the outer ring
  is re-covered. The radius oscillates slightly (±radius_dither) to
  avoid repeatedly covering the exact same arc.
- ``initial_radius`` defaults to a fraction of ``search_radius`` so the
  UAV doesn't waste time scanning the very centre (which all 3 UAVs
  would overlap on). Instead it starts at a meaningful offset.
- The waypoint is converted to lat/lon via ``destination_point`` using the
  spherical-Earth model already used by ``geometry.haversine_m``.

All angles are in degrees, bearings are clockwise from north (navigational
convention), matching ``geometry.bearing_deg``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Reuse the same spherical-Earth radius as geometry.haversine_m so the
# forward/inverse transforms are consistent (R must match).
from examples.uav_search_track_car.search_track.geometry import (
    EARTH_RADIUS_M, bearing_deg, haversine_m,
)


@dataclass
class SectorSearchParams:
    base_lat: float
    base_lon: float
    base_alt: float
    search_radius: float
    # Seconds for the radius to grow from initial_radius -> search_radius.
    # Shorter = faster expansion but coarser coverage of inner rings.
    expand_time: float = 30.0
    # Sector sweep angular speed (deg/s). The bearing oscillates inside the
    # UAV's sector at this rate (triangle wave -> constant turn rate).
    sector_angular_speed_dps: float = 40.0
    # Starting radius fraction (0..1 of search_radius). Avoids wasting time
    # scanning the very centre where all UAVs overlap. 0.15 means start at
    # 15% of search_radius (e.g. 120m of 800m).
    initial_radius_frac: float = 0.15
    # Radius dither amplitude (fraction of search_radius) in phase 2.
    # Prevents the UAV from repeatedly tracing the exact same arc.
    radius_dither_frac: float = 0.08
    # Legacy field kept for backward compat with tests that set
    # search_sweep_time. When expand_time is not explicitly set, this
    # value is used as expand_time.
    search_sweep_time: float = 90.0


def destination_point(
    lat: float, lon: float, bearing_deg_: float, dist_m: float
) -> tuple[float, float]:
    """Spherical-Earth forward transform: (lat, lon) + bearing + distance.

    Inverse of ``haversine_m``/``bearing_deg``. Uses the same
    ``EARTH_RADIUS_M`` so the round-trip (destination_point then
    haversine_m/bearing_deg) is self-consistent to numerical precision.
    """
    if dist_m <= 0.0:
        return lat, lon
    delta = dist_m / EARTH_RADIUS_M  # angular distance (radians)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing_deg_)
    cos_d, sin_d = math.cos(delta), math.sin(delta)
    phi2 = math.asin(
        math.sin(phi1) * cos_d + math.cos(phi1) * sin_d * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * sin_d * math.cos(phi1),
        cos_d - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), ((math.degrees(lam2) + 540.0) % 360.0) - 180.0


def _triangle_wave(phase: float) -> float:
    """Triangle wave in [0, 1] with period 1. ``phase`` is taken mod 1.

    Rising 0 -> 1 on [0, 0.5), falling 1 -> 0 on [0.5, 1)."""
    p = phase - math.floor(phase)  # [0, 1)
    return 2.0 * p if p < 0.5 else 2.0 * (1.0 - p)


def sector_bearing(t: float, uav_index: int, n_uavs: int,
                   angular_speed_dps: float) -> float:
    """Absolute bearing (deg, [0,360)) for UAV ``uav_index`` of ``n_uavs``.

    Each UAV owns sector ``[lo, hi)`` where ``step = 360/n_uavs``. Its
    bearing oscillates inside that sector as a triangle wave whose phase
    advances at ``angular_speed_dps`` deg/s.
    """
    if n_uavs <= 0:
        n_uavs = 1
    step = 360.0 / n_uavs
    lo = (uav_index % n_uavs) * step
    hi = lo + step
    # Distance swept along the sector in degrees since t=0.
    swept = angular_speed_dps * t
    # Map the swept distance (a 1-D coordinate growing at angular_speed) onto
    # the sector via a triangle wave of period 2*step (down + up).
    frac = _triangle_wave(swept / (2.0 * step))
    return lo + frac * (hi - lo)


def sector_radius(t: float, p: SectorSearchParams) -> float:
    """Radius (m) at time ``t``.

    Phase 1 (t < expand_time): linear growth from initial_radius to
    search_radius — fast expansion to cover the area quickly.
    Phase 2 (t >= expand_time): search_radius with a small dither to
    avoid repeatedly tracing the same arc.
    """
    r_min = p.search_radius * p.initial_radius_frac
    if p.expand_time <= 0.0:
        return p.search_radius
    if t < p.expand_time:
        # Linear expansion from r_min to search_radius.
        frac = t / p.expand_time
        return r_min + (p.search_radius - r_min) * frac
    # Phase 2: full radius with dither.
    dither_amp = p.search_radius * p.radius_dither_frac
    # Slow dither cycle (~20s period) so the UAV doesn't just retrace.
    dither = dither_amp * math.sin(2.0 * math.pi * t / 20.0)
    return max(r_min, p.search_radius + dither)


def sector_waypoint(
    t: float, p: SectorSearchParams, uav_index: int, n_uavs: int
) -> tuple[float, float, float]:
    """Compute (lat, lon, alt) search waypoint for UAV ``uav_index``.

    ``uav_index`` is 0-based within the fleet of ``n_uavs``; the caller
    (run.py) assigns indices in sorted-uid order so each UAV gets a stable,
    deterministic sector.
    """
    brng = sector_bearing(t, uav_index, n_uavs, p.sector_angular_speed_dps)
    r = sector_radius(t, p)
    if r < 1.0:
        r = 1.0  # avoid a zero-radius waypoint exactly on the centre
    lat, lon = destination_point(p.base_lat, p.base_lon, brng, r)
    return lat, lon, p.base_alt


def point_bearing_from(lat: float, lon: float,
                       base_lat: float, base_lon: float) -> float:
    """Bearing from base -> point. Helper for in-sector assertions."""
    return bearing_deg(base_lat, base_lon, lat, lon)


def point_radius_from(lat: float, lon: float,
                      base_lat: float, base_lon: float) -> float:
    """Ground distance (m) from base -> point. Helper for assertions."""
    return haversine_m(base_lat, base_lon, lat, lon)
