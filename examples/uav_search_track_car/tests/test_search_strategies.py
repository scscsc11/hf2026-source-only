"""Tests for search strategies (T022)."""
import math
import pytest

from search_track.search_strategies import SpiralParams, spiral_next_waypoint, sweep_orientation


def test_spiral_returns_valid_lat_lon_within_radius():
    p = SpiralParams(
        base_lat=27.0, base_lon=125.0, base_alt=300.0,
        search_radius=500.0, spiral_growth_rate=30.0, angular_speed_dps=30.0,
    )
    for t in [0.0, 5.0, 30.0, 120.0, 600.0]:
        lat, lon, alt = spiral_next_waypoint(t, p)
        # crude distance check (lat/lon delta, not haversine)
        dlat_deg = abs(lat - 27.0)
        dlon_deg = abs(lon - 125.0)
        # at 1 deg lat ≈ 111 km, so 500m ≈ 0.0045 deg
        assert dlat_deg < 0.01, f"t={t}: lat too far: {dlat_deg}"
        # 0.01 deg lon at lat=27 ≈ 1.0 km — generous bound for the 500m radius
        assert dlon_deg < 0.015, f"t={t}: lon too far: {dlon_deg}"
        assert alt == 300.0


def test_spiral_clamps_to_search_radius():
    p = SpiralParams(
        base_lat=27.0, base_lon=125.0, base_alt=300.0,
        search_radius=300.0, spiral_growth_rate=50.0, angular_speed_dps=360.0,
    )
    lat, lon, _ = spiral_next_waypoint(3600.0, p)  # many revolutions
    # the search radius cap means lat delta ≤ 300m / 111km ≈ 0.0027 deg
    assert abs(lat - 27.0) <= 0.0035


def test_sweep_pitch_oscillates_in_range():
    period = 4.0
    pmin, pmax = -60.0, -30.0
    seen = set()
    for t in [0.0, 0.5, 1.0, 2.0, 3.0, 4.0]:
        _, tilt = sweep_orientation(t, period, pmin, pmax)
        assert pmin <= tilt <= pmax, f"t={t} tilt={tilt} out of [{pmin},{pmax}]"
        seen.add(round(tilt, 4))
    # We should hit at least 3 distinct tilts over one period
    assert len(seen) >= 3


def test_sweep_handles_zero_period():
    _, tilt = sweep_orientation(0.0, 0.0, -60.0, -30.0)
    assert -60.0 <= tilt <= -30.0
