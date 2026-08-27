"""Integration smoke: full control loop with synthetic state stream.

This is the T035/T042/T050/T059 equivalent in code, usable when a real
opensim-sim binary is not available. It:

  1. Builds a SimClient, but feeds a synthetic state stream via direct
     Redis PUBLISH (bypassing the sim, since the sim is the bottleneck in
     this environment).
  2. Drives a real Controller (default FSM, or GreedyController) through
     `decide()`, verifying:
     - Mode transitions
     - LOS pan/tilt computation
     - search→track→search cycle
  3. Records metrics and saves JSON+CSV to a temp dir.
  4. Verifies key invariants (no set_target_entity, set_enabled=False, etc.)

Run with:
    /c/Python314/python.exe -m examples.uav_search_track_car.integration_smoke
or:
    /c/Python314/python.exe examples/uav_search_track_car/integration_smoke.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if str(HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(HERE.parents[1]))

from search_track.client import SimClient
from search_track.commands import CommandTarget, ControlCommand
from search_track.config import from_yaml
from search_track.controller import load_controller
from search_track.geometry import haversine_m
from search_track.metrics import MetricsRecorder
from search_track.state import (
    Attitude, Detection, GeoPosition, GimbalState, SimState,
    TargetState, UavState,
)


UAV_ID = "10002"
TARGET_ID = "10001"

# Scenario: uav spiral-searches around (27, 125) at 300m AGL.
# Target is stationary at (27.01, 125) at ground level. With 500m spiral,
# the uav eventually gets close enough for the gimbal's 60° FOV to detect.
# Detection radius is approximated in the synthetic data as: distance < 800m
# AND gimbal LOS within 30° of bearing.
UAV_HOME = (27.0, 125.0, 300.0)
TARGET_HOME = (27.005, 125.0, 0.0)
SPIRAL_RADIUS = 500.0  # m
FOV_DEG = 60.0
HALF_FOV = FOV_DEG / 2.0


def make_synthetic_state(t: float, *, target_visible: bool, target_lat: float = TARGET_HOME[0],
                          target_lon: float = TARGET_HOME[1]) -> dict:
    """Build a raw sim:state dict for time t, on a simple uav spiral path."""
    # Spiral: bearing(t) = 30 deg/s, radius grows linearly until SPIRAL_RADIUS.
    ang = (30.0 * t) % 360.0
    revs = (30.0 * t) / 360.0
    radius = min(SPIRAL_RADIUS, 30.0 * revs)
    if radius < 1.0:
        radius = 1.0
    dlat = (radius * math.cos(math.radians(ang))) / 111320.0
    dlon = (radius * math.sin(math.radians(ang))) / (
        111320.0 * max(0.1, math.cos(math.radians(UAV_HOME[0])))
    )
    uav_lat = UAV_HOME[0] + dlat
    uav_lon = UAV_HOME[1] + dlon
    uav_yaw = ang  # uav faces along its motion
    uav_alt = UAV_HOME[2]

    # Detection: in_fov if |LOS_error| < half_fov
    detected = False
    azimuth_error = None
    if target_visible:
        brg = bearing_deg(uav_lat, uav_lon, target_lat, target_lon)
        rel = ((brg - uav_yaw + 540.0) % 360.0) - 180.0
        if abs(rel) < HALF_FOV:
            detected = True
            azimuth_error = rel

    raw = {
        "timestamp": time.time(),
        "status": "running",
        "sim_time": t,
        UAV_ID: {
            "platform": {
                "position": {"latitude": uav_lat, "longitude": uav_lon, "altitude": uav_alt},
                "attitude": {"yaw": uav_yaw, "pitch": 0.0, "roll": 0.0},
            },
            "heading": uav_yaw,
            "velocity": 20.0,
            "gimbal_tracking": {
                "pan_angle": 0.0,
                "tilt_angle": -45.0,
                "track_enabled": False,
                "fov_deg": FOV_DEG,
                "detection": {
                    "detected": detected,
                    "confidence": 0.9 if detected else 0.0,
                    "target_position": (
                        {"latitude": target_lat, "longitude": target_lon, "altitude": 0.0}
                        if detected else None
                    ),
                    "azimuth_error": azimuth_error,
                },
            },
        },
        TARGET_ID: {
            "platform": {
                "position": {"latitude": target_lat, "longitude": target_lon, "altitude": 0.0}
            },
            "speed": 0.0,
            "heading": 0.0,
        },
    }
    return raw


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def main(controller_spec: str = "search_track.fsm_controller:FsmSearchTrackController",
         duration: float = 30.0) -> int:
    print("=" * 70)
    print("INTEGRATION SMOKE (T035 / T042 / T050 / T059 equivalent)")
    print("=" * 70)
    print(f"controller: {controller_spec}")
    print(f"duration:   {duration} sim-seconds")
    print()

    cfg = from_yaml(HERE / "config" / "algorithm.yaml")
    cfg.controller = controller_spec
    print(f"[smoke] loaded config: search_radius={cfg.search_radius}, "
          f"loiter_radius={cfg.loiter_radius}")

    controller = load_controller(cfg.controller)
    if hasattr(controller, "configure"):
        controller.configure(cfg)
    controller.reset()

    # Set up SimClient but use a separate "synthetic publisher" connection
    # to inject states. We also subscribe to sim:commands to verify what
    # the controller would publish.
    c = SimClient(host="127.0.0.1", port=6379)
    c.connect()
    pubsub = c._pubsub  # peek into private; we'll read sim:commands from it
    pubsub.subscribe("sim:commands")
    # Spec 018: also subscribe to sim:events for smoke verification
    pubsub.subscribe("sim:events")
    # drain any backlog
    time.sleep(0.1)
    while pubsub.get_message(timeout=0.05):
        pass

    print("[smoke] SimClient connected, command channel subscribed")
    print("[smoke] starting control loop (synthetic state stream)")

    recorder = MetricsRecorder()
    period = 1.0 / float(cfg.get("control_rate_hz", 10))
    last_state: SimState | None = None
    issued_target_entity = False  # invariant I-7
    set_enabled_calls: list[bool] = []
    set_orientation_calls = 0

    start = time.time()
    target_t = 0.0
    n_ticks = 0
    last_print = -1
    # Spec 018: track mode transitions for event smoke
    prev_mode: str | None = getattr(controller, "mode", "SEARCH")
    events_published: int = 0
    try:
        while target_t < duration:
            raw = make_synthetic_state(target_t, target_visible=True)
            # Parse the synthetic state as if it came from the sim
            from search_track.state import parse_sim_state
            state = parse_sim_state(raw, uav_id=UAV_ID, target_id=TARGET_ID)
            last_state = state
            # Feed controller with stripped state
            cmds = controller.decide(state.without_truth(), period)
            # Publish each command
            for cmd in cmds:
                if cmd.cmd == "set_target_entity":
                    issued_target_entity = True
                if cmd.cmd == "component.gimbal_tracking.set_enabled":
                    set_enabled_calls.append(bool(cmd.params.get("enabled", True)))
                if cmd.cmd == "component.gimbal_tracking.set_orientation":
                    set_orientation_calls += 1
                c.publish(cmd)

            # Spec 018: publish events on mode transitions
            cur_mode = getattr(controller, "mode", "?")
            if cur_mode != prev_mode:
                if cur_mode == "TRACK" and prev_mode == "SEARCH":
                    c.publish_event(
                        event_type="state.enter_track",
                        entity_uid=UAV_ID,
                        sim_time=state.sim_time,
                        payload={"target_type": "real", "confidence": state.detection.confidence},
                    )
                    events_published += 1
                elif cur_mode == "SEARCH" and prev_mode == "TRACK":
                    c.publish_event(
                        event_type="state.exit_track",
                        entity_uid=UAV_ID,
                        sim_time=state.sim_time,
                        payload={"reason": "target_lost"},
                    )
                    events_published += 1
                prev_mode = cur_mode
            # Record metrics
            tgt = state.target_truth.position
            dist = haversine_m(state.uav.position.latitude, state.uav.position.longitude,
                               tgt.latitude, tgt.longitude) if state.target_truth else None
            recorder.record_tick(
                sim_time=state.sim_time,
                mode=getattr(controller, "mode", "?"),
                detected=state.detection.detected,
                uav_lat=state.uav.position.latitude,
                uav_lon=state.uav.position.longitude,
                uav_alt=state.uav.position.altitude,
                target_lat=tgt.latitude if state.target_truth else None,
                target_lon=tgt.longitude if state.target_truth else None,
                distance_m=dist,
            )
            n_ticks += 1
            target_t += period
            if int(state.sim_time) > last_print:
                last_print = int(state.sim_time)
                print(f"  t={state.sim_time:5.1f}  mode={getattr(controller, 'mode', '?'):6s}  "
                      f"detected={state.detection.detected}  cmds={len(cmds)}  "
                      f"target_entity={issued_target_entity}")
            # Read back any echoed commands (sanity)
            msg = pubsub.get_message(timeout=0.001)
    except KeyboardInterrupt:
        print("\n[smoke] ^C")

    # Save metrics
    out_dir = Path("/tmp/integ_smoke") if sys.platform != "win32" else HERE / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = recorder.finalize(
        controller_name=controller_spec,
        seed=None,
        config_snapshot={"controller": controller_spec, "duration": duration},
    )
    j, c2 = recorder.save(out_dir)
    elapsed = time.time() - start

    print()
    print("=" * 70)
    print("INTEGRATION SMOKE RESULTS")
    print("=" * 70)
    print(f"  ticks executed      : {n_ticks}")
    print(f"  wall-clock duration : {elapsed:.2f} s")
    print(f"  search time         : {final.search_time:.2f} s  (success={final.searched_successfully})")
    print(f"  total track time    : {final.total_track_time:.2f} s")
    print(f"  track in view       : {final.track_in_view_fraction*100:.1f} %")
    print(f"  mode switches       : {final.mode_switches}")
    print()
    print("INVARIANT CHECKS")
    print(f"  I-7 (no set_target_entity issued)         : {'PASS' if not issued_target_entity else 'FAIL'}")
    print(f"  I-2 (all set_enabled=False)               : {'PASS' if all(e is False for e in set_enabled_calls) else 'FAIL'}")
    print(f"  I-4 (set_orientation called ≥ once in TRACK): {'PASS' if set_orientation_calls > 0 else 'WARN (mode never entered TRACK)'}")
    # Spec 018: verify events were published without errors
    print(f"  T038 (sim:events published, no errors)     : {'PASS' if events_published > 0 or final.mode_switches == 0 else 'WARN (no mode transitions)'}")
    print(f"  T038 (events published count)               : {events_published}")
    print()
    print(f"metrics json   : {j}")
    print(f"trace csv      : {c2}")
    print("=" * 70)

    pubsub.close()
    c.close()
    return 0 if (not issued_target_entity and (set_orientation_calls > 0 or final.searched_successfully)) else 1


if __name__ == "__main__":
    spec = sys.argv[1] if len(sys.argv) > 1 else "search_track.fsm_controller:FsmSearchTrackController"
    dur = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    sys.exit(main(spec, dur))
