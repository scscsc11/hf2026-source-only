"""Tests for run.py target-trajectory injection + sector index assignment.

Uses a fake client to capture published commands and verifies that
declared target trajectories are activated (set_speed + set_trajectory)
and that UAVs get distinct fleet indices.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
EXAMPLE_DIR = HERE.parents[1]
for p in (str(REPO_ROOT), str(EXAMPLE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# run.py lives at examples/multi_uav_coop_decoy/run.py and is importable as
# examples.multi_uav_coop_decoy.run from repo root.
from examples.multi_uav_coop_decoy import run as runmod
from search_track.multi_state import EntityState, MultiSimState


SCENARIO_PATH = str(
    Path(__file__).resolve().parents[1] / "config" / "scenario.json"
)


class FakeClient:
    def __init__(self):
        self.published: list[dict] = []

    def publish_dict(self, d):
        self.published.append(d)
        return 1


def _state_with_targets(*uids: str) -> MultiSimState:
    ents = {uid: EntityState(uid=uid, kind="ground_vehicle", name=uid)
            for uid in uids}
    return MultiSimState(sim_time=0.0, timestamp=0.0,
                         status="running", entities=ents)


def _no_log(*a, **kw):
    pass


# ── _load_target_trajectories ────────────────────────────────────────────

def test_load_target_trajectories_reads_all_three_targets():
    trajs = runmod._load_target_trajectories(SCENARIO_PATH)
    assert set(trajs.keys()) == {"10001", "10002", "10003"}
    for uid, t in trajs.items():
        assert t["speed"] > 0
        assert len(t["waypoints"]) >= 3
        for w in t["waypoints"]:
            assert {"lat", "lon", "alt", "t"} <= set(w.keys())


# ── _inject_target_trajectories ──────────────────────────────────────────

def test_inject_publishes_set_speed_and_set_trajectory_per_target():
    trajs = {
        "10001": {"speed": 5.0, "waypoints": [
            {"lat": 27.0, "lon": 125.0, "alt": 0.0, "t": 0.0}]},
        "10002": {"speed": 9.0, "waypoints": [
            {"lat": 27.0, "lon": 125.0, "alt": 0.0, "t": 0.0}]},
    }
    client = FakeClient()
    state = _state_with_targets("10001", "10002")
    n = runmod._inject_target_trajectories(
        client, state, trajs, dry_run=False, log=_no_log)
    assert n == 2
    cmds_by_uid = {}
    for c in client.published:
        cmds_by_uid.setdefault(c["unique_id"], []).append(c["cmd"])
    for uid in ("10001", "10002"):
        assert "set_speed" in cmds_by_uid[uid]
        assert "set_trajectory" in cmds_by_uid[uid]
    # set_speed must precede set_trajectory (speed set before activating).
    seq = [c["cmd"] for c in client.published if c["unique_id"] == "10001"]
    assert seq.index("set_speed") < seq.index("set_trajectory")


def test_inject_dry_run_publishes_nothing():
    trajs = {"10001": {"speed": 5.0, "waypoints": [
        {"lat": 27.0, "lon": 125.0, "alt": 0.0, "t": 0.0}]}}
    client = FakeClient()
    state = _state_with_targets("10001")
    n = runmod._inject_target_trajectories(
        client, state, trajs, dry_run=True, log=_no_log)
    assert n == 1
    assert client.published == []


def test_inject_skips_targets_not_in_state():
    trajs = {"99999": {"speed": 5.0, "waypoints": [
        {"lat": 27.0, "lon": 125.0, "alt": 0.0, "t": 0.0}]}}
    client = FakeClient()
    state = _state_with_targets()  # empty
    n = runmod._inject_target_trajectories(
        client, state, trajs, dry_run=False, log=_no_log)
    assert n == 0
    assert client.published == []


# ── fleet index assignment ───────────────────────────────────────────────

def test_build_controllers_assigns_distinct_fleet_indices():
    from search_track.coop_controller import CoopController
    uav_uids = ["20001", "20002", "20003"]
    # Minimal first state carrying the three UAVs for the centroid path.
    ents = {}
    for i, uid in enumerate(uav_uids):
        from examples.uav_search_track_car.search_track.state import (
            Attitude, GeoPosition, UavState,
        )
        ents[uid] = EntityState(
            uid=uid, kind="uav", name=uid,
            uav=UavState(
                position=GeoPosition(latitude=27.0 + i * 0.001,
                                     longitude=125.0, altitude=300.0),
                attitude=Attitude(yaw=0.0, pitch=0.0, roll=0.0),
                velocity=20.0, heading=0.0,
            ),
        )
    first = MultiSimState(sim_time=0.0, timestamp=0.0,
                          status="running", entities=ents)
    cfg = {
        "search_radius": 800.0, "search_altitude_agl": 300.0,
        "use_sector_search": True, "search_sweep_time": 90.0,
        "sector_angular_speed_dps": 25.0,
        "sweep_period": 4.0, "sweep_pitch_min": -60.0, "sweep_pitch_max": -30.0,
    }
    ctrls = runmod._build_controllers(uav_uids, cfg, first, _no_log)
    indices = sorted(c._fleet_index for c in ctrls.values())
    assert indices == [0, 1, 2]
    for c in ctrls.values():
        assert c._fleet_size == 3
        assert c._sector_params is not None
        # centroid of 27.0, 27.001, 27.002 -> 27.001
        assert abs(c._sector_params.base_lat - 27.001) < 1e-6
