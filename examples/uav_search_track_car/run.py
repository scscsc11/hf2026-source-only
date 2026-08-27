"""Run the UAV search-track-car example.

Usage:
    python -m examples.uav_search_track_car.run                  # require sim already running
    python -m examples.uav_search_track_car.run --start-sim       # also start opensim-sim
    python -m examples.uav_search_track_car.run --duration 60    # run 60 sim-seconds
    python -m examples.uav_search_track_car.run --controller pkg.mod:Cls
    python -m examples.uav_search_track_car.run --dry-run        # don't publish

Requires:
    - Redis on 127.0.0.1:6379
    - opensim-sim running with examples/uav_search_track_car/config/scenario.json
      (or use --start-sim to spawn it)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

# allow running as `python -m examples.uav_search_track_car.run` from repo root,
# AND as `python run.py` from within the example directory.
HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE
from examples._common.argparser import bootstrap_paths  # noqa: E402
REPO_ROOT = bootstrap_paths(EXAMPLE_DIR)  # NOTE: was HERE.parents[2] — pointed
#                                          above the repo root; now derived
#                                          consistently from the package layout.

from search_track.client import SimClient  # noqa: E402
from search_track.commands import ControlCommand  # noqa: E402
from search_track.config import AlgorithmConfig, from_yaml  # noqa: E402
from search_track.controller import load_controller  # noqa: E402
from search_track.metrics import MetricsRecorder  # noqa: E402

from examples._common.sim_runner import start_sim, stop_sim  # noqa: E402
from examples._common.coop_eval import (  # noqa: E402
    CoopTrackingEvaluator, profile_uav_search_track_car,
)
from examples._common.uav_target_map import (  # noqa: E402
    UavDetection, resolve_uav_to_target,
)
from examples._common.metrics_summary import write_json  # noqa: E402
from examples._common.score_publisher import ScorePublisher  # noqa: E402


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="UAV search-track example runner")
    p.add_argument("--config", type=str, default=str(EXAMPLE_DIR / "config" / "algorithm.yaml"))
    p.add_argument("--scenario", type=str, default=str(EXAMPLE_DIR / "config" / "scenario.json"))
    p.add_argument("--duration", type=float, default=60.0, help="sim seconds (default 60)")
    p.add_argument("--controller", type=str, default=None, help="override controller spec")
    p.add_argument("--output", type=str, default=str(EXAMPLE_DIR / "output"))
    p.add_argument("--start-sim", action="store_true", help="spawn opensim-sim as subprocess")
    p.add_argument("--sim-binary", type=str, default=os.environ.get(
        "OPENSIM_SIM_BIN", str(REPO_ROOT / "build" / (
            "opensim-sim.exe" if sys.platform == "win32" else "opensim-sim"))))
    p.add_argument("--dry-run", action="store_true", help="don't publish to Redis")
    p.add_argument("--redis-host", type=str, default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--batch", type=int, default=0, help="run N times and aggregate")
    p.add_argument("--seeds", type=str, default=None,
                   help="seed range, e.g. '0..29'; default uses 0..N-1")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    log = (lambda *a, **kw: None) if args.quiet else print

    # Batch mode: orchestrate N runs and summarize
    if args.batch and args.batch > 1:
        from search_track.batch import BatchRunner
        seed_base = 0
        seed_count = args.batch
        if args.seeds:
            try:
                a, b = args.seeds.split("..")
                seed_base = int(a)
                seed_count = int(b) - int(a) + 1
            except Exception:
                log(f"[run] invalid --seeds {args.seeds!r}; using 0..{args.batch - 1}")
                seed_base, seed_count = 0, args.batch
        log(f"[run] BATCH mode: {seed_count} runs, seeds {seed_base}..{seed_base + seed_count - 1}")
        runner = BatchRunner(
            output_dir=args.output,
            sim_binary=args.sim_binary,
            scenario_path=args.scenario,
            duration=args.duration,
        )
        results = runner.run(
            n=seed_count, seed_base=seed_base,
            controller_name=cfg.controller if False else None,  # populated below
            config_snapshot={"controller": None},
        )
        log(f"[run] batch complete: {len(results)} runs, summary at {args.output}/summary.json")
        return 0

    # 1) Load algorithm config
    cfg = from_yaml(args.config)
    if args.controller:
        cfg.controller = args.controller
    log(f"[run] algorithm config: {args.config}")
    log(f"[run] controller: {cfg.controller}")

    # 2) Optionally start opensim-sim
    sim_proc = None
    if args.start_sim:
        sim_proc = start_sim(args.sim_binary, args.scenario, log=log)
        if sim_proc is None:
            return 2

    # 3) Connect
    try:
        client = SimClient(host=args.redis_host, port=args.redis_port)
        if not args.dry_run:
            client.connect()
            try:
                first = client.wait_first_state(timeout=120.0)
            except TimeoutError as e:
                log(f"[run] ERROR: {e}")
                return 3
            log(f"[run] first state @ sim_time={first.sim_time:.3f}")
            # Start the target vehicle's trajectory so it maneuvers during
            # the test.  Without this the target sits stationary at its
            # initial position and the algorithm has nothing to do.
            route = {
                "speed": 8.0,
                "waypoints": [
                    {"lat": 27.002, "lon": 125.007, "alt": 0},
                    {"lat": 26.999, "lon": 125.010, "alt": 0},
                    {"lat": 27.001, "lon": 125.015, "alt": 0},
                ],
            }
            client.publish_dict({"target": "target", "cmd": "set_trajectory", "params": route})
            log(f"[run] target route: {len(route['waypoints'])} waypoints @ {route['speed']} m/s")
        else:
            # dry-run: synthesize a fake first state to anchor the loop
            from search_track.state import (
                Attitude, Detection, GeoPosition, GimbalState, SimState,
                TargetState, UavState,
            )
            first = SimState(
                sim_time=0.0, timestamp=0.0, status="running",
                uav=UavState(GeoPosition(27.0, 125.0, 300.0), Attitude(0, 0, 0), 20.0, 0.0),
                gimbal=GimbalState(0.0, -30.0, False, 60.0),
                detection=Detection(False, 0.0, None, None),
                target_truth=TargetState(GeoPosition(27.002, 125.002, 0.0), 8.0, 0.0),
            )
            log("[run] dry-run: skipping Redis connect, using synthetic state")

        # 4) Construct controller and configure
        controller = load_controller(cfg.controller)
        if hasattr(controller, "configure"):
            controller.configure(cfg)
        controller.reset()

        # 5) Run loop
        recorder = MetricsRecorder()
        # Spec 025: cooperative continuous-tracking evaluator (K=1, single UAV).
        TARGET_UID = "target"
        evaluator = CoopTrackingEvaluator(
            profile_uav_search_track_car(duration_s=args.duration), {TARGET_UID})
        score_timeline: list[dict] = []
        # Spec 025 (sim:score): per-tick live-score publisher for the front-end.
        score_pub = ScorePublisher(
            host=args.redis_host, port=args.redis_port,
            connect=not args.dry_run,
        )
        rate = float(cfg.get("control_rate_hz", 10))
        period = 1.0 / rate
        start_wall = time.time()
        sim_t0 = first.sim_time
        # duration is relative: count from first state's sim_time
        target_sim_end = sim_t0 + args.duration

        log(f"[run] control loop @ {rate} Hz for {args.duration:.1f} sim-seconds")
        log(f"[run] sim_t0={sim_t0:.1f} target_end={target_sim_end:.1f}")
        log(f"[run] mode=SEARCH (initial)")

        last_print = -1.0
        last_state = first
        # Spec 018: track mode changes and target discovery for event publishing.
        prev_mode: str | None = getattr(controller, "mode", "SEARCH")
        discovered_uids: set[str] = set()  # dedup: entity_uid seen as discovered in current TRACK window
        try:
            while True:
                if not args.dry_run:
                    state = client.poll_latest(timeout=0.01)
                    if state is None:
                        state = last_state
                else:
                    # in dry-run we just hold the synthetic state
                    state = last_state
                if args.duration > 0 and state.sim_time >= target_sim_end:
                    break
                if state.status == "ended":
                    log("[run] simulator reported status=ended; exiting")
                    break

                # Use stripped state for the controller (FR-007 enforcement)
                safe_state = state.without_truth()
                cmds = controller.decide(safe_state, period)

                if not args.dry_run:
                    for cmd in cmds:
                        client.publish(cmd)

                # Spec 018: detect mode transitions and publish events.
                current_mode = getattr(controller, "mode", "?")
                if current_mode != prev_mode:
                    if current_mode == "TRACK" and prev_mode == "SEARCH":
                        payload: dict = {}
                        if state.detection.target_position:
                            tp = state.detection.target_position
                            payload["target_position"] = {"latitude": tp.latitude, "longitude": tp.longitude, "altitude": tp.altitude}
                        payload["target_type"] = "real"
                        payload["confidence"] = state.detection.confidence
                        client.publish_event(
                            event_type="state.enter_track",
                            entity_uid=client.uav_id,
                            sim_time=state.sim_time,
                            payload=payload,
                        )
                        # Reset discovered set for new TRACK window
                        discovered_uids.clear()
                    elif current_mode == "SEARCH" and prev_mode == "TRACK":
                        client.publish_event(
                            event_type="state.exit_track",
                            entity_uid=client.uav_id,
                            sim_time=state.sim_time,
                            payload={"reason": "target_lost"},
                        )
                        discovered_uids.clear()
                    prev_mode = current_mode

                # Spec 018: publish target.discovered on first detection in TRACK window.
                if current_mode == "TRACK" and state.detection.detected:
                    det_uid = f"target-{client.uav_id}"
                    if det_uid not in discovered_uids:
                        discovered_uids.add(det_uid)
                        payload = {}
                        if state.detection.target_position:
                            tp = state.detection.target_position
                            payload["target_position"] = {"latitude": tp.latitude, "longitude": tp.longitude, "altitude": tp.altitude}
                        payload["target_type"] = "real"
                        payload["confidence"] = state.detection.confidence
                        if state.detection.azimuth_error_deg is not None:
                            payload["azimuth_error"] = state.detection.azimuth_error_deg
                        client.publish_event(
                            event_type="target.discovered",
                            entity_uid=client.uav_id,
                            sim_time=state.sim_time,
                            payload=payload,
                        )

                # record metrics — use full state for ground truth
                tgt_lat = tgt_lon = dist = None
                if state.target_truth is not None:
                    tgt_lat = state.target_truth.position.latitude
                    tgt_lon = state.target_truth.position.longitude
                    from search_track.geometry import haversine_m
                    dist = haversine_m(
                        state.uav.position.latitude, state.uav.position.longitude,
                        tgt_lat, tgt_lon,
                    )
                mode = getattr(controller, "mode", "?")
                recorder.record_tick(
                    sim_time=state.sim_time,
                    mode=mode,
                    detected=state.detection.detected,
                    uav_lat=state.uav.position.latitude,
                    uav_lon=state.uav.position.longitude,
                    uav_alt=state.uav.position.altitude,
                    target_lat=tgt_lat,
                    target_lon=tgt_lon,
                    distance_m=dist,
                )

                # Spec 025: observe cooperative tracking + live score snapshot.
                det_pos = state.detection.target_position
                uav_det = UavDetection(
                    uid=client.uav_id,
                    detected=state.detection.detected,
                    target_lat=det_pos.latitude if det_pos else None,
                    target_lon=det_pos.longitude if det_pos else None,
                    target_type="ground_vehicle",  # single-UAV scenario: no decoys
                    misid_flag=False,
                    destroyed=False,
                    confidence=state.detection.confidence,
                )
                true_targets = (
                    {TARGET_UID: (tgt_lat, tgt_lon)}
                    if tgt_lat is not None and tgt_lon is not None else {})
                uav_map = resolve_uav_to_target([uav_det], true_targets, {})
                evaluator.observe(state.sim_time, uav_map, set())
                _m = recorder._metrics
                _ivf = (_m.track_in_view_time / _m.total_track_time
                        if _m.total_track_time > 0 else 0.0)
                snap = evaluator.score({
                    "search_time": _m.search_time,
                    "track_in_view_fraction": _ivf,
                    "sim_t0": sim_t0,
                })
                score_timeline.append({
                    "sim_time": state.sim_time,
                    "total_score": snap["total_score"],
                    "completion_rate": snap["completion_rate"],
                })
                # Spec 025 (sim:score): publish live score for the front-end.
                score_pub.publish(
                    snap, sim_time=state.sim_time, tick=evaluator.tick_count,
                )

                # log every sim-second
                if int(state.sim_time) > last_print:
                    last_print = int(state.sim_time)
                    log(
                        f"  t={state.sim_time:6.1f}  mode={mode:6s}  "
                        f"uav=({state.uav.position.latitude:.5f}, {state.uav.position.longitude:.5f}, {state.uav.position.altitude:.0f}m)  "
                        f"detected={state.detection.detected}"
                    )

                last_state = state
                # wall-clock pacing
                if not args.dry_run:
                    elapsed = time.time() - start_wall
                    target_wall = (state.sim_time - sim_t0) / rate
                    sleep_for = target_wall - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                else:
                    time.sleep(period)
                    # advance synthetic sim time
                    last_state = _advance_synthetic(last_state, period, detected=False)
                    if mode == "TRACK":
                        # turn on detection after a few ticks for dry-run
                        last_state = _advance_synthetic(last_state, period, detected=True)

        except KeyboardInterrupt:
            log("\n[run] ^C; finalizing")

        # 6) Finalize
        final = recorder.finalize(
            controller_name=cfg.controller,
            seed=cfg.seed,
            config_snapshot={
                "controller": cfg.controller,
                "search_radius": cfg.search_radius,
                "loiter_radius": cfg.loiter_radius,
            },
        )
        j, c = recorder.save(args.output)
        # Spec 025: final evaluation snapshot + per-tick score timeline.
        evaluation = evaluator.score({
            "search_time": final.search_time,
            "track_in_view_fraction": final.track_in_view_fraction,
            "sim_t0": sim_t0,
        })
        evaluation["score_timeline"] = score_timeline
        # total sim duration (relative to first state) for the final score frame
        sim_dur = last_state.sim_time - sim_t0
        eval_path = write_json(
            evaluation, args.output, f"{recorder.run_id}.evaluation.json")
        # Spec 025 (sim:score): final frame so the front-end shows the
        # definitive pass/fail verdict after the loop exits.
        score_pub.publish_final(
            evaluation, sim_time=sim_dur, tick=evaluator.tick_count,
            evaluation_path=eval_path,
        )
        log("")
        log("=" * 60)
        log("SCENARIO COMPLETE")
        log(f"  sim duration   : {final.sim_duration:.1f} s")
        log(f"  wall duration  : {final.wall_duration:.1f} s")
        log(f"  search time    : {final.search_time:.2f} s  (success={final.searched_successfully})")
        log(f"  track total    : {final.total_track_time:.2f} s")
        log(f"  track in view  : {final.track_in_view_fraction*100:.1f} %")
        log(f"  mode switches  : {final.mode_switches}")
        log(f"  metrics json   : {j}")
        log(f"  trace csv      : {c}")
        log("  --- EVALUATION (Spec 025) ---")
        log(f"  total score    : {evaluation['total_score']:.1f} / 100  "
            f"(passed={evaluation['passed']})")
        log(f"  K/dwell/grace  : {evaluation['K']} / "
            f"{evaluation['dwell_target_s']}s / {evaluation['grace_s']}s")
        log(f"  completed      : {evaluation['n_completed']}/"
            f"{evaluation['n_targets']}  (rate={evaluation['completion_rate']:.2f})")
        log(f"  evaluation json: {eval_path}")
        log("=" * 60)

    finally:
        stop_sim(sim_proc)
        score_pub.close()
    return 0


def _advance_synthetic(state, dt: float, *, detected: bool):
    from search_track.state import (
        Attitude, Detection, GeoPosition, GimbalState, SimState,
        TargetState, UavState,
    )
    new_pos = GeoPosition(
        state.uav.position.latitude + 1e-5,
        state.uav.position.longitude,
        state.uav.position.altitude,
    )
    tgt = state.target_truth
    new_det = Detection(detected=detected, confidence=0.9 if detected else 0.0,
                        target_position=(GeoPosition(tgt.position.latitude, tgt.position.longitude, 0.0) if detected and tgt else None),
                        azimuth_error_deg=0.5 if detected else None)
    return SimState(
        sim_time=state.sim_time + dt,
        timestamp=state.timestamp + dt,
        status=state.status,
        uav=UavState(new_pos, state.uav.attitude, state.uav.velocity, state.uav.heading),
        gimbal=state.gimbal,
        detection=new_det,
        target_truth=tgt,
    )


if __name__ == "__main__":
    sys.exit(main())
