"""Tests for FSM state transitions and invariants (T024)."""
import pytest

from search_track.config import AlgorithmConfig
from search_track.fsm_controller import FsmSearchTrackController
from search_track.commands import ControlCommand

from .fixtures.states import make_state


@pytest.fixture
def controller():
    c = FsmSearchTrackController()
    cfg = AlgorithmConfig(
        controller="search_track.fsm_controller:FsmSearchTrackController",
        seed=None, mode="spiral",
        search_radius=500.0, search_altitude_agl=300.0,
        sweep_period=4.0, loiter_radius=200.0,
        advanced={"k_acquire": 5, "k_lost": 60, "control_rate_hz": 10,
                  "spiral_growth_rate": 30.0, "sweep_pitch_min": -60.0,
                  "sweep_pitch_max": -30.0, "loiter_refresh_period": 3.0},
    )
    c.configure(cfg)
    return c


def _feed(c, n, *, detected):
    for _ in range(n):
        s = make_state(
            sim_time=0.1, detected=detected,
            tgt_lat=27.01, tgt_lon=125.0, tgt_alt=0.0,
        )
        c.decide(s, 0.1)


def test_search_to_track_after_k_acquire(controller):
    assert controller.mode == "SEARCH"
    _feed(controller, 4, detected=True)
    assert controller.mode == "SEARCH", "4 < k_acquire=5; should stay SEARCH"
    _feed(controller, 1, detected=True)
    assert controller.mode == "TRACK"


def test_track_to_search_after_k_lost(controller):
    _feed(controller, 6, detected=True)
    assert controller.mode == "TRACK"
    _feed(controller, 59, detected=False)
    assert controller.mode == "TRACK", "59 < k_lost=60; should stay TRACK"
    _feed(controller, 1, detected=False)
    assert controller.mode == "SEARCH"


def test_search_emits_set_destination_and_set_orientation(controller):
    out = controller.decide(make_state(detected=False), 0.1)
    cmds = {c.cmd for c in out}
    assert "set_destination" in cmds
    assert "component.gimbal_tracking.set_orientation" in cmds


def test_search_never_emits_set_target_entity(controller):
    for _ in range(50):
        out = controller.decide(make_state(detected=False), 0.1)
        for c in out:
            assert c.cmd != "set_target_entity"
        _feed(controller, 1, detected=False)


def test_track_emits_set_orientation_with_los(controller):
    _feed(controller, 6, detected=True)
    assert controller.mode == "TRACK"
    out = controller.decide(
        make_state(
            detected=True, tgt_lat=27.01, tgt_lon=125.0, tgt_alt=0.0,
            uav_lat=27.0, uav_lon=125.0, uav_alt=300.0, uav_yaw=0.0,
        ),
        0.1,
    )
    cmds = {c.cmd: c for c in out}
    assert "component.gimbal_tracking.set_orientation" in cmds
    # pan should be near 0 since target is due north and yaw=0
    p = cmds["component.gimbal_tracking.set_orientation"].params["pan"]
    assert abs(p) < 1.0


def test_decide_returns_at_most_five_commands(controller):
    for _ in range(200):
        out = controller.decide(make_state(detected=True, tgt_lat=27.01, tgt_lon=125.0), 0.1)
        assert len(out) <= 5, f"got {len(out)} commands"
