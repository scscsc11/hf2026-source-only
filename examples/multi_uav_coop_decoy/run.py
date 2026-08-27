"""Run the 017 multi-UAV cooperative search-track-decoy example.

Usage:
    python -m examples.multi_uav_coop_decoy.run                  # sim already running
    python -m examples.multi_uav_coop_decoy.run --start-sim       # also spawn opensim-sim
    python -m examples.multi_uav_coop_decoy.run --duration 120    # 120 sim-seconds
    python -m examples.multi_uav_coop_decoy.run --dry-run         # no Redis

Requires:
    - Redis on 127.0.0.1:6379
    - opensim-sim running with examples/multi_uav_coop_decoy/config/scenario.json
      (or use --start-sim to spawn it)

Scenario (spec FR-020): 3 UAVs + 3 real targets + 15 decoys, dispersed.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE
# 协同阈值 K(=赛题二评分规则,与 competition.sdk.scenarios.coop_decoy.DEFAULT_K 对齐)。
# 摧毁一个真目标需 ≥COOP_K 架 UAV 同时有效盯防满 20s。锁定不可由命令行覆盖。
COOP_K = 2
from examples._common.argparser import bootstrap_paths  # noqa: E402
REPO_ROOT = bootstrap_paths(EXAMPLE_DIR)

from search_track.coop_controller import CoopController  # noqa: E402
from search_track.comm_adapter import CommCommand  # noqa: E402
from search_track.config_reuse import load_algorithm_config  # type: ignore  # noqa: E402
from search_track.multi_client import MultiSimClient  # noqa: E402
from search_track.multi_state import EntityState, MultiSimState  # noqa: E402

from examples._common.scenario_targets import (  # noqa: E402
    inject_target_trajectories, load_target_trajectories,
)
from examples._common.sim_runner import start_sim, stop_sim  # noqa: E402
from examples._common.metrics_summary import (  # noqa: E402
    print_completion_banner, write_json, write_summary_json,
)
from examples._common.coop_eval import (  # noqa: E402
    CoopTrackingEvaluator, profile_multi_uav_coop_decoy,
)
from examples._common.uav_target_map import (  # noqa: E402
    UavDetection, resolve_uav_to_target,
)
from examples._common.score_publisher import ScorePublisher  # noqa: E402


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="017 multi-UAV cooperative example runner")
    p.add_argument("--config", type=str, default=str(EXAMPLE_DIR / "config" / "algorithm.yaml"))
    p.add_argument("--scenario", type=str, default=str(EXAMPLE_DIR / "config" / "scenario.json"))
    p.add_argument("--duration", type=float, default=120.0, help="sim seconds (default 120)")
    p.add_argument("--output", type=str, default=str(EXAMPLE_DIR / "output"))
    p.add_argument("--start-sim", action="store_true", help="spawn opensim-sim as subprocess")
    p.add_argument("--sim-binary", type=str, default=os.environ.get(
        "OPENSIM_SIM_BIN", str(REPO_ROOT / "build" / (
            "opensim-sim.exe" if sys.platform == "win32" else "opensim-sim"))))
    p.add_argument("--dry-run", action="store_true", help="don't publish to Redis")
    p.add_argument("--redis-host", type=str, default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--quiet", action="store_true")
    return p


def _build_controllers(uav_uids: list[str], cfg, first: MultiSimState,
                       log) -> dict[str, CoopController]:
    """One CoopController per UAV, each configured + reset + sector-assigned.

    Each UAV is given a stable fleet index (its position in the sorted uid
    list) so the sector-divided search fans the fleet out across the area
    instead of stacking everyone on one spiral. If the algorithm config
    leaves the sector centre null, we fill it from the first frame's UAV
    centroid so all three UAVs orbit the same centre.
    """
    controllers: dict[str, CoopController] = {}
    # Resolve sector centre: explicit config wins, else UAV centroid.
    center_lat = cfg.get("sector_center_latitude", None) if hasattr(cfg, "get") else None
    center_lon = cfg.get("sector_center_longitude", None) if hasattr(cfg, "get") else None
    if not center_lat or not center_lon:
        uav_pos = [e.uav.position for _, e in first.entities.items()
                   if e.kind == "uav" and e.uav is not None]
        if uav_pos:
            center_lat = sum(p.latitude for p in uav_pos) / len(uav_pos)
            center_lon = sum(p.longitude for p in uav_pos) / len(uav_pos)
        else:
            center_lat, center_lon = 27.0, 125.0
        log(f"[run] sector center auto-filled from UAV centroid: "
            f"({center_lat:.6f}, {center_lon:.6f})")
    n = len(uav_uids)
    for idx, uid in enumerate(uav_uids):
        c = CoopController(my_uid=uid)
        if hasattr(c, "configure"):
            c.configure(cfg)
        c.set_fleet_index(idx, n)
        c.set_sector_center(float(center_lat), float(center_lon))
        c.reset()
        controllers[uid] = c
    return controllers


def _load_target_trajectories(scenario_path: str) -> dict[str, dict]:
    """Back-compat alias for :func:`examples._common.scenario_targets.load_target_trajectories`.

    Kept so ``tests/test_run_inject.py`` (which calls
    ``runmod._load_target_trajectories``) keeps working without touching
    the tests.
    """
    return load_target_trajectories(scenario_path)


def _inject_target_trajectories(client, state: MultiSimState,
                                trajectories: dict[str, dict],
                                *, dry_run: bool, log) -> int:
    """Activate each declared target trajectory via set_speed + set_trajectory.

    Thin shim over :func:`examples._common.scenario_targets.inject_target_trajectories`;
    kept here to preserve the multi-specific publish path
    (``client.publish_dict(cmd)``) and the ``entities`` membership check.
    """
    def is_known_uid(uid: str) -> bool:
        return uid in state.entities

    def publish_fn(cmd: dict) -> None:
        client.publish_dict(cmd)

    return inject_target_trajectories(
        state, trajectories,
        dry_run=dry_run, log=log,
        is_known_uid=is_known_uid,
        publish_fn=publish_fn if not dry_run else None,
    )


def _summarise(state: MultiSimState) -> dict:
    """Compute per-tick summary stats for logging + final metrics (FR-025)."""
    uavs = [e for e in state.entities.values() if e.kind == "uav"]
    real_targets = [e for e in state.entities.values() if e.kind == "ground_vehicle"]
    decoys = [e for e in state.entities.values() if e.kind == "decoy_vehicle"]
    tracking_uavs = [e for e in uavs if e.detection and e.detection.detected]
    misid_uavs = [e for e in uavs if e.detection and e.detection.detected
                  and e.detection.misid_flag]
    comm_sent = sum(e.comm.stats.sent for e in uavs if e.comm)
    comm_delivered = sum(e.comm.stats.delivered for e in uavs if e.comm)
    return {
        "n_uavs": len(uavs),
        "n_real_targets": len(real_targets),
        "n_decoys": len(decoys),
        "n_tracking": len(tracking_uavs),
        "n_misid_tracking": len(misid_uavs),
        "comm_sent": comm_sent,
        "comm_delivered": comm_delivered,
    }


def _multi_to_eval_inputs(state: MultiSimState):
    """Project a MultiSimState into the evaluator's standardised inputs.

    Returns ``(uavs, true_targets, decoys)``:
      uavs         : list[UavDetection] (one per UAV entity)
      true_targets : {uid: (lat, lon)} real ground vehicles
      decoys       : {uid: (lat, lon)} decoy vehicles
    The cooperative scenario has no threats, so destroyed is always False.
    """
    uavs: list[UavDetection] = []
    true_targets: dict[str, tuple[float, float]] = {}
    decoys: dict[str, tuple[float, float]] = {}
    for uid, e in state.entities.items():
        det = e.detection
        if e.kind == "uav":
            pos = det.target_position if det else None
            uavs.append(UavDetection(
                uid=uid,
                detected=bool(det.detected) if det else False,
                target_lat=pos.latitude if pos else None,
                target_lon=pos.longitude if pos else None,
                target_type=det.target_type if det else "",
                misid_flag=bool(det.misid_flag) if det else False,
                destroyed=False,
                confidence=float(det.confidence) if det else 0.0,
            ))
        elif e.kind == "ground_vehicle" and e.vehicle_truth is not None:
            tp = e.vehicle_truth.position
            true_targets[uid] = (tp.latitude, tp.longitude)
        elif e.kind == "decoy_vehicle" and e.vehicle_truth is not None:
            dp = e.vehicle_truth.position
            decoys[uid] = (dp.latitude, dp.longitude)
    return uavs, true_targets, decoys


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    log = (lambda *a, **kw: None) if args.quiet else print

    # Load algorithm config (reuse 016's loader).
    cfg = load_algorithm_config(args.config)
    log(f"[run] algorithm config: {args.config}")

    # Optionally start opensim-sim.
    sim_proc = None
    if args.start_sim:
        sim_proc = start_sim(args.sim_binary, args.scenario, log=log)
        if sim_proc is None:
            return 2

    try:
        client = MultiSimClient(host=args.redis_host, port=args.redis_port)
        if not args.dry_run:
            client.connect()
            try:
                first = client.wait_first_state(timeout=120.0)
            except TimeoutError as e:
                log(f"[run] ERROR: {e}")
                return 3
        else:
            # dry-run: synthesize a minimal multi-entity first state.
            from search_track.multi_state import parse_multi_sim_state
            first = parse_multi_sim_state(_dry_run_seed_state())
            log("[run] dry-run: using synthetic multi-entity state")

        log(f"[run] first state @ sim_time={first.sim_time:.3f}, "
            f"entities={len(first.entities)}")

        # Discover UAVs from the first state frame.
        uav_uids = sorted(uid for uid, e in first.entities.items()
                          if e.kind == "uav")
        if not uav_uids:
            log("[run] ERROR: no UAVs in scenario state")
            return 4
        log(f"[run] UAVs discovered: {uav_uids}")

        controllers = _build_controllers(uav_uids, cfg, first, log)

        # Spec 025: cooperative continuous-tracking evaluator.
        true_target_uids = {uid for uid, e in first.entities.items()
                            if e.kind == "ground_vehicle"}
        evaluator = CoopTrackingEvaluator(
            profile_multi_uav_coop_decoy(duration_s=args.duration, K=COOP_K),
            true_target_uids)
        score_timeline: list[dict] = []
        # Spec 025 (sim:score): per-tick live-score publisher for the front-end.
        score_pub = ScorePublisher(
            host=args.redis_host, port=args.redis_port,
            connect=not args.dry_run,
        )
        log(f"[run] evaluation: K={COOP_K}, "
            f"targets={len(true_target_uids)}, dwell=20s, grace=2s")

        # Activate declared target trajectories (Feature 007 auto-static:
        # targets only move after a set_trajectory command flips
        # is_navigating). Without this the ground targets sit still and
        # the search/track loop never exercises moving targets.
        trajectories = load_target_trajectories(args.scenario)
        n_activated = _inject_target_trajectories(
            client, first, trajectories, dry_run=args.dry_run, log=log)
        log(f"[run] activated trajectories for {n_activated}/{len(trajectories)} "
            f"declared targets")

        rate = float(cfg.get("control_rate_hz", 10)) if hasattr(cfg, "get") else 10.0
        period = 1.0 / rate
        start_wall = time.time()
        sim_t0 = first.sim_time
        target_sim_end = sim_t0 + args.duration

        log(f"[run] control loop @ {rate} Hz for {args.duration:.1f} sim-seconds")

        last_print = -1.0
        last_state = first
        tick_count = 0
        # Aggregated metrics (FR-025).
        agg = {
            "true_targets_found_max": 0,
            "tracking_ticks_total": 0,
            "misid_track_ticks_total": 0,
            "comm_sent_total": 0,
            "comm_delivered_total": 0,
        }
        # Spec 018: track per-UAV mode transitions for event publishing.
        prev_modes: dict[str, str] = {uid: "SEARCH" for uid in uav_uids}
        discovered_uids: set[str] = set()
        try:
            while True:
                if not args.dry_run:
                    state = client.poll_latest(timeout=0.01)
                    if state is None:
                        state = last_state
                else:
                    state = last_state
                if args.duration > 0 and state.sim_time >= target_sim_end:
                    break
                if state.status == "ended":
                    log("[run] simulator reported status=ended; exiting")
                    break

                # Each UAV controller decides independently.
                all_cmds: list[dict] = []
                tracking_this_tick = 0
                for uid, ctrl in controllers.items():
                    cmds = ctrl.decide(state, period)
                    all_cmds.extend(cmds)
                    if ctrl.mode == "TRACK":
                        tracking_this_tick += 1
                        agg["tracking_ticks_total"] += 1
                    agg["misid_track_ticks_total"] += ctrl.misid_track_ticks

                if not args.dry_run:
                    for cmd in all_cmds:
                        client.publish_dict(cmd)

                # Spec 018: publish events for per-UAV mode transitions.
                if not args.dry_run:
                    for uid, ctrl in controllers.items():
                        cur_mode = ctrl.mode
                        prev_mode = prev_modes.get(uid, "SEARCH")
                        if cur_mode != prev_mode:
                            if cur_mode == "TRACK" and prev_mode == "SEARCH":
                                client.publish_event(
                                    event_type="state.enter_track",
                                    entity_uid=uid,
                                    sim_time=state.sim_time,
                                    payload={"target_type": "real", "confidence": 0.0},
                                )
                            elif cur_mode == "SEARCH" and prev_mode == "TRACK":
                                client.publish_event(
                                    event_type="state.exit_track",
                                    entity_uid=uid,
                                    sim_time=state.sim_time,
                                    payload={"reason": "target_lost"},
                                )
                            prev_modes[uid] = cur_mode

                # Aggregate metrics from state.
                s = _summarise(state)
                agg["true_targets_found_max"] = max(
                    agg["true_targets_found_max"], s["n_tracking"])
                agg["comm_sent_total"] = s["comm_sent"]
                agg["comm_delivered_total"] = s["comm_delivered"]

                # Spec 025: observe cooperative tracking + live score.
                _uavs, _tts, _dcs = _multi_to_eval_inputs(state)
                _umap = resolve_uav_to_target(_uavs, _tts, _dcs)
                evaluator.observe(state.sim_time, _umap, set())
                _snap = evaluator.score({
                    "comm_sent": s["comm_sent"],
                    "comm_delivered": s["comm_delivered"],
                    "sim_t0": sim_t0,
                })
                score_timeline.append({
                    "sim_time": state.sim_time,
                    "total_score": _snap["total_score"],
                    "completion_rate": _snap["completion_rate"],
                })
                # Spec 025 (sim:score): publish live score for the front-end.
                score_pub.publish(
                    _snap, sim_time=state.sim_time, tick=evaluator.tick_count,
                )

                if int(state.sim_time) > last_print:
                    last_print = int(state.sim_time)
                    log(
                        f"  t={state.sim_time:6.1f}  tracking={tracking_this_tick}/"
                        f"{len(uav_uids)}  misid_track={s['n_misid_tracking']}  "
                        f"comm_sent={s['comm_sent']}  comm_delivered={s['comm_delivered']}"
                    )

                last_state = state
                tick_count += 1
                if not args.dry_run:
                    elapsed = time.time() - start_wall
                    target_wall = (state.sim_time - sim_t0) / rate
                    sleep_for = target_wall - elapsed
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                else:
                    # dry-run: advance sim_time so the loop terminates at
                    # target_sim_end. We mutate the frozen dataclass via
                    # replace to avoid touching the real-state path.
                    from dataclasses import replace as _replace
                    last_state = _replace(last_state,
                                          sim_time=last_state.sim_time + period)
                    time.sleep(0.001)  # yield, but don't pace wall-clock
        except KeyboardInterrupt:
            log("\n[run] ^C; finalizing")

        # Finalize + save metrics (FR-025).
        sim_dur = last_state.sim_time - sim_t0
        wall_dur = time.time() - start_wall
        summary = {
            "controller": "search_track.coop_controller:CoopController",
            "uav_count": len(uav_uids),
            "true_targets_found_max": agg["true_targets_found_max"],
            "true_targets_total": sum(1 for e in last_state.entities.values()
                                      if e.kind == "ground_vehicle"),
            "decoys_total": sum(1 for e in last_state.entities.values()
                                if e.kind == "decoy_vehicle"),
            "tracking_ticks_total": agg["tracking_ticks_total"],
            "misid_track_ticks_total": controllers and sum(
                c.misid_track_ticks for c in controllers.values()),
            "comm_sent_total": agg["comm_sent_total"],
            "comm_delivered_total": agg["comm_delivered_total"],
            "sim_duration_s": sim_dur,
            "wall_duration_s": wall_dur,
            "tick_count": tick_count,
        }
        # Spec 025: final evaluation snapshot + per-tick score timeline.
        evaluation = evaluator.score({
            "comm_sent": summary["comm_sent_total"],
            "comm_delivered": summary["comm_delivered_total"],
            "sim_t0": sim_t0,
        })
        evaluation["score_timeline"] = score_timeline
        summary["evaluation"] = evaluation
        eval_path = write_json(
            evaluation, args.output,
            f"run_{int(time.time())}.evaluation.json")
        # Spec 025 (sim:score): final frame so the front-end shows the
        # definitive pass/fail verdict after the loop exits.
        score_pub.publish_final(
            evaluation, sim_time=sim_dur, tick=evaluator.tick_count,
            evaluation_path=eval_path,
        )
        j_path = write_summary_json(summary, args.output, log=log)
        print_completion_banner(
            "COOP SCENARIO COMPLETE",
            [
                f"  sim duration       : {sim_dur:.1f} s",
                f"  wall duration      : {wall_dur:.1f} s",
                f"  UAVs               : {len(uav_uids)}",
                f"  true targets found : {summary['true_targets_found_max']}/"
                f"{summary['true_targets_total']}",
                f"  tracking ticks     : {summary['tracking_ticks_total']}",
                f"  misid track ticks  : {summary['misid_track_ticks_total']}",
                f"  comm sent/delivered: {summary['comm_sent_total']}/"
                f"{summary['comm_delivered_total']}",
                f"  --- EVALUATION (Spec 025) ---",
                f"  total score        : {evaluation['total_score']:.1f} / 100  "
                f"(passed={evaluation['passed']})",
                f"  K/dwell/grace      : {evaluation['K']} / "
                f"{evaluation['dwell_target_s']}s / {evaluation['grace_s']}s",
                f"  completed          : {evaluation['n_completed']}/"
                f"{evaluation['n_targets']}  (rate={evaluation['completion_rate']:.2f})",
                f"  misid rate         : {evaluation['misid_rate']:.2f}",
                f"  evaluation json    : {eval_path}",
                f"  metrics json       : {j_path}",
            ],
            log=log,
        )

    finally:
        stop_sim(sim_proc)
        score_pub.close()
    return 0


def _dry_run_seed_state() -> dict:
    """Minimal synthetic multi-entity state for --dry-run mode."""
    def uav(uid, name, lat, lon):
        return {"type": "fixed_wing_uav", "name": name,
                "platform": {"position": {"latitude": lat, "longitude": lon, "altitude": 300.0},
                             "attitude": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}},
                "velocity": 20.0, "heading": 0.0,
                "gimbal_tracking": {"pan_angle": 0.0, "tilt_angle": -45.0,
                                     "track_enabled": True, "fov_deg": 30.0,
                                     "detection": {"detected": False, "confidence": 0.0}},
                "comm": {"enabled": True, "range_m": 1000.0, "max_bytes": 50,
                         "max_rate_hz": 4.0, "inbox": [],
                         "stats": {"sent": 0, "delivered": 0, "received": 0,
                                   "rejected_bytes": 0, "rejected_rate": 0,
                                   "rejected_range": 0, "rejected_jam": 0}}}
    def veh(uid, name, lat, lon, kind):
        return {"type": kind, "name": name,
                "platform": {"position": {"latitude": lat, "longitude": lon, "altitude": 0.0}},
                "speed": 8.0, "heading": 0.0}
    s: dict = {"timestamp": 0.0, "sim_time": 0.0, "status": "running"}
    s["20001"] = uav("20001", "uav_alpha", 27.000, 124.995)
    s["20002"] = uav("20002", "uav_bravo", 27.000, 125.005)
    s["20003"] = uav("20003", "uav_charlie", 26.995, 125.000)
    s["10001"] = veh("10001", "target_1", 27.005, 124.998, "ground_vehicle")
    s["10002"] = veh("10002", "target_2", 26.998, 125.010, "ground_vehicle")
    s["10003"] = veh("10003", "target_3", 27.002, 124.990, "ground_vehicle")
    for i in range(1, 16):
        s[f"300{i:02d}"] = veh(f"300{i:02d}", f"decoy_{i:02d}",
                               27.0 + (i % 5) * 0.001,
                               125.0 + (i % 7) * 0.001, "decoy_vehicle")
    return s


if __name__ == "__main__":
    sys.exit(main())
