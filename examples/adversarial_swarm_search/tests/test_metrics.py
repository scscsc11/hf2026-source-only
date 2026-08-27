"""Spec 019 metrics — unit tests for FR-026 indicators.

Pure-Python tests; no Redis or sim required. We construct synthetic
SwarmState objects and feed them to RunMetrics.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.metrics import RunMetrics, DestroyedEvent, AuctionOutcome  # noqa: E402
from search_track.state import SwarmState, UavView, ZoneView  # noqa: E402


def _state(sim_time: float, uavs: list, destroyed: set = None,
           targets: list = None, decoys: list = None,
           zones: list = None) -> SwarmState:
    st = SwarmState()
    st.sim_time = sim_time
    st.status = "running"
    for u in uavs:
        st.uavs[u["uid"]] = UavView(
            uid=u["uid"], name=u.get("name", u["uid"]),
            latitude=u.get("lat", 27.0), longitude=u.get("lon", 124.99),
            altitude=u.get("alt", 600.0),
            destroyed=u["uid"] in (destroyed or set()),
            jammed=u.get("jammed", False),
            comm_sent=u.get("sent", 0),
            comm_delivered=u.get("delivered", 0),
            detected=u.get("detected", False),
            misid_flag=u.get("misid_flag", False),
            target_uid=u.get("target_uid"),
        )
    st.targets = {t["uid"]: t for t in (targets or [])}
    st.decoys = {d["uid"]: d for d in (decoys or [])}
    st.zones = zones or []
    return st


class RunMetricsBasicTest(unittest.TestCase):
    def test_initial_setup(self):
        m = RunMetrics()
        m.initialize({"u1", "u2", "u3"}, {"t1", "t2", "t3"})
        self.assertEqual(m.initial_uav_count, 3)
        self.assertEqual(m.n_true_targets, 3)

    def test_destroyed_counter(self):
        m = RunMetrics()
        m.initialize({"u1", "u2"}, {"t1", "t2"})
        # u1 destroyed at t=10
        st = _state(10.0, [{"uid": "u1", "lat": 27.0, "lon": 124.99}],
                    destroyed={"u1"})
        m.observe(st)
        self.assertEqual(m.destroyed_count, 1)
        self.assertEqual(m.alive_count, 1)
        self.assertAlmostEqual(m.alive_rate, 0.5)
        self.assertEqual(len(m.destroyed_events), 1)
        self.assertEqual(m.destroyed_events[0].uid, "u1")

    def test_discovery_tracking(self):
        m = RunMetrics()
        m.initialize({"u1"}, {"t1", "t2"})
        # First 2 ticks: detect t1, then detect t2 (a different true target)
        st = _state(5.0, [{"uid": "u1", "detected": True,
                           "target_uid": "t1"}])
        m.observe(st)
        st = _state(6.0, [{"uid": "u1", "detected": True,
                           "target_uid": "t2"}])
        m.observe(st)
        self.assertEqual(len(m.true_positive_ids), 2)
        self.assertAlmostEqual(m.discovery_rate, 1.0)

    def test_tracking_share_and_misid_ratio(self):
        m = RunMetrics()
        m.initialize({"u1"}, {"t1"})
        # 3 ticks of true detection, 1 tick of misid
        for t in range(4):
            misid = (t == 3)
            st = _state(float(t), [{"uid": "u1", "detected": True,
                                    "misid_flag": misid, "target_uid": "dec1" if misid else "t1"}])
            m.observe(st)
        self.assertEqual(m.tracking_ticks, 3)
        self.assertEqual(m.misid_ticks, 1)
        self.assertAlmostEqual(m.tracking_share, 0.75)
        self.assertAlmostEqual(m.misid_to_true_ratio, 1 / 3)

    def test_comm_deltas(self):
        m = RunMetrics()
        m.initialize({"u1"}, set())
        st = _state(1.0, [{"uid": "u1", "sent": 5, "delivered": 3}])
        m.observe(st)
        st = _state(2.0, [{"uid": "u1", "sent": 8, "delivered": 6}])
        m.observe(st)
        self.assertEqual(m.comm_sent_total, 8)
        self.assertEqual(m.comm_delivered_total, 6)

    def test_auction_record(self):
        m = RunMetrics()
        m.initialize({"u1", "u2"}, {"t1"})
        m.record_auction_outcome(AuctionOutcome(
            sim_time=10.0, target_uid="t1", winner_uid="u2",
            bid_value=0.8, n_bidders=2, conflict=False))
        m.record_auction_outcome(AuctionOutcome(
            sim_time=15.0, target_uid="t1", winner_uid="u1",
            bid_value=0.9, n_bidders=2, conflict=True))
        self.assertEqual(m.auction_rounds, 2)
        self.assertEqual(m.auction_winners["u1"], 1)
        self.assertEqual(m.auction_winners["u2"], 1)
        self.assertEqual(m.auction_conflict_count, 1)

    def test_suspect_threat_counter(self):
        m = RunMetrics()
        m.initialize({"u1", "u2"}, set())
        m.record_suspect_threat_point()
        m.record_suspect_threat_point()
        m.record_suspect_threat_point()
        self.assertEqual(m.suspect_threat_points, 3)

    def test_sc001_discovery_time(self):
        m = RunMetrics()
        # 10 targets
        m.initialize({"u1"}, {f"t{i}" for i in range(10)})
        for t in range(8):
            # 8 distinct targets discovered across 8 ticks
            st = _state(float(t), [{"uid": "u1", "detected": True,
                                    "target_uid": f"t{t}"}])
            m.observe(st)
        sc001 = m.sc001_discovery_time_s
        self.assertIsNotNone(sc001)
        self.assertAlmostEqual(sc001, 7.0)
        self.assertAlmostEqual(m.discovery_rate, 0.8)

    def test_sc005_handoff_max(self):
        m = RunMetrics()
        m.initialize({"u1", "u2"}, {"t1"})
        # t1 tracked by u1 from t=0..5, then handoff to u2 at t=10
        for t in range(0, 6):
            st = _state(float(t), [{"uid": "u1", "detected": True,
                                    "target_uid": "t1"}])
            m.observe(st)
        for t in range(10, 13):
            st = _state(float(t), [{"uid": "u2", "detected": True,
                                    "target_uid": "t1"}])
            m.observe(st)
        gap = m.sc005_handoff_max_s()
        # handoff happened at t=10 after last seen at t=5 -> gap 5.0
        self.assertAlmostEqual(gap, 5.0)

    def test_summarize_keys(self):
        m = RunMetrics()
        m.initialize({"u1"}, {"t1"})
        summary = m.summarize()
        expected = {
            "initial_uav_count", "alive_count", "destroyed_count", "alive_rate",
            "targets_discovered", "n_true_targets", "discovery_rate",
            "sc001_discovery_time_s", "tracking_ticks", "misid_ticks",
            "total_detected_ticks", "tracking_share", "misid_to_true_ratio",
            "sc005_handoff_max_s", "comm_sent_total", "comm_delivered_total",
            "auction_rounds", "auction_winners", "auction_conflict_count",
            "destroyed_events", "suspect_threat_points",
        }
        self.assertTrue(expected.issubset(summary.keys()))


if __name__ == "__main__":
    unittest.main()
