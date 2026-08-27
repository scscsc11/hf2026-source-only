"""Search strategies — expanding-square spiral and gimbal sweep."""
from __future__ import annotations

import math
from dataclasses import dataclass


EARTH_RADIUS_M = 6_371_000.0


@dataclass
class SpiralParams:
    base_lat: float
    base_lon: float
    base_alt: float
    search_radius: float
    spiral_growth_rate: float = 30.0  # m per revolution
    angular_speed_dps: float = 30.0  # deg / s of bearing change


def spiral_next_waypoint(t: float, p: SpiralParams) -> tuple[float, float, float]:
    """Compute (lat, lon, alt) waypoint on an Archimedean spiral at time t.

    The spiral starts at the base point and grows outward at growth_rate
    per revolution; angular speed is angular_speed_dps."""
    if p.spiral_growth_rate <= 0:
        # no growth: pure circle of radius 0; return base
        return p.base_lat, p.base_lon, p.base_alt
    bearing = (p.angular_speed_dps * t) % 360.0
    revs = (p.angular_speed_dps * t) / 360.0
    radius = min(p.search_radius, p.spiral_growth_rate * revs)
    if radius < 1.0:
        radius = 1.0
    dlat = (radius * math.cos(math.radians(bearing))) / 111320.0
    dlon = (radius * math.sin(math.radians(bearing))) / (
        111320.0 * math.cos(math.radians(p.base_lat))
    )
    return p.base_lat + dlat, p.base_lon + dlon, p.base_alt


def sweep_orientation(t: float, period: float, pitch_min: float, pitch_max: float) -> tuple[float, float]:
    """Return (pan, tilt) for a sinusoidal pitch + pan sweep.

    Pan oscillates slowly (1 revolution per 2 periods) over a ±90° range
    so the camera footprint sweeps both sides of the flight direction;
    pitch oscillates between pitch_min and pitch_max with the given
    period. The caller sends this as a target orientation each tick."""
    if period <= 0:
        period = 0.1
    phase = (t % period) / period
    # 0 -> pitch_min, 0.5 -> pitch_max, 1 -> pitch_min
    tilt = pitch_min + (pitch_max - pitch_min) * 0.5 * (1 - math.cos(2 * math.pi * phase))
    # Pan: half-period, full ±90° so we cover both sides.
    pan_phase = (t % (period * 2)) / (period * 2)
    pan = -90.0 + 180.0 * 0.5 * (1 - math.cos(2 * math.pi * pan_phase))
    return pan, tilt
