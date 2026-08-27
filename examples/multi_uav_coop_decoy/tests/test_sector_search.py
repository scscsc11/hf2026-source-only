"""Tests for the sector-divided search geometry (017).

Verifies the geometry that replaces the single-centre spiral so the fleet
fans out instead of all circling on top of each other.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
EXAMPLE_DIR = HERE.parents[1]
for p in (str(REPO_ROOT), str(EXAMPLE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from search_track.sector_search import (
    SectorSearchParams,
    destination_point,
    point_bearing_from,
    point_radius_from,
    sector_bearing,
    sector_radius,
    sector_waypoint,
)
from examples.uav_search_track_car.search_track.geometry import (
    bearing_deg, haversine_m,
)

BASE = (27.0, 125.0)
PARAMS = SectorSearchParams(
    base_lat=BASE[0], base_lon=BASE[1], base_alt=300.0,
    search_radius=800.0, expand_time=30.0,
    sector_angular_speed_dps=40.0,
    initial_radius_frac=0.15,
    radius_dither_frac=0.08,
)


# ── destination_point round-trip ──────────────────────────────────────────

def test_destination_point_inverse_of_haversine_bearing():
    """destination_point then haversine/bearing should round-trip."""
    brng, dist = 47.0, 650.0
    lat, lon = destination_point(BASE[0], BASE[1], brng, dist)
    back_brg = bearing_deg(BASE[0], BASE[1], lat, lon)
    back_dist = haversine_m(BASE[0], BASE[1], lat, lon)
    assert abs(back_brg - brng) < 1e-6
    assert abs(back_dist - dist) < 1e-3


def test_destination_point_zero_distance_returns_input():
    lat, lon = destination_point(BASE[0], BASE[1], 90.0, 0.0)
    assert lat == BASE[0] and lon == BASE[1]


# ── radius growth ─────────────────────────────────────────────────────────

def test_radius_starts_at_initial_radius_frac():
    """At t=0, radius should be search_radius * initial_radius_frac."""
    R = PARAMS.search_radius
    frac = PARAMS.initial_radius_frac
    assert abs(sector_radius(0.0, PARAMS) - R * frac) < 1e-6


def test_radius_grows_linearly_to_search_radius():
    """Radius grows linearly from initial to search_radius over expand_time."""
    R = PARAMS.search_radius
    frac = PARAMS.initial_radius_frac
    r_min = R * frac
    T = PARAMS.expand_time
    # At t=0: r_min
    assert abs(sector_radius(0.0, PARAMS) - r_min) < 1e-6
    # At t=T/2: halfway between r_min and R
    expected_mid = r_min + (R - r_min) * 0.5
    assert abs(sector_radius(T / 2.0, PARAMS) - expected_mid) < 1e-6
    # At t=T: search_radius
    assert abs(sector_radius(T, PARAMS) - R) < 1e-6


def test_radius_dithers_after_expand_time():
    """After expand_time, radius oscillates around search_radius."""
    R = PARAMS.search_radius
    T = PARAMS.expand_time
    # At t well past expand_time, radius should be close to R (within dither).
    dither_amp = R * PARAMS.radius_dither_frac
    r = sector_radius(T + 10.0, PARAMS)
    assert abs(r - R) <= dither_amp + 1e-6


# ── sector assignment ────────────────────────────────────────────────────

def test_sector_bearing_stays_within_assigned_sector():
    """UAV i's bearing must lie inside [i*step, (i+1)*step)."""
    n = 3
    step = 360.0 / n
    for i in range(n):
        lo = i * step
        hi = (i + 1) * step
        # Sample many times; the triangle-wave sweep must never leave the sector.
        for t in (0.0, 0.7, 3.1, 8.4, 15.2, 29.9, 60.0):
            b = sector_bearing(t, i, n, PARAMS.sector_angular_speed_dps)
            assert lo <= b < hi + 1e-9, (
                f"uav {i} bearing {b} outside [{lo},{hi}) at t={t}")


def test_different_uavs_get_different_bearings():
    """The whole point: at the same instant, distinct UAVs aim differently."""
    t = 5.0
    bs = [sector_bearing(t, i, 3, PARAMS.sector_angular_speed_dps) for i in range(3)]
    assert len(set(round(b, 3) for b in bs)) == 3


# ── full waypoint ─────────────────────────────────────────────────────────

def test_sector_waypoints_lie_in_each_uavs_sector_and_grow():
    n = 3
    step = 360.0 / n
    seen_radii = []
    for i in range(n):
        lo = i * step
        hi = (i + 1) * step
        lat, lon, alt = sector_waypoint(15.0, PARAMS, i, n)
        # bearing from base must be inside the sector
        b = point_bearing_from(lat, lon, BASE[0], BASE[1])
        # wrap tolerance at the 360/0 seam for the last sector
        assert lo <= b < hi + 1e-6 or (i == n - 1 and b < 1e-6), (
            f"uav {i}: bearing {b} not in sector [{lo},{hi})")
        # altitude is the configured search altitude
        assert alt == PARAMS.base_alt
        seen_radii.append(point_radius_from(lat, lon, BASE[0], BASE[1]))
    # At t=15 of expand_time=30, radius should be ~halfway between
    # initial_radius and search_radius.
    r_min = PARAMS.search_radius * PARAMS.initial_radius_frac
    expected = r_min + (PARAMS.search_radius - r_min) * 15.0 / PARAMS.expand_time
    for r in seen_radii:
        assert abs(r - expected) < 5.0, f"radius {r} far from expected {expected}"


def test_sector_waypoints_diverge_over_time():
    """Waypoints at late t are farther from base than at early t (coverage expands)."""
    lat0, lon0, _ = sector_waypoint(1.0, PARAMS, 1, 3)
    lat1, lon1, _ = sector_waypoint(25.0, PARAMS, 1, 3)
    r0 = point_radius_from(lat0, lon0, BASE[0], BASE[1])
    r1 = point_radius_from(lat1, lon1, BASE[0], BASE[1])
    assert r1 > r0
