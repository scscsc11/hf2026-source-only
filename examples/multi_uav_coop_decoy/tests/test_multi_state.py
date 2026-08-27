"""Tests for 017 multi-entity state parsing."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root + example dir on path when run via pytest from repo root.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
EXAMPLE_DIR = HERE.parents[1]
for p in (str(REPO_ROOT), str(EXAMPLE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from search_track.multi_state import parse_multi_sim_state


def test_parse_three_uavs_three_targets_fifteen_decoys():
    raw = {
        "timestamp": 1.0, "sim_time": 1.0, "status": "running",
        "step_perf": {},
        "20001": {"type": "fixed_wing_uav", "name": "uav_alpha",
                  "platform": {"position": {"latitude": 27.0, "longitude": 125.0, "altitude": 300.0},
                               "attitude": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}},
                  "velocity": 20.0, "heading": 0.0,
                  "gimbal_tracking": {"pan_angle": 0.0, "tilt_angle": -45.0,
                                       "track_enabled": True,
                                       "detection": {"detected": False, "confidence": 0.0}},
                  "comm": {"enabled": True, "range_m": 1000.0, "max_bytes": 50,
                           "max_rate_hz": 4.0, "inbox": [],
                           "stats": {"sent": 1, "delivered": 1, "received": 0,
                                     "rejected_bytes": 0, "rejected_rate": 0,
                                     "rejected_range": 0, "rejected_jam": 0}}},
        "10001": {"type": "ground_vehicle", "name": "target_1",
                  "platform": {"position": {"latitude": 27.005, "longitude": 124.998, "altitude": 0.0}},
                  "speed": 8.0, "heading": 0.0},
        "30001": {"type": "decoy_vehicle", "name": "decoy_01",
                  "platform": {"position": {"latitude": 27.003, "longitude": 125.003, "altitude": 0.0}},
                  "speed": 0.0, "heading": 0.0},
    }
    state = parse_multi_sim_state(raw)
    assert state.sim_time == 1.0
    assert state.status == "running"
    assert len(state.entities) == 3
    assert state.entities["20001"].kind == "uav"
    assert state.entities["20001"].uav is not None
    assert state.entities["20001"].comm is not None
    assert state.entities["20001"].comm.stats.sent == 1
    assert state.entities["10001"].kind == "ground_vehicle"
    assert state.entities["10001"].vehicle_truth is not None
    assert state.entities["30001"].kind == "decoy_vehicle"


def test_parse_skips_non_entity_keys():
    raw = {"timestamp": 0.0, "sim_time": 0.0, "status": "running",
           "sim_time_str": "2026-01-01T00:00:00Z", "step_perf": {"ms": 1.0}}
    state = parse_multi_sim_state(raw)
    assert state.entities == {}


def test_parse_extended_detection_fields():
    raw = {
        "sim_time": 0.0, "status": "running",
        "20001": {"type": "fixed_wing_uav", "name": "uav",
                  "platform": {"position": {"latitude": 0, "longitude": 0, "altitude": 0},
                               "attitude": {"yaw": 0, "pitch": 0, "roll": 0}},
                  "gimbal_tracking": {"pan_angle": 0, "tilt_angle": 0,
                                       "track_enabled": True,
                                       "detection": {"detected": True, "confidence": 0.7,
                                                     "target_position": {"latitude": 1, "longitude": 2, "altitude": 0},
                                                     "target_type": "decoy_vehicle",
                                                     "misid_flag": True,
                                                     "misid_count": 3,
                                                     "misid_track_duration": 1.5}}},
    }
    state = parse_multi_sim_state(raw)
    det = state.entities["20001"].detection
    assert det is not None
    assert det.detected is True
    assert det.target_type == "decoy_vehicle"
    assert det.misid_flag is True
    assert det.misid_count == 3
    assert det.misid_track_duration == 1.5
