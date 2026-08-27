"""Spec 019 US5 (FR-014, FR-015, FR-016) — ThreatIntel tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.threat_intel import ThreatIntel, SuspectThreatPoint  # noqa: E402


class ThreatIntelBasicTest(unittest.TestCase):
    def test_add_suspect(self):
        ti = ThreatIntel(my_uid="u1", safe_radius_m=500.0)
        ti.add_suspect(27.0, 124.99)
        self.assertEqual(len(ti.suspect_points()), 1)

    def test_duplicate_suspect_dedup(self):
        ti = ThreatIntel(my_uid="u1", safe_radius_m=500.0)
        ti.add_suspect(27.0, 124.99)
        ti.add_suspect(27.0, 124.99)
        self.assertEqual(len(ti.suspect_points()), 1)

    def test_clear_suspect(self):
        ti = ThreatIntel(my_uid="u1", safe_radius_m=500.0)
        ti.add_suspect(27.0, 124.99)
        self.assertTrue(ti.clear_suspect(27.0, 124.99))
        self.assertEqual(len(ti.suspect_points()), 0)

    def test_clear_unknown_returns_false(self):
        ti = ThreatIntel(my_uid="u1", safe_radius_m=500.0)
        self.assertFalse(ti.clear_suspect(27.0, 124.99))

    def test_path_threat_cost_in_circle(self):
        ti = ThreatIntel(my_uid="u1", safe_radius_m=500.0)
        ti.add_suspect(27.00, 124.990)  # origin
        # Path through the suspect point — cost should be > 0
        path = [(27.00, 124.990), (27.00, 124.9905)]
        cost = ti.path_threat_cost(path)
        self.assertGreater(cost, 0.0)

    def test_path_threat_cost_misses(self):
        ti = ThreatIntel(my_uid="u1", safe_radius_m=500.0)
        ti.add_suspect(27.00, 124.990)
        # Path 1km north — outside the 500m circle
        path = [(27.01, 124.990), (27.02, 124.990)]
        cost = ti.path_threat_cost(path)
        self.assertEqual(cost, 0.0)


if __name__ == "__main__":
    unittest.main()
