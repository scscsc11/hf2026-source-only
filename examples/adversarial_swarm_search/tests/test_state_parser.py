"""Unit tests for Spec 019 example state parsing.

Validates that the SwarmState dataclass + parse_swarm_state correctly
read the kernel's published `zones` bucket and per-entity fields.
"""
from __future__ import annotations

import unittest

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.state import SwarmState, parse_swarm_state  # noqa: E402


def _seed_state() -> dict:
    return {
        "sim_time": 12.5, "status": "running",
        "20001": {
            "type": "fixed_wing_uav", "name": "uav_01",
            "platform": {"position": {"latitude": 27.0, "longitude": 124.99, "altitude": 600.0},
                         "status": "active"},
            "comm": {"enabled": True, "range_m": 1000.0,
                     "external_jammed": True,
                     "stats": {"sent": 3, "delivered": 1}},
            "gimbal_tracking": {"detection": {"detected": True, "misid_flag": False}},
        },
        "10001": {
            "type": "ground_vehicle", "name": "target_01",
            "platform": {"position": {"latitude": 27.01, "longitude": 124.990, "altitude": 0.0}},
        },
        "30001": {
            "type": "decoy_vehicle", "name": "decoy_01",
            "platform": {"position": {"latitude": 26.999, "longitude": 124.9895, "altitude": 0.0}},
        },
        "zones": {
            "air_defense": [
                {"polygon": [[27.010, 124.9895], [27.010, 124.9905],
                             [27.020, 124.9905], [27.020, 124.9895]],
                 "alt_min": 0.0, "alt_max": 2500.0}],
            "comm_jam_static": [],
            "comm_jam_random": [
                {"polygon": [[27.000, 124.9900], [27.000, 124.9910],
                             [27.010, 124.9910], [27.010, 124.9900]],
                 "alt_min": 0.0, "alt_max": 5000.0}],
        },
    }


class SwarmStateParseTest(unittest.TestCase):
    def test_sim_time_and_status(self):
        st = parse_swarm_state(_seed_state())
        self.assertAlmostEqual(st.sim_time, 12.5)
        self.assertEqual(st.status, "running")

    def test_uav_extraction(self):
        st = parse_swarm_state(_seed_state())
        self.assertEqual(len(st.uavs), 1)
        u = st.uavs["20001"]
        self.assertAlmostEqual(u.latitude, 27.0)
        self.assertAlmostEqual(u.longitude, 124.99)
        self.assertAlmostEqual(u.altitude, 600.0)
        self.assertEqual(u.comm_sent, 3)
        self.assertEqual(u.comm_delivered, 1)
        self.assertTrue(u.jammed)
        self.assertTrue(u.detected)
        self.assertFalse(u.misid_flag)
        self.assertFalse(u.destroyed)

    def test_targets_and_decoys_partition(self):
        st = parse_swarm_state(_seed_state())
        self.assertEqual(len(st.targets), 1)
        self.assertEqual(len(st.decoys), 1)
        self.assertIn("10001", st.targets)
        self.assertIn("30001", st.decoys)
        self.assertFalse(st.targets["10001"].is_decoy)
        self.assertTrue(st.decoys["30001"].is_decoy)

    def test_zones_bucket(self):
        st = parse_swarm_state(_seed_state())
        self.assertEqual(len(st.zones), 2)
        kinds = sorted(z.type for z in st.zones)
        self.assertEqual(kinds, ["air_defense", "comm_jam_random"])
        ad = next(z for z in st.zones if z.type == "air_defense")
        self.assertEqual(len(ad.polygon), 4)

    def test_n_alive(self):
        st = parse_swarm_state(_seed_state())
        self.assertEqual(st.n_alive, 1)


if __name__ == "__main__":
    unittest.main()
