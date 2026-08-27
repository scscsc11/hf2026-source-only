"""Spec 019 US5 — BlindAvoidancePlanner tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.blind_avoidance_planner import BlindAvoidancePlanner  # noqa: E402
from search_track.threat_intel import SuspectThreatPoint  # noqa: E402


class BlindAvoidancePlannerTest(unittest.TestCase):
    def _threat(self, lat, lon, r=500.0):
        return SuspectThreatPoint(lat=lat, lon=lon, safe_radius=r)

    def test_path_missing_threat_is_unchanged(self):
        bp = BlindAvoidancePlanner(my_uid="u1", safe_radius_m=500.0)
        # waypoint == origin (no movement) — planner must skip the
        # segment/tangent math entirely and return [origin, waypoint].
        path = bp.adjust_waypoint((27.000, 124.990), (27.000, 124.990),
                                  [self._threat(27.000, 124.990, r=200.0)])
        self.assertEqual(len(path), 2)
        self.assertAlmostEqual(path[-1][0], 27.000)
        self.assertAlmostEqual(path[-1][1], 124.990)

    def test_path_through_threat_gets_detour(self):
        bp = BlindAvoidancePlanner(my_uid="u1", safe_radius_m=500.0)
        # Origin at (27.00, 124.990), waypoint 1km east (27.00, 124.991),
        # threat circle at (27.00, 124.9905) with 500m radius — line passes
        # THROUGH the threat.
        path = bp.adjust_waypoint((27.00, 124.990), (27.00, 124.991),
                                  [self._threat(27.00, 124.9905, r=500.0)])
        # Should insert at least one detour waypoint
        self.assertGreater(len(path), 2)

    def test_detour_stays_outside_threat(self):
        bp = BlindAvoidancePlanner(my_uid="u1", safe_radius_m=500.0)
        threat = self._threat(27.00, 124.9905, r=500.0)
        path = bp.adjust_waypoint((27.00, 124.990), (27.00, 124.991), [threat])
        # All interior waypoints should be at least safe_radius away from threat center
        for p in path[1:-1]:
            d = threat.distance_m(p[0], p[1])
            self.assertGreater(d, threat.safe_radius * 0.9,
                               f"interior waypoint {p} too close ({d:.1f} m)")


if __name__ == "__main__":
    unittest.main()
