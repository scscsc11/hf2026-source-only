"""Spec 025 — cooperative continuous-tracking evaluator unit tests.

Pure-Python tests; no Redis or sim required. We construct synthetic
per-tick UAV->target maps and feed them to CoopTrackingEvaluator, then
assert the continuous-tracking state machine, scoring, and the
UAV->target nearest-neighbour resolver behave as specified.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]   # examples/_common/tests -> repo
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from examples._common.coop_eval import (  # noqa: E402
    CoopTrackingEvaluator, ScoringProfile,
    profile_uav_search_track_car, profile_multi_uav_coop_decoy,
    profile_adversarial_swarm_search,
)
from examples._common.uav_target_map import (  # noqa: E402
    UavDetection, TargetMatch, resolve_uav_to_target,
)


# ── helpers ───────────────────────────────────────────────────────────────


def _tick(sim_time, trackers, destroyed=()):
    """Build one tick's (sim_time, uav_target_map, destroyed_uids).

    ``trackers``: {target_uid: set_of_uav_uids} of EFFECTIVE trackers.
    """
    uav_map = {}
    for tgt, uavs in trackers.items():
        for u in uavs:
            uav_map[u] = TargetMatch(target_uid=tgt, is_effective=True,
                                     was_misid=False, confidence=1.0)
    return (float(sim_time), uav_map, set(destroyed))


def _misid_tick(sim_time, uav, decoy_uid, destroyed=()):
    """One tick where ``uav`` is mis-tracking a decoy."""
    return (float(sim_time),
            {uav: TargetMatch(target_uid=decoy_uid, is_effective=False,
                              was_misid=True)},
            set(destroyed))


def _run(ev, ticks):
    for t in ticks:
        ev.observe(t[0], t[1], t[2])


def _track_range(t0, t1, target, uavs, dt=1.0):
    """Continuous tracking of ``target`` by ``uavs`` over [t0, t1] inclusive."""
    t = t0
    out = []
    while t <= t1 + 1e-9:
        out.append(_tick(t, {target: set(uavs)}))
        t += dt
    return out


def _lost_range(t0, t1, dt=1.0):
    """No tracking at all over [t0, t1] inclusive."""
    t = t0
    out = []
    while t <= t1 + 1e-9:
        out.append(_tick(t, {}))
        t += dt
    return out


def _test_profile(dwell: float = 20.0, K: int = 1,
                  linear: bool = False) -> ScoringProfile:
    """Minimal profile for state-machine tests (independent of the real
    example profiles, whose dwell_target / weights may change)."""
    return ScoringProfile(name="test", K=K, dwell_target_s=dwell,
                          grace_s=2.0, duration_s=60.0,
                          linear_completion=linear,
                          weights={"completion": 1.0})


# ── continuous-tracking state machine ────────────────────────────────────


class ContinuousTrackingTest(unittest.TestCase):
    def test_reaches_completion_after_20s(self):
        ev = CoopTrackingEvaluator(_test_profile(), {"T1"})
        _run(ev, _track_range(0, 20, "T1", {"U1"}))
        st = ev.states["T1"]
        self.assertTrue(st.completed)
        self.assertAlmostEqual(st.completed_at, 20.0)
        self.assertEqual(st.resets, 0)
        self.assertAlmostEqual(st.max_dwell_run, 20.0)

    def test_short_interruption_is_tolerated(self):
        # Track 0..15 (dwell=15), lose 16..17 (2s, within grace=2),
        # resume 18..20 -> total continuous dwell reaches 20 at t=20.
        ev = CoopTrackingEvaluator(_test_profile(), {"T1"})
        _run(ev, _track_range(0, 15, "T1", {"U1"}))
        _run(ev, _lost_range(16, 17))
        _run(ev, _track_range(18, 20, "T1", {"U1"}))
        st = ev.states["T1"]
        self.assertTrue(st.completed)
        self.assertAlmostEqual(st.completed_at, 20.0)
        self.assertEqual(st.resets, 0)
        self.assertAlmostEqual(st.max_dwell_run, 20.0)

    def test_long_interruption_resets(self):
        # Track 0..10 (dwell=10), lose 11..14 (4s > grace=2) -> reset,
        # resume 15..20 (fresh attempt, dwell only reaches 6).
        ev = CoopTrackingEvaluator(profile_uav_search_track_car(), {"T1"})
        _run(ev, _track_range(0, 10, "T1", {"U1"}))
        _run(ev, _lost_range(11, 14))
        _run(ev, _track_range(15, 20, "T1", {"U1"}))
        st = ev.states["T1"]
        self.assertEqual(st.resets, 1)
        self.assertFalse(st.completed)
        self.assertAlmostEqual(st.max_dwell_run, 10.0)   # best run before reset
        # fresh attempt resumes from the second coop window
        self.assertAlmostEqual(st.first_coop_at, 15.0)

    def test_completion_is_monotonic(self):
        # Once completed, a later loss must NOT un-complete it.
        ev = CoopTrackingEvaluator(_test_profile(), {"T1"})
        _run(ev, _track_range(0, 20, "T1", {"U1"}))
        self.assertTrue(ev.states["T1"].completed)
        _run(ev, _lost_range(21, 30))
        self.assertTrue(ev.states["T1"].completed)
        self.assertAlmostEqual(ev.completion_rate, 1.0)


class CoopThresholdTest(unittest.TestCase):
    def test_k_threshold_requires_two_trackers(self):
        prof = profile_multi_uav_coop_decoy(K=2)
        ev = CoopTrackingEvaluator(prof, {"T1"})
        # Only one tracker -> never cooperative -> no dwell, no completion.
        _run(ev, _track_range(0, 25, "T1", {"U1"}))
        self.assertFalse(ev.states["T1"].completed)
        self.assertEqual(ev.states["T1"].coop_ticks, 0)
        # Two trackers -> cooperative -> completes.
        _run(ev, _track_range(26, 50, "T1", {"U1", "U2"}))
        self.assertTrue(ev.states["T1"].completed)

    def test_destroyed_uav_does_not_count(self):
        prof = profile_multi_uav_coop_decoy(K=2)
        ev = CoopTrackingEvaluator(prof, {"T1"})
        _run(ev, _track_range(0, 25, "T1", {"U1", "U2"},
                              )[:0] + [_tick(t, {"T1": {"U1", "U2"}},
                                             destroyed={"U2"})
                                       for t in range(0, 26)])
        # U2 destroyed -> only U1 effective -> not cooperative.
        self.assertFalse(ev.states["T1"].completed)


# ── scoring ───────────────────────────────────────────────────────────────


class ScoringTest(unittest.TestCase):
    def test_score_is_pure_and_repeatable(self):
        ev = CoopTrackingEvaluator(profile_uav_search_track_car(), {"T1"})
        _run(ev, _track_range(0, 10, "T1", {"U1"}))
        s1 = ev.score({"search_time": 5.0, "track_in_view_fraction": 1.0,
                       "sim_t0": 0.0})
        s2 = ev.score({"search_time": 5.0, "track_in_view_fraction": 1.0,
                       "sim_t0": 0.0})
        self.assertEqual(s1, s2)
        # scoring must not mutate observed state -> observe still advances.
        before = ev.states["T1"].dwell_accumulated
        ev.observe(*_tick(11, {"T1": {"U1"}}))
        self.assertGreater(ev.states["T1"].dwell_accumulated, before)

    def test_total_score_in_range(self):
        ev = CoopTrackingEvaluator(profile_uav_search_track_car(), {"T1"})
        _run(ev, _track_range(0, 25, "T1", {"U1"}))
        s = ev.score({"search_time": 3.0, "track_in_view_fraction": 1.0,
                      "sim_t0": 0.0})
        self.assertGreaterEqual(s["total_score"], 0.0)
        self.assertLessEqual(s["total_score"], 100.0)

    def test_adversarial_blend(self):
        prof = profile_adversarial_swarm_search(K=3)
        ev = CoopTrackingEvaluator(prof, {"T1"})
        _run(ev, _track_range(0, 25, "T1", {"U1", "U2", "U3"}))
        # completion=1, alive=1 -> 0.7*100 + 0.3*100 = 100
        s = ev.score({"alive_rate": 1.0})
        self.assertAlmostEqual(s["total_score"], 100.0, places=1)
        self.assertTrue(s["passed"])
        # completion=1, alive=0.5 -> 0.7*100 + 0.3*50 = 85, but alive<0.5 gate
        # fails at exactly 0.5 boundary only if <; alive_rate=0.5 passes gate.
        s2 = ev.score({"alive_rate": 0.5})
        self.assertAlmostEqual(s2["total_score"], 85.0, places=1)
        self.assertTrue(s2["passed"])

    def test_adversarial_partial_completion(self):
        prof = profile_adversarial_swarm_search(K=3)
        ev = CoopTrackingEvaluator(prof, {"T1", "T2"})
        _run(ev, _track_range(0, 25, "T1", {"U1", "U2", "U3"}))
        # only T1 completed -> completion_rate=0.5
        self.assertAlmostEqual(ev.completion_rate, 0.5)
        s = ev.score({"alive_rate": 1.0})
        # 0.5*50 + 0.2*100(quality) + 0.3*100 = 75; pass requires completion==1 -> False
        self.assertAlmostEqual(s["total_score"], 75.0, places=1)
        self.assertFalse(s["passed"])

    def test_misid_accounting(self):
        ev = CoopTrackingEvaluator(profile_multi_uav_coop_decoy(), {"T1"})
        _run(ev, [
            _tick(0, {"T1": {"U1", "U2"}}),
            _tick(1, {"T1": {"U1", "U2"}}),
            _tick(2, {"T1": {"U1", "U2"}}),
            _misid_tick(3, "U1", "D1"),
        ])
        self.assertEqual(ev.misid_ticks, 1)
        # 3 effective frames (U1+U2 each = 2 detections/frame = 6) + 1 misid
        self.assertEqual(ev.total_detected_ticks, 7)
        self.assertAlmostEqual(ev.misid_rate, 1 / 7)

    def test_duration_zero_does_not_crash_score(self):
        # Regression: web bridge forces --duration 0 (infinite run). The
        # uav_search_track_car profile weights include "search" and
        # "time_to_all", both of which divide by profile.duration_s.
        # duration_s==0 -> ZeroDivisionError in _dimension -> runner crash
        # -> controller_exited. score() must stay bounded [0,100] instead.
        ev = CoopTrackingEvaluator(
            profile_uav_search_track_car(duration_s=0.0), {"T1"})
        _run(ev, _track_range(0, 10, "T1", {"U1"}))
        s = ev.score({"search_time": 5.0, "track_in_view_fraction": 1.0,
                      "sim_t0": 0.0})
        self.assertGreaterEqual(s["total_score"], 0.0)
        self.assertLessEqual(s["total_score"], 100.0)


class FullCoopTest(unittest.TestCase):
    def test_full_coop_counts_three_trackers(self):
        prof = profile_multi_uav_coop_decoy(K=2)   # full_coop_K=3
        ev = CoopTrackingEvaluator(prof, {"T1"})
        # 5 ticks with 3 trackers -> full coop every tick.
        _run(ev, [_tick(t, {"T1": {"U1", "U2", "U3"}}) for t in range(5)])
        self.assertEqual(ev.full_coop_ticks, 5)
        # 5 ticks with only 2 trackers -> no full coop.
        _run(ev, [_tick(t, {"T1": {"U1", "U2"}}) for t in range(5, 10)])
        self.assertEqual(ev.full_coop_ticks, 5)
        s = ev.score({})
        self.assertGreater(s["dimension_scores"]["full_coop"], 0.0)


class TrackQualityTest(unittest.TestCase):
    def test_quality_averages_confidence(self):
        ev = CoopTrackingEvaluator(profile_multi_uav_coop_decoy(), {"T1"})
        # 2 ticks at confidence 1.0, then 2 ticks at 0.5 -> avg 0.75
        for t, conf in [(0, 1.0), (1, 1.0), (2, 0.5), (3, 0.5)]:
            uav_map = {
                "U1": TargetMatch("T1", True, False, confidence=conf),
                "U2": TargetMatch("T1", True, False, confidence=conf),
            }
            ev.observe(float(t), uav_map, set())
        s = ev.score({})
        self.assertAlmostEqual(s["dimension_scores"]["track_quality"], 75.0,
                               places=1)

    def test_quality_zero_when_no_effective_track(self):
        ev = CoopTrackingEvaluator(profile_multi_uav_coop_decoy(), {"T1"})
        _run(ev, _lost_range(0, 5))
        s = ev.score({})
        self.assertEqual(s["dimension_scores"]["track_quality"], 0.0)


# ── UAV -> target nearest-neighbour resolver ──────────────────────────────


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.true_targets = {"T1": (27.0000, 125.0000)}
        self.decoys = {"D1": (27.0005, 125.0000)}   # ~55 m north of T1

    def test_matches_real_target(self):
        uavs = [UavDetection(uid="U1", detected=True, confidence=0.9,
                             target_lat=27.0000, target_lon=125.0000,
                             target_type="ground_vehicle")]
        m = resolve_uav_to_target(uavs, self.true_targets, self.decoys)
        self.assertEqual(m["U1"].target_uid, "T1")
        self.assertTrue(m["U1"].is_effective)
        self.assertFalse(m["U1"].was_misid)
        self.assertAlmostEqual(m["U1"].confidence, 0.9)   # flows through

    def test_decoy_match_has_zero_confidence(self):
        uavs = [UavDetection(uid="U1", detected=True, confidence=0.9,
                             target_lat=27.0005, target_lon=125.0000,
                             target_type="decoy_vehicle", misid_flag=True)]
        m = resolve_uav_to_target(uavs, self.true_targets, self.decoys)
        self.assertEqual(m["U1"].target_uid, "D1")
        self.assertAlmostEqual(m["U1"].confidence, 0.0)   # misid -> no quality

    def test_matches_decoy_as_misid(self):
        uavs = [UavDetection(uid="U1", detected=True,
                             target_lat=27.0005, target_lon=125.0000,
                             target_type="decoy_vehicle", misid_flag=True)]
        m = resolve_uav_to_target(uavs, self.true_targets, self.decoys)
        self.assertEqual(m["U1"].target_uid, "D1")
        self.assertFalse(m["U1"].is_effective)
        self.assertTrue(m["U1"].was_misid)

    def test_too_far_is_no_match(self):
        uavs = [UavDetection(uid="U1", detected=True,
                             target_lat=27.5, target_lon=125.5)]   # far away
        m = resolve_uav_to_target(uavs, self.true_targets, self.decoys,
                                  max_match_m=120.0)
        self.assertIsNone(m["U1"].target_uid)
        self.assertFalse(m["U1"].is_effective)

    def test_not_detected_is_no_match(self):
        uavs = [UavDetection(uid="U1", detected=False)]
        m = resolve_uav_to_target(uavs, self.true_targets, self.decoys)
        self.assertIsNone(m["U1"].target_uid)

    def test_destroyed_is_no_match(self):
        uavs = [UavDetection(uid="U1", detected=True, destroyed=True,
                             target_lat=27.0000, target_lon=125.0000)]
        m = resolve_uav_to_target(uavs, self.true_targets, self.decoys)
        self.assertIsNone(m["U1"].target_uid)


if __name__ == "__main__":
    unittest.main()
