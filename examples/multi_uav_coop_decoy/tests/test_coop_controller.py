"""Tests for 017 cooperative controller (FR-021/022).

Drives a CoopController with synthetic MultiSimState frames (no Redis) and
asserts the cooperation behaviour: per-UAV search/track decisions, comm
broadcast when tracking, inbox consumption.
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

from search_track.coop_controller import CoopController
from search_track.multi_state import EntityState, MultiSimState, parse_multi_sim_state


def _make_state(*, sim_time=0.0, detected=False, in_track_mode=False,
                inbox=None, my_uid="20001", target_type="decoy_vehicle",
                misid_flag=None, target_lat=27.001, target_lon=125.001):
    """Build a minimal MultiSimState with one UAV (my_uid) + one target detection."""
    if misid_flag is None:
        misid_flag = detected and target_type == "decoy_vehicle"
    det = {"detected": detected, "confidence": 0.8 if detected else 0.0,
           "target_position": {"latitude": target_lat, "longitude": target_lon, "altitude": 0.0}
           if detected else None,
           "target_type": target_type if detected else "",
           "misid_flag": misid_flag}
    uav_entry = {
        "type": "fixed_wing_uav", "name": "uav_alpha",
        "platform": {"position": {"latitude": 27.0, "longitude": 125.0, "altitude": 300.0},
                     "attitude": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}},
        "velocity": 20.0, "heading": 0.0,
        "gimbal_tracking": {"pan_angle": 0.0, "tilt_angle": -45.0,
                             "track_enabled": True, "detection": det},
        "comm": {"enabled": True, "range_m": 1000.0, "max_bytes": 50,
                 "max_rate_hz": 4.0, "inbox": inbox or [],
                 "stats": {"sent": 0, "delivered": 0, "received": 0,
                           "rejected_bytes": 0, "rejected_rate": 0,
                           "rejected_range": 0, "rejected_jam": 0}},
    }
    raw = {"sim_time": sim_time, "timestamp": sim_time, "status": "running",
           my_uid: uav_entry}
    return parse_multi_sim_state(raw)


def _configured_controller(my_uid="20001"):
    c = CoopController(my_uid=my_uid)
    c.configure({
        "k_acquire": 1,  # enter TRACK after 1 detected tick (fast for tests)
        "k_lost": 60,
        "search_radius": 500.0,
        "search_altitude_agl": 300.0,
        "loiter_radius": 200.0,
        "sweep_period": 4.0,
        "coop_broadcast_period": 1.0,
    })
    c.reset()
    return c


def test_search_mode_emits_set_destination():
    c = _configured_controller()
    state = _make_state(detected=False)
    cmds = c.decide(state, dt=0.1)
    # SEARCH mode should emit at least a set_destination command.
    assert any(cmd["cmd"] == "set_destination" for cmd in cmds)
    assert c.mode == "SEARCH"


def test_track_mode_enters_after_consecutive_detections():
    c = _configured_controller()
    # Tick 1: detected ground_vehicle -> k_acquire=1 -> enter TRACK.
    state = _make_state(detected=True, sim_time=0.1,
                        target_type="ground_vehicle", misid_flag=False)
    c.decide(state, dt=0.1)
    assert c.mode == "TRACK"


def test_track_mode_broadcasts_tracking_message():
    c = _configured_controller()
    # Enter TRACK on a ground_vehicle.
    s1 = _make_state(detected=True, sim_time=0.1,
                     target_type="ground_vehicle", misid_flag=False)
    c.decide(s1, dt=0.1)
    assert c.mode == "TRACK"
    # Next tick: should broadcast "T:..." (cooperation, FR-021).
    s2 = _make_state(detected=True, sim_time=1.2,
                     target_type="ground_vehicle", misid_flag=False)
    cmds = c.decide(s2, dt=0.1)
    bcast_cmds = [c for c in cmds if c["cmd"] == "comm.broadcast"]
    assert len(bcast_cmds) >= 1
    assert bcast_cmds[0]["unique_id"] == "20001"


def test_inbox_consumption_records_peer_tracking():
    c = _configured_controller()
    inbox = [{"sender": "20002", "payload": "T:10001", "recv_time": 0.5}]
    state = _make_state(detected=False, sim_time=0.6, inbox=inbox)
    c.decide(state, dt=0.1)
    # The controller should have recorded peer 20002 tracking target 10001.
    assert "10001" in c._peer_tracking
    assert c._peer_tracking["10001"] == "20002"


def test_misid_ticks_accumulate_when_tracking_decoy():
    c = _configured_controller()
    # Enter TRACK on a decoy (misid_flag=True via _make_state).
    s1 = _make_state(detected=True, sim_time=0.1)
    c.decide(s1, dt=0.1)
    # The misid_track_ticks counter accumulates while detected + misid_flag.
    assert c.misid_track_ticks >= 1


def test_returns_empty_when_self_not_in_state():
    c = _configured_controller(my_uid="99999")  # not in state
    state = _make_state(detected=False, my_uid="20001")
    cmds = c.decide(state, dt=0.1)
    assert cmds == []


def test_returns_empty_before_configure():
    c = CoopController(my_uid="20001")
    state = _make_state(detected=False)
    cmds = c.decide(state, dt=0.1)
    assert cmds == []


def test_sector_search_fans_uavs_apart():
    """Two UAVs with different fleet indices must aim at different waypoints
    in SEARCH mode (the fix for the 'all circling on top of each other' bug)."""
    base_cfg = {
        "k_acquire": 5, "k_lost": 60,
        "search_radius": 800.0, "search_altitude_agl": 300.0,
        "loiter_radius": 200.0, "sweep_period": 4.0,
        "coop_broadcast_period": 1.0,
        "use_sector_search": True, "expand_time": 30.0,
        "sector_angular_speed_dps": 40.0,
        "initial_radius_frac": 0.15, "radius_dither_frac": 0.08,
        "sector_center_latitude": 27.0, "sector_center_longitude": 125.0,
        "sweep_pitch_min": -60.0, "sweep_pitch_max": -30.0,
    }
    c0 = CoopController(my_uid="20001"); c0.configure(base_cfg); c0.reset()
    c1 = CoopController(my_uid="20002"); c1.configure(base_cfg); c1.reset()
    c0.set_fleet_index(0, 2)
    c1.set_fleet_index(1, 2)
    # Build a state with BOTH UAVs so each controller can find itself.
    uav_entry = {
        "type": "fixed_wing_uav", "name": "uav_alpha",
        "platform": {"position": {"latitude": 27.0, "longitude": 125.0,
                                  "altitude": 300.0},
                     "attitude": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}},
        "velocity": 20.0, "heading": 0.0,
        "gimbal_tracking": {"pan_angle": 0.0, "tilt_angle": -45.0,
                             "track_enabled": True,
                             "detection": {"detected": False, "confidence": 0.0,
                                           "target_position": None,
                                           "target_type": "",
                                           "misid_flag": False}},
        "comm": {"enabled": True, "range_m": 1000.0, "max_bytes": 50,
                 "max_rate_hz": 4.0, "inbox": [],
                 "stats": {"sent": 0, "delivered": 0, "received": 0,
                           "rejected_bytes": 0, "rejected_rate": 0,
                           "rejected_range": 0, "rejected_jam": 0}},
    }
    raw = {"sim_time": 5.0, "timestamp": 5.0, "status": "running",
           "20001": uav_entry, "20002": uav_entry}
    state = parse_multi_sim_state(raw)
    cmds0 = c0.decide(state, dt=0.1)
    cmds1 = c1.decide(state, dt=0.1)
    def _dest(cmds):
        for c in cmds:
            if c.get("cmd") == "set_destination":
                return (c["params"]["latitude"], c["params"]["longitude"])
        return None
    d0, d1 = _dest(cmds0), _dest(cmds1)
    assert d0 is not None and d1 is not None
    # The two waypoints should be far apart (different sectors).
    assert abs(d0[0] - d1[0]) > 1e-4 or abs(d0[1] - d1[1]) > 1e-4


def test_sector_search_can_be_disabled_falls_back_to_spiral():
    """With use_sector_search=False the controller keeps using the FSM spiral."""
    c = _configured_controller_with({"use_sector_search": False})
    state = _make_state(detected=False, sim_time=5.0)
    cmds = c.decide(state, dt=0.1)
    assert any(cmd["cmd"] == "set_destination" for cmd in cmds)
    assert c.mode == "SEARCH"


def _configured_controller_with(overrides: dict) -> CoopController:
    cfg = {
        "k_acquire": 1, "k_lost": 60,
        "search_radius": 500.0, "search_altitude_agl": 300.0,
        "loiter_radius": 200.0, "sweep_period": 4.0,
        "coop_broadcast_period": 1.0,
        "use_sector_search": True, "expand_time": 30.0,
        "sector_angular_speed_dps": 40.0,
        "initial_radius_frac": 0.15, "radius_dither_frac": 0.08,
        "sweep_pitch_min": -60.0, "sweep_pitch_max": -30.0,
        **overrides,
    }
    c = CoopController(my_uid="20001")
    c.configure(cfg)
    c.reset()
    return c


def test_published_uav_commands_include_unique_id():
    """Every UAV-targeted command must carry unique_id so the C++ command
    router dispatches by ID (entity_handler_by_unique_id_) instead of
    falling back to the entity-NAME path. Without unique_id, target='uav'
    matches no entity in a multi-UAV scenario (uav_alpha/bravo/charlie)
    and TRACK-mode set_destination is silently dropped."""
    c = _configured_controller(my_uid="20001")
    state = _make_state(detected=False, my_uid="20001")
    cmds = c.decide(state, dt=0.1)
    uav_cmds = [d for d in cmds
                if d.get("cmd") in ("set_destination",
                                    "component.gimbal_tracking.set_orientation")]
    assert uav_cmds, "expected at least one UAV control command in SEARCH"
    for d in uav_cmds:
        assert d.get("unique_id") == "20001", (
            f"UAV command missing unique_id: {d}")


def test_target_position_jump_triggers_loss():
    """When the auto-track gimbal silently switches from one target to a
    far-away one, the controller must surface this as TRACK→SEARCH so
    run.py emits state.exit_track. We verify by feeding TRACK with a
    detection at one location, then jumping the reported position past
    the threshold; within k_lost ticks the FSM must visit SEARCH."""
    # Use k_acquire=10 so re-acquiring TRACK takes longer than the test
    # window and we can see the SEARCH state once it's entered.
    c = _configured_controller_with({"k_acquire": 10, "k_lost": 5})
    # Tick 1: enter TRACK at the original target position. Need k_acquire
    # consecutive detections, so feed a few then check.
    for i in range(12):
        s = _make_state(detected=True, sim_time=0.1 + i * 0.1, my_uid="20001")
        c.decide(s, dt=0.1)
    assert c.mode == "TRACK"
    # Now feed ticks with a detection that has jumped >>80m (here ~1.1km).
    # The anchor stays at the original 27.001/125.001 because every tick
    # is out of tolerance, so consecutive_lost accumulates and after
    # k_lost=5 ticks we transition to SEARCH.
    saw_search = False
    for i in range(8):
        det = {"detected": True, "confidence": 0.7,
               "target_position": {"latitude": 27.011,
                                   "longitude": 125.011, "altitude": 0.0},
               "target_type": "ground_vehicle", "misid_flag": False}
        uav_entry = {
            "type": "fixed_wing_uav", "name": "uav_alpha",
            "platform": {"position": {"latitude": 27.0, "longitude": 125.0,
                                      "altitude": 300.0},
                         "attitude": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}},
            "velocity": 20.0, "heading": 0.0,
            "gimbal_tracking": {"pan_angle": 0.0, "tilt_angle": -45.0,
                                "track_enabled": True, "detection": det},
            "comm": {"enabled": True, "range_m": 1000.0, "max_bytes": 50,
                     "max_rate_hz": 4.0, "inbox": [],
                     "stats": {"sent": 0, "delivered": 0, "received": 0,
                               "rejected_bytes": 0, "rejected_rate": 0,
                               "rejected_range": 0, "rejected_jam": 0}},
        }
        raw = {"sim_time": 1.4 + i * 0.1, "timestamp": 0.0,
               "status": "running", "20001": uav_entry}
        c.decide(parse_multi_sim_state(raw), dt=0.1)
        if c.mode == "SEARCH":
            saw_search = True
            break
    assert saw_search, "FSM never transitioned to SEARCH after target jump"


def test_anchor_tracks_slow_target_motion():
    """A genuinely moving target (slow drift below the jump threshold per
    refresh window) must NOT be treated as a loss. The anchor rolls
    forward each tick so legitimate target motion stays anchored."""
    c = _configured_controller_with({"k_acquire": 1, "k_lost": 5})
    # Enter TRACK on a ground_vehicle.
    c.decide(_make_state(detected=True, sim_time=0.1, my_uid="20001",
                         target_type="ground_vehicle", misid_flag=False),
             dt=0.1)
    assert c.mode == "TRACK"
    # Feed many ticks where the target drifts 5m per tick — well below
    # the 80m threshold, so the anchor should roll along with it.
    base_lat = 27.001
    for i in range(20):
        # 5m/tick north ≈ 4.5e-5 deg lat/tick
        det = {"detected": True, "confidence": 0.7,
               "target_position": {"latitude": base_lat + i * 4.5e-5,
                                   "longitude": 125.001, "altitude": 0.0},
               "target_type": "ground_vehicle", "misid_flag": False}
        uav_entry = {
            "type": "fixed_wing_uav", "name": "uav_alpha",
            "platform": {"position": {"latitude": 27.0, "longitude": 125.0,
                                      "altitude": 300.0},
                         "attitude": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}},
            "velocity": 20.0, "heading": 0.0,
            "gimbal_tracking": {"pan_angle": 0.0, "tilt_angle": -45.0,
                                "track_enabled": True, "detection": det},
            "comm": {"enabled": True, "range_m": 1000.0, "max_bytes": 50,
                     "max_rate_hz": 4.0, "inbox": [],
                     "stats": {"sent": 0, "delivered": 0, "received": 0,
                               "rejected_bytes": 0, "rejected_rate": 0,
                               "rejected_range": 0, "rejected_jam": 0}},
        }
        raw = {"sim_time": 0.2 + i * 0.1, "timestamp": 0.0,
               "status": "running", "20001": uav_entry}
        c.decide(parse_multi_sim_state(raw), dt=0.1)
    # A 20-tick × 5m = 100m total drift would exceed the threshold if
    # measured against the original anchor — but we roll, so each tick's
    # 5m is well within tolerance and TRACK persists.
    assert c.mode == "TRACK"


def test_rejected_decoy_detection_is_ignored_in_search():
    c = _configured_controller()
    c._decoy_avoid.add("27.001,125.001")
    # k_acquire=1 would immediately enter TRACK if the decoy detection were
    # accepted.  Avoided decoys are spoofed as not-detected instead.
    state = _make_state(detected=True, sim_time=0.1)
    c.decide(state, dt=0.1)
    assert c.mode == "SEARCH"


def test_rejected_decoy_detection_is_ignored_by_spatial_cooldown():
    c = _configured_controller()
    c._decoy_cooldowns.append((27.001, 125.001, 10.0))
    # Slightly shifted report (~15m) should still be considered the same
    # rejected decoy while the cooldown is active.
    state = _make_state(detected=True, sim_time=1.0,
                        target_lat=27.0011, target_lon=125.0011)
    c.decide(state, dt=0.1)
    assert c.mode == "SEARCH"


def test_expired_decoy_spatial_cooldown_allows_detection_again():
    c = _configured_controller()
    c._decoy_cooldowns.append((27.001, 125.001, 0.5))
    state = _make_state(detected=True, sim_time=1.0,
                        target_type="ground_vehicle", misid_flag=False,
                        target_lat=27.0011, target_lon=125.0011)
    c.decide(state, dt=0.1)
    assert c.mode == "TRACK"


def test_search_stage_cooldown_zone_blocks_reacquire():
    c = _configured_controller()
    c._fsm._mode = "SEARCH"
    c._decoy_cooldowns.append((27.001, 125.001, 10.0))
    # A slightly shifted detection inside the cooldown radius must not
    # accumulate k_acquire and must not enter TRACK.
    state = _make_state(detected=True, sim_time=1.0,
                        target_type="decoy_vehicle", misid_flag=True,
                        target_lat=27.0011, target_lon=125.0011)
    c.decide(state, dt=0.1)
    assert c.mode == "SEARCH"
    assert c._fsm._consecutive_detected == 0


def test_classifier_real_latches_track_active_flag():
    c = _configured_controller()
    c._fsm._mode = "TRACK"
    c._clf = None
    c._real_track_active = False
    state = _make_state(detected=True, sim_time=2.0,
                        target_type="ground_vehicle", misid_flag=False)
    me = state.entities["20001"]

    class RealClassifier:
        samples = []
        started_at = 1.0
        def observe(self, *_args):
            return "real"
    c._clf = RealClassifier()
    c._noncoop_track_reject(state, me, was_tracking_pre=True)
    assert c._real_track_active is True


def test_force_search_clears_real_track_active():
    c = _configured_controller()
    c._real_track_active = True
    c._force_search()
    assert c._real_track_active is False


def test_real_target_identity_lock_blocks_other_detections_in_search():
    c = _configured_controller()
    c._fsm._mode = "SEARCH"
    c._real_track_active = True
    c._real_target_key = "27.0010,125.0010"
    # Different target (e.g. engine hopping to a decoy): must be spoofed.
    state = _make_state(detected=True, sim_time=1.0,
                        target_type="decoy_vehicle", misid_flag=True,
                        target_lat=27.003, target_lon=125.003)
    c.decide(state, dt=0.1)
    assert c._fsm._consecutive_detected == 0


def test_search_stage_cooldown_zone_resets_fsm_state():
    c = _configured_controller()
    c._fsm._mode = "SEARCH"
    c._fsm._consecutive_detected = 2
    c._decoy_cooldowns.append((27.001, 125.001, 10.0))
    state = _make_state(detected=True, sim_time=1.0,
                        target_type="decoy_vehicle", misid_flag=True,
                        target_lat=27.0011, target_lon=125.0011)
    c.decide(state, dt=0.1)
    assert c._fsm._mode == "SEARCH"
    assert c._fsm._consecutive_detected == 0


def test_search_waypoint_is_pushed_out_of_decoy_cooldown():
    c = _configured_controller()
    c._decoy_cooldowns.append((27.001, 125.001, 10.0))
    lat, lon = c._push_point_out_of_decoy_cooldown(27.001, 125.001, 1.0)
    assert c._point_in_decoy_cooldown(lat, lon, 1.0) is False


def test_search_sweep_avoids_recent_decoy_bearing():
    c = _configured_controller()
    c._search_t0 = 0.0
    c._decoy_avoid_pan = 0.0
    c._decoy_cooldown_until = 10.0
    # At t=2s with the default 4s sweep period, raw sweep pan would be 0°.
    cmds = c._search_commands_sector(sim_time=2.0)
    gimbal = next(cmd for cmd in cmds
                  if cmd.cmd == "component.gimbal_tracking.set_orientation")
    assert abs(float(gimbal.params["pan"])) >= c._decoy_avoid_margin_deg


def test_real_decision_does_not_commit_in_sector_spread_mode():
    c = _configured_controller()
    c._fsm._mode = "TRACK"
    c._clf = None
    state = _make_state(detected=True, sim_time=2.0,
                        target_type="ground_vehicle", misid_flag=False)
    me = state.entities["20001"]
    # Pretend the motion classifier has already confirmed this detection.
    class RealClassifier:
        samples = []
        started_at = 1.0
        def observe(self, *_args):
            return "real"
    c._clf = RealClassifier()
    c._noncoop_track_reject(state, me, was_tracking_pre=True)
    assert c._last_real_sim_time == 2.0
    assert c._cur_real_key is None
    assert c._known_real == {}


def test_track_decoy_gate_spoofs_misid_before_fsm():
    c = _configured_controller()
    state = _make_state(detected=True, sim_time=2.0,
                        target_type="decoy_vehicle", misid_flag=True)
    me = state.entities["20001"]
    gated = c._maybe_track_decoy_gate(me, was_tracking=True)
    assert gated.detection is not None
    assert gated.detection.detected is False
    assert gated.detection.target_position is None


def test_decide_resets_fsm_state_on_decoy_cooldown_detection():
    c = _configured_controller()
    c._fsm._mode = "TRACK"
    c._decoy_cooldowns.append((27.001, 125.001, 10.0))
    state = _make_state(detected=True, sim_time=1.0,
                        target_type="decoy_vehicle", misid_flag=True)
    c.decide(state, dt=0.1)
    # Decoy-zone detection must force FSM back to SEARCH so the k_acquire
    # loop cannot push the controller into TRACK again.
    assert c._fsm.mode == "SEARCH"
    assert c._fsm._consecutive_detected == 0


def test_decoy_detection_does_not_pull_confirmed_real_filter():
    c = _configured_controller()
    c._cur_real_key = "real"
    c._known_real["real"] = (27.001, 125.001, 1.0)
    state = _make_state(detected=True, sim_time=2.0,
                        target_type="decoy_vehicle", misid_flag=True,
                        target_lat=27.003, target_lon=125.003)
    me = state.entities["20001"]
    c._committed_update(state, me, dt=0.1)
    lat, lon, _ = c._known_real["real"]
    assert (lat, lon) == (27.001, 125.001)
    assert "27.003,125.003" in c._decoy_avoid


def test_classifier_does_not_release_confirmed_real_track():
    c = _configured_controller()
    c._coop_summon = True
    c._fsm._mode = "TRACK"
    c._cur_real_key = "real"
    c._known_real["real"] = (27.001, 125.001, 1.0)
    state = _make_state(detected=True, sim_time=2.0,
                        target_type="decoy_vehicle", misid_flag=True,
                        target_lat=27.003, target_lon=125.003)
    me = state.entities["20001"]
    c._noncoop_track_reject(state, me, was_tracking_pre=True)
    assert c._cur_real_key == "real"
    assert c._fsm.mode == "TRACK"


def test_confirmed_real_filter_predicts_motion_without_detection():
    c = _configured_controller()
    c._cur_real_key = "real"
    c._known_real["real"] = (27.001, 125.001, 1.0)
    c._real_last_meas_t = 1.0
    c._real_vel_lat_dps = 0.0001
    c._real_vel_lon_dps = 0.0002
    state = _make_state(detected=False, sim_time=3.0)
    me = state.entities["20001"]
    c._committed_update(state, me, dt=0.1)
    lat, lon, t = c._known_real["real"]
    assert abs(lat - 27.0012) < 1e-9
    assert abs(lon - 125.0014) < 1e-9
    assert t == 3.0


def test_confirmed_real_filter_releases_after_coast_timeout():
    c = _configured_controller()
    c._cur_real_key = "real"
    c._known_real["real"] = (27.001, 125.001, 1.0)
    c._real_last_meas_t = 1.0
    state = _make_state(detected=False, sim_time=4.0)
    me = state.entities["20001"]
    c._committed_update(state, me, dt=0.1)
    assert c._cur_real_key is None
    assert c._fsm.mode == "SEARCH"


def test_unrealistic_real_measurement_speed_is_rejected():
    c = _configured_controller()
    c._cur_real_key = "real"
    c._known_real["real"] = (27.001, 125.001, 1.0)
    c._real_last_meas_t = 1.0
    ok = c._update_real_filter(1.1, 27.003, 125.003)
    assert ok is False
    assert c._known_real["real"] == (27.001, 125.001, 1.0)


def test_committed_track_uses_standoff_destination_not_target_center():
    c = _configured_controller()
    c._cur_real_key = "real"
    c._known_real["real"] = (27.001, 125.001, 1.0)
    state = _make_state(detected=False, sim_time=1.0)
    me = state.entities["20001"]
    cmds = c._committed_track_commands(state, me)
    dest = next(cmd for cmd in cmds if cmd.cmd == "set_destination")
    assert abs(dest.params["latitude"] - 27.001) > 1e-5 or \
        abs(dest.params["longitude"] - 125.001) > 1e-5
    assert dest.params["loiter_radius"] == c._track_orbit_radius_m
    assert any(cmd.cmd == "component.gimbal_tracking.set_orientation"
               for cmd in cmds)
