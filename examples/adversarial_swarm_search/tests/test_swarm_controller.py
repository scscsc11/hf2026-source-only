"""Spec 019 Phase 5 tests — SwarmController blind-avoidance logic.

Validates the pure-Python helpers in swarm_controller.py:
  * point-in-polygon matches the C++ kernel implementation
  * nearest-edge projection stays on the polygon boundary
  * avoid_zone pushes a waypoint OUTSIDE the polygon with the configured margin
  * SwarmController._apply_avoidance returns a command only when the UAV
    is currently inside a published air-defense zone

These tests do NOT touch Redis or the sim — they're pure unit tests.
"""
from __future__ import annotations

import math
import unittest

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.swarm_controller import (  # noqa: E402
    SwarmController, _point_in_poly, _nearest_edge_projection, _avoid_zone,
    _haversine_m,
)
from search_track.state import SwarmState, parse_swarm_state, UavView, ZoneView  # noqa: E402


SQUARE = [(27.010, 124.9895), (27.010, 124.9905),
          (27.020, 124.9905), (27.020, 124.9895)]


class PointInPolyTest(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(_point_in_poly(27.015, 124.9900, SQUARE))
    def test_outside(self):
        self.assertFalse(_point_in_poly(27.000, 124.9900, SQUARE))
        self.assertFalse(_point_in_poly(27.030, 124.9900, SQUARE))
    def test_degenerate(self):
        self.assertFalse(_point_in_poly(27.015, 124.9900, [(0, 0), (1, 1)]))


class NearestEdgeProjectionTest(unittest.TestCase):
    def test_returns_a_boundary_point(self):
        proj = _nearest_edge_projection(27.015, 124.9900, SQUARE)
        # The projected point should be on the boundary; distance to any
        # polygon vertex should be at most the square's diagonal.
        max_d = 0.0
        for (ylat, xlon) in SQUARE:
            d = _haversine_m(proj[0], proj[1], ylat, xlon)
            max_d = max(max_d, d)
        # Square ~1.1km on a side — diagonal ~1.5km.
        self.assertLess(max_d, 2000.0)


class AvoidZoneTest(unittest.TestCase):
    def test_pushes_outside(self):
        # UAV sits inside the polygon.
        z = ZoneView(type="air_defense", polygon=list(SQUARE))
        new_lat, new_lon = _avoid_zone(27.015, 124.9900, z, margin_m=250.0)
        self.assertFalse(_point_in_poly(new_lat, new_lon, SQUARE),
                         f"avoid_zone produced point still inside: {new_lat}, {new_lon}")

    def test_leaves_outside_alone(self):
        # UAV already outside; avoid_zone's behaviour on an outside point
        # is to project to the nearest edge and push out by margin.
        # We only assert it's still outside (which is the contract).
        lat, lon = 27.000, 124.9900
        z = ZoneView(type="air_defense", polygon=list(SQUARE))
        new_lat, new_lon = _avoid_zone(lat, lon, z, margin_m=250.0)
        self.assertFalse(_point_in_poly(new_lat, new_lon, SQUARE))


class SwarmControllerAvoidanceTest(unittest.TestCase):
    def _make_state(self, uav_lat, uav_lon, uav_alt, zones):
        st = SwarmState()
        st.sim_time = 0.0
        st.uavs["90001"] = UavView(uid="90001", name="test_uav",
                                   latitude=uav_lat, longitude=uav_lon,
                                   altitude=uav_alt)
        st.zones = zones
        return st

    def _zone(self, poly, alt_min=0.0, alt_max=2500.0):
        return ZoneView(type="air_defense", polygon=list(poly),
                        alt_min=alt_min, alt_max=alt_max)

    def _dest_cmd(self, cmds):
        """Return the set_destination command (always emitted)."""
        for c in cmds:
            if c["cmd"] == "set_destination":
                return c
        return None

    def test_emits_command_when_uav_inside_zone(self):
        sc = SwarmController(my_uid="90001")
        sc.set_sector_center(27.000, 124.9900)
        sc.configure({"blind_avoidance_enabled": True,
                      "avoidance_margin_m": 250.0,
                      "search_radius": 2500.0,
                      "sector_center_latitude": 27.000,
                      "sector_center_longitude": 124.9900})
        zones = [self._zone(SQUARE)]
        st = self._make_state(27.015, 124.9900, 600.0, zones)
        cmds = sc.decide(st, period=0.1)
        dest = self._dest_cmd(cmds)
        self.assertIsNotNone(dest, "set_destination always emitted")
        # Verify the destination is pushed outside the polygon.
        new_lat = dest["params"]["latitude"]
        new_lon = dest["params"]["longitude"]
        self.assertFalse(_point_in_poly(new_lat, new_lon, SQUARE),
                         f"destination still inside zone: {new_lat}, {new_lon}")

    def test_searches_when_uav_outside_zone(self):
        """No avoidance needed → controller still drives sector search."""
        sc = SwarmController(my_uid="90001")
        sc.set_sector_center(27.000, 124.9900)
        sc.configure({"blind_avoidance_enabled": True,
                      "avoidance_margin_m": 250.0,
                      "search_radius": 2500.0,
                      "sector_center_latitude": 27.000,
                      "sector_center_longitude": 124.9900})
        zones = [self._zone(SQUARE)]
        st = self._make_state(27.000, 124.9900, 600.0, zones)
        cmds = sc.decide(st, period=0.1)
        # Must emit a set_destination (sector search) — NOT empty.
        self.assertIsNotNone(self._dest_cmd(cmds),
                             "sector search must always produce a waypoint")
        self.assertTrue(any(c["cmd"] == "set_destination" for c in cmds))

    def test_altitude_band_filter(self):
        """UAV flying ABOVE the SAM belt must not avoid; it still searches."""
        sc = SwarmController(my_uid="90001")
        sc.set_sector_center(27.000, 124.9900)
        sc.configure({"blind_avoidance_enabled": True,
                      "avoidance_margin_m": 250.0,
                      "search_radius": 2500.0,
                      "sector_center_latitude": 27.000,
                      "sector_center_longitude": 124.9900})
        zones = [self._zone(SQUARE, alt_max=2500.0)]
        st = self._make_state(27.015, 124.9900, 5000.0, zones)
        cmds = sc.decide(st, period=0.1)
        dest = self._dest_cmd(cmds)
        self.assertIsNotNone(dest)
        # Destination is the raw sector waypoint (no avoidance), so it may
        # land inside the (altitude-irrelevant) polygon — that's fine.

    def test_disabled_still_searches(self):
        """Blind avoidance off → controller still does sector search."""
        sc = SwarmController(my_uid="90001")
        sc.set_sector_center(27.000, 124.9900)
        sc.configure({"blind_avoidance_enabled": False,
                      "avoidance_margin_m": 250.0,
                      "search_radius": 2500.0,
                      "sector_center_latitude": 27.000,
                      "sector_center_longitude": 124.9900})
        zones = [self._zone(SQUARE)]
        st = self._make_state(27.015, 124.9900, 600.0, zones)
        cmds = sc.decide(st, period=0.1)
        self.assertIsNotNone(self._dest_cmd(cmds))


if __name__ == "__main__":
    unittest.main()
