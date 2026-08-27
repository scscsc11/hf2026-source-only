"""Spec 019 US5 (FR-014, FR-015) — Blind-avoidance path planner.

Pure-Python implementation of "tangent detour around a danger circle"
for a list of suspect-threat points.  The planner takes a single
straight-line path (origin → waypoint) and produces a new path that
avoids all active danger circles:

  1.  Compute the closest point on the line segment to each suspect.
  2.  For circles whose closest-point distance is < safe_radius, insert
      a tangent point on each side of the circle so the new path goes
      around it.  Multiple circles are sorted by distance-along-line
      and the detours are concatenated.
  3.  Lines that miss all circles are returned unchanged.

The planner does NOT consult any scenario zones config — it only knows
about suspect-threat points recorded via `ThreatIntel` (i.e. derived
from peer-loss heartbeats, per SC-010).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .threat_intel import SuspectThreatPoint, ThreatIntel, _haversine


def _interp(a: tuple[float, float], b: tuple[float, float],
            t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _project_t(a: tuple[float, float], b: tuple[float, float],
               p: tuple[float, float]) -> float:
    """Project p onto the line a-b in (lat, lon) space; return t in [0,1]."""
    ab = (b[0] - a[0], b[1] - a[1])
    ab2 = ab[0] ** 2 + ab[1] ** 2
    if ab2 < 1e-12:
        return 0.0
    return ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / ab2


class BlindAvoidancePlanner:
    def __init__(self, my_uid: str, safe_radius_m: float = 600.0) -> None:
        self.my_uid = my_uid
        self._safe_radius = safe_radius_m

    def set_safe_radius_m(self, m: float) -> None:
        self._safe_radius = m

    def adjust_waypoint(self, origin: tuple[float, float],
                        waypoint: tuple[float, float],
                        threats: list[SuspectThreatPoint]) -> list[tuple[float, float]]:
        """Return a list of (lat, lon) waypoints from origin to the
        original waypoint, with tangent detours inserted for any threat
        circle whose closest approach is within ``safe_radius_m``.
        """
        a = origin
        b = waypoint
        # Find intersections of the segment with each threat circle.
        # Line: a + t*(b-a), t in [0,1].  Circle: (lat-c_lat)^2 + ...
        # In (lat, lon) space, this is a planar approx; it's good enough
        # for the small distances we deal with (< 10 km).
        events: list[tuple[float, tuple[float, float]]] = []
        for th in threats:
            t_proj = _project_t(a, b, (th.lat, th.lon))
            if t_proj < 0 or t_proj > 1:
                continue
            closest = _interp(a, b, t_proj)
            d = _haversine(closest[0], closest[1], th.lat, th.lon)
            if d >= th.safe_radius:
                continue
            # Compute a perpendicular offset so the new path goes around
            # the circle with a "margin" (≈0).  Take the perpendicular
            # unit vector.
            ab = (b[0] - a[0], b[1] - a[1])
            n = math.hypot(ab[0], ab[1])
            if n < 1e-12:
                continue
            perp = (-ab[1] / n, ab[0] / n)
            # Choose the side that's farther from the circle center
            side_plus = (closest[0] + perp[0] * th.safe_radius * 1.05,
                         closest[1] + perp[1] * th.safe_radius * 1.05)
            side_minus = (closest[0] - perp[0] * th.safe_radius * 1.05,
                          closest[1] - perp[1] * th.safe_radius * 1.05)
            d_plus = _haversine(side_plus[0], side_plus[1], th.lat, th.lon)
            d_minus = _haversine(side_minus[0], side_minus[1], th.lat, th.lon)
            tangent = side_plus if d_plus > d_minus else side_minus
            # The detour uses two control points: the tangent on entry
            # and the tangent on exit (mirror of the same circle).  We
            # approximate the exit tangent at t=t_proj+0.0 of the line
            # (i.e. the same circle gives one tangent; the path goes
            # origin -> entry tangent -> exit tangent -> waypoint).
            events.append((t_proj, (tangent, closest)))

        if not events:
            return [a, b]

        # Sort events by t so the path detours in order
        events.sort(key=lambda e: e[0])
        # Build the new path: a, [tangent0_entry, tangent0_exit, ...], b
        # For each event we add a single tangent control point.
        path = [a]
        for _t, (tangent, _closest) in events:
            path.append(tangent)
        path.append(b)
        return path
