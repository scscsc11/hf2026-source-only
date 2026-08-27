"""Spec 019 US5 (FR-014, FR-015, FR-016) — ThreatIntel.

Records suspect-threat points derived purely from **peer loss events**
(the last heartbeat position of a peer that has gone silent).  Per
info-isolation (SC-010), the algorithm has no other source of threat
information — there is no read of scenario zones config or ground-truth
threat fields.

A suspect-threat point is a 2-D point with an associated ``safe_radius``
— any path coming within `safe_radius` of a suspect point should detour
around it (handled by `blind_avoidance_planner.BlindAvoidancePlanner`).

The intel layer also exposes:

  * `add_suspect(lat, lon)`        — record a new suspect point
  * `clear_suspect(lat, lon)`     — remove a suspect point (recovery)
  * `suspect_points() -> list`    — currently active suspect points
  * `path_threat_cost(path)`       — sum of "distance-from-suspect"
                                     values along a path (used by the
                                     auction bid function)
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SuspectThreatPoint:
    lat: float
    lon: float
    safe_radius: float

    def distance_m(self, lat: float, lon: float) -> float:
        return _haversine(self.lat, self.lon, lat, lon)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


class ThreatIntel:
    def __init__(self, my_uid: str, safe_radius_m: float = 600.0) -> None:
        self.my_uid = my_uid
        self._safe_radius = safe_radius_m
        self._points: list[SuspectThreatPoint] = []

    @property
    def safe_radius_m(self) -> float:
        return self._safe_radius

    def set_safe_radius_m(self, m: float) -> None:
        self._safe_radius = m

    def add_suspect(self, lat: float, lon: float) -> SuspectThreatPoint:
        p = SuspectThreatPoint(lat=lat, lon=lon, safe_radius=self._safe_radius)
        # Don't duplicate (within 1m)
        for existing in self._points:
            if existing.distance_m(lat, lon) < 1.0:
                return existing
        self._points.append(p)
        return p

    def clear_suspect(self, lat: float, lon: float) -> bool:
        for i, p in enumerate(self._points):
            if p.distance_m(lat, lon) < 1.0:
                self._points.pop(i)
                return True
        return False

    def suspect_points(self) -> list[SuspectThreatPoint]:
        return list(self._points)

    def danger_circles(self) -> list[tuple[float, float, float]]:
        """Return [(lat, lon, safe_radius), ...] for the active set."""
        return [(p.lat, p.lon, p.safe_radius) for p in self._points]

    def path_threat_cost(self, path: list[tuple[float, float]]) -> float:
        """Sum of ``max(0, safe_radius - distance_to_suspect)`` over the
        whole path.  Higher = closer to a suspect = more dangerous.
        """
        cost = 0.0
        for plat, plon in path:
            for p in self._points:
                d = p.distance_m(plat, plon)
                if d < p.safe_radius:
                    cost += (p.safe_radius - d)
        return cost
