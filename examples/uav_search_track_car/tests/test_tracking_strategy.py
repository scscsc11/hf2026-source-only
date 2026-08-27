"""Tests for tracking strategy and LOS math (T023)."""
import math
import pytest

from search_track.geometry import los_angles, bearing_deg, haversine_m
from search_track.tracking_strategy import LoiterTracker, TrackerParams


def test_los_pan_zero_when_target_bearing_equals_uav_yaw():
    # target due north of UAV
    pan, tilt = los_angles(27.0, 125.0, 300.0, 0.0, 27.01, 125.0, 0.0)
    assert abs(pan) < 0.5, f"expected pan≈0, got {pan}"


def test_los_pan_180_when_target_behind():
    # target south of UAV (yaw=0 means north-facing)
    pan, tilt = los_angles(27.01, 125.0, 300.0, 0.0, 27.0, 125.0, 0.0)
    # south bearing = 180; relative to yaw=0 → pan=180
    assert abs(abs(pan) - 180.0) < 0.5, f"expected |pan|≈180, got {pan}"


def test_los_tilt_negative_when_looking_down():
    # target on ground, UAV at 300m AGL
    _, tilt = los_angles(27.0, 125.0, 300.0, 0.0, 27.01, 125.0, 0.0)
    assert tilt < 0, f"tilt should be negative (looking down), got {tilt}"


def test_tracker_emits_set_destination_and_set_orientation():
    p = TrackerParams(loiter_radius=200.0, loiter_refresh_period=3.0)
    t = LoiterTracker(p)
    t.reset(27.0, 125.0)
    out = t.commands(
        sim_time=1.0,
        uav_lat=27.0, uav_lon=125.0, uav_alt=300.0, uav_yaw=0.0,
        tgt_lat=27.01, tgt_lon=125.0, tgt_alt=0.0,
    )
    assert len(out) == 2
    cmds = {(c["cmd"]): c for c in out}
    assert "set_destination" in cmds
    assert cmds["set_destination"]["params"]["loiter_radius"] == 200.0
    assert "component.gimbal_tracking.set_orientation" in cmds


def test_tracker_refreshes_loiter_after_period():
    p = TrackerParams(loiter_radius=200.0, loiter_refresh_period=2.0)
    t = LoiterTracker(p)
    t.reset(27.0, 125.0)
    out1 = t.commands(0.0, 27.0, 125.0, 300.0, 0.0, 27.01, 125.0, 0.0)
    out2 = t.commands(1.0, 27.0, 125.0, 300.0, 0.0, 27.05, 125.05, 0.0)  # 1s < 2s: no refresh
    out3 = t.commands(2.5, 27.0, 125.0, 300.0, 0.0, 27.05, 125.05, 0.0)  # >2s: refresh
    # First call at t=0 sets current target to the first target position (27.01)
    # (since the first-frame condition triggers refresh at t=0 regardless of period).
    assert abs(out1[0]["params"]["latitude"] - 27.01) < 1e-6
    # After 1s, target is unchanged (no refresh yet)
    assert abs(out2[0]["params"]["latitude"] - 27.01) < 1e-6
    # After 2.5s, refreshed to 27.05
    assert abs(out3[0]["params"]["latitude"] - 27.05) < 1e-6
