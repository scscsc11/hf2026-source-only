"""Tests for SimState dataclass (T011)."""
from dataclasses import FrozenInstanceError
import math
import pytest

from search_track.state import (
    Attitude,
    Detection,
    GeoPosition,
    GimbalState,
    SimState,
    TargetState,
    UavState,
    parse_sim_state,
)


def test_construction_and_immutability():
    s = SimState(
        sim_time=1.0,
        timestamp=1.0,
        status="running",
        uav=UavState(GeoPosition(0, 0, 0), Attitude(0, 0, 0), 10.0, 0.0),
        gimbal=GimbalState(0.0, -30.0, False, 60.0),
        detection=Detection(False, 0.0, None, None),
        target_truth=None,
    )
    with pytest.raises(FrozenInstanceError):
        s.sim_time = 2.0  # type: ignore[misc]


def test_without_truth_strips_target_truth():
    truth = TargetState(GeoPosition(1, 2, 0), 10.0, 90.0)
    s = SimState(
        sim_time=0.0,
        timestamp=0.0,
        status="running",
        uav=UavState(GeoPosition(0, 0, 0), Attitude(0, 0, 0), 0.0, 0.0),
        gimbal=GimbalState(0.0, 0.0, False, None),
        detection=Detection(False, 0.0, None, None),
        target_truth=truth,
    )
    s2 = s.without_truth()
    assert s2.target_truth is None
    assert s.target_truth is truth  # original unchanged


def test_parse_sim_state_minimal():
    raw = {
        "sim_time": 1.5,
        "timestamp": 100.0,
        "status": "running",
        "10002": {
            "platform": {
                "position": {"latitude": 27.0, "longitude": 125.0, "altitude": 300.0},
                "attitude": {"yaw": 90.0, "pitch": 0.0, "roll": 0.0},
            },
            "heading": 90.0,
            "gimbal_tracking": {
                "pan_angle": 10.0,
                "tilt_angle": -45.0,
                "track_enabled": True,
                "detection": {
                    "detected": True,
                    "confidence": 0.9,
                    "target_position": {
                        "latitude": 27.01,
                        "longitude": 125.01,
                        "altitude": 0.0,
                    },
                    "azimuth_error": 1.2,
                },
            },
        },
        "10001": {
            "platform": {
                "position": {"latitude": 27.01, "longitude": 125.01, "altitude": 0.0}
            },
            "speed": 10.0,
            "heading": 90.0,
        },
    }
    s = parse_sim_state(raw, uav_id="10002", target_id="10001")
    assert s.sim_time == 1.5
    assert s.status == "running"
    assert s.uav.position.latitude == 27.0
    assert s.uav.attitude.yaw == 90.0
    assert s.gimbal.pan_angle == 10.0
    assert s.gimbal.track_enabled is True
    assert s.detection.detected is True
    assert s.detection.confidence == 0.9
    assert s.detection.target_position == GeoPosition(27.01, 125.01, 0.0)
    assert s.detection.azimuth_error_deg == 1.2
    assert s.target_truth is not None
    assert s.target_truth.position == GeoPosition(27.01, 125.01, 0.0)


def test_parse_sim_state_missing_fields_use_defaults():
    raw = {}
    s = parse_sim_state(raw, uav_id="10002", target_id="10001")
    assert s.sim_time == 0.0
    assert s.status == "unknown"
    assert s.uav.position == GeoPosition(0.0, 0.0, 0.0)
    assert s.detection.detected is False
    assert s.target_truth is not None
