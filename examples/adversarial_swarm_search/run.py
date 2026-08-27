"""Spec 019 — minimal kernel-integration runner.

The MVP validates the Spec 019 kernel wiring end-to-end:
  1. Spawn a UAV swarm (10) + targets/decoys + zones via opensim-sim.
  2. Subscribe to sim:state, parse the published `zones` bucket.
  3. Print a per-second summary: alive UAVs, jammed UAVs, zones active.

The full distributed algorithm (US3-US5) is NOT exercised here — that
lands in subsequent phases (per the user's MVP-first directive). This
runner is the integration test scaffold for Phase 4 (single-machine
arbitration verification) and the launch pad for Phase 5.

Usage:
    # Redis on default 127.0.0.1:6379, opensim-sim already running with
    # the example's scenario.json.
    py -m examples.adversarial_swarm_search.run --duration 60

    # Auto-spawn opensim-sim and tear it down on exit.
    py -m examples.adversarial_swarm_search.run --start-sim --duration 60

    # Dry-run against a synthetic state (no Redis, no sim).
    py -m examples.adversarial_swarm_search.run --dry-run
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE
from examples._common.argparser import bootstrap_paths  # noqa: E402
REPO_ROOT = bootstrap_paths(EXAMPLE_DIR)

from search_track.state import SwarmState, parse_swarm_state  # noqa: E402
from search_track.swarm_controller import SwarmController  # noqa: E402

from examples._common.scenario_targets import (  # noqa: E402
    inject_target_trajectories, load_target_trajectories,
)
from examples._common.redis_sub import connect_redis  # noqa: E402
from examples._common.sim_runner import start_sim, stop_sim  # noqa: E402
from examples._common.metrics_summary import (  # noqa: E402
    print_completion_banner, write_json, write_summary_json,
)
from examples._common.coop_eval import (  # noqa: E402
    CoopTrackingEvaluator, profile_adversarial_swarm_search,
)
from examples._common.uav_target_map import (  # noqa: E402
    UavDetection, resolve_uav_to_target,
)
from examples._common.score_publisher import ScorePublisher  # noqa: E402




def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Spec 019 swarm-search kernel-integration runner")
    p.add_argument("--scenario", type=str,
                   default=str(EXAMPLE_DIR / "config" / "scenario.json"))
    p.add_argument("--duration", type=float, default=60.0,
                   help="sim seconds (default 60)")
    p.add_argument("--output", type=str, default=str(EXAMPLE_DIR / "output"))
    p.add_argument("--start-sim", action="store_true",
                   help="spawn opensim-sim as a subprocess")
    p.add_argument("--sim-binary", type=str, default=os.environ.get(
        "OPENSIM_SIM_BIN", str(REPO_ROOT / "build" / (
            "opensim-sim.exe" if sys.platform == "win32" else "opensim-sim"))))
    p.add_argument("--dry-run", action="store_true",
                   help="don't connect to Redis; print synthetic state")
    p.add_argument("--redis-host", type=str, default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--redis-state-channel", type=str, default="sim:state")
    p.add_argument("--redis-command-channel", type=str, default="sim:commands")
    p.add_argument("--quiet", action="store_true")
    return p


def _inject_target_trajectories(redis_conn, state: SwarmState,
                                trajectories: dict,
                                *, dry_run: bool, log) -> int:
    """Activate each declared target trajectory via set_speed + set_trajectory.

    Thin shim over :func:`examples._common.scenario_targets.inject_target_trajectories`;
    kept here only to preserve the adversarial-specific publish path
    (``redis_conn.publish("sim:commands", ...)``) and the
    ``uavs | targets | decoys`` membership check.
    """
    def is_known_uid(uid: str) -> bool:
        return (uid in state.uavs
                or uid in state.targets
                or uid in state.decoys)

    def publish_fn(cmd: dict) -> None:
        redis_conn.publish("sim:commands", json.dumps(cmd))

    return inject_target_trajectories(
        state, trajectories,
        dry_run=dry_run, log=log,
        is_known_uid=is_known_uid,
        publish_fn=publish_fn if not dry_run else None,
    )


def _summarise(st: SwarmState) -> dict:
    return {
        "n_alive": st.n_alive,
        "n_destroyed": sum(1 for u in st.uavs.values() if u.destroyed),
        "n_jammed": sum(1 for u in st.uavs.values() if u.jammed),
        "n_tracking": sum(1 for u in st.uavs.values() if u.detected and not u.misid_flag),
        "n_misid": sum(1 for u in st.uavs.values() if u.detected and u.misid_flag),
        "n_zones_air_defense": sum(1 for z in st.zones if z.type == "air_defense"),
        "n_zones_static_jam": sum(1 for z in st.zones if z.type == "comm_jam_static"),
        "n_zones_random_jam": sum(1 for z in st.zones if z.type == "comm_jam_random"),
    }


def _swarm_to_eval_inputs(state: SwarmState):
    """Project a SwarmState into the evaluator's standardised inputs."""
    uavs = [UavDetection(
        uid=uid, detected=u.detected,
        target_lat=u.target_lat, target_lon=u.target_lon,
        target_type=u.target_type, misid_flag=u.misid_flag,
        destroyed=u.destroyed, confidence=u.confidence,
    ) for uid, u in state.uavs.items()]
    true_targets = {uid: (t.latitude, t.longitude)
                    for uid, t in state.targets.items()}
    decoys = {uid: (d.latitude, d.longitude) for uid, d in state.decoys.items()}
    return uavs, true_targets, decoys


def _dry_run_seed_state() -> dict:
    """A minimal state that exercises the kernel's zones bucket."""
    def uav(uid, name, lat, lon, alt=600.0):
        return {"type": "fixed_wing_uav", "name": name,
                "platform": {"position": {"latitude": lat, "longitude": lon, "altitude": alt},
                             "status": "active"},
                "comm": {"enabled": True, "range_m": 1000.0,
                         "stats": {"sent": 0, "delivered": 0}}}
    s: dict = {"sim_time": 0.0, "status": "running"}
    for i in range(1, 11):
        s[f"200{i:02d}"] = uav(f"200{i:02d}", f"uav_{i:02d}",
                               27.00 + 0.002 * i, 124.985 + 0.002 * i)
    s["zones"] = {
        "air_defense": [
            {"polygon": [[27.008, 124.990], [27.008, 125.000],
                         [27.018, 125.000], [27.018, 124.990]],
             "alt_min": 0.0, "alt_max": 2500.0}],
        "comm_jam_static": [
            {"polygon": [[27.020, 125.005], [27.020, 125.015],
                         [27.025, 125.015], [27.025, 125.005]],
             "alt_min": 0.0, "alt_max": 5000.0}],
        "comm_jam_random": [],
    }
    return s


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    log = (lambda *a, **kw: None) if args.quiet else print

    # Optionally start opensim-sim.
    sim_proc = None
    if args.start_sim:
        sim_proc = start_sim(
            args.sim_binary, args.scenario, log=log,
            stderr_file=str(Path(args.output) / "sim.stderr.log"))
        if sim_proc is None:
            return 2

    try:
        r = None  # Redis connection; None is safe when dry_run=True
        if not args.dry_run:
            log(f"[run] connecting to Redis {args.redis_host}:{args.redis_port}, "
                f"subscribing to {args.redis_state_channel}")
            r, pubsub = connect_redis(args.redis_host, args.redis_port,
                                      args.redis_state_channel)
        else:
            log("[run] dry-run: no Redis connection")

        # Per-US3 (T054): self-termination.  This runner represents ONE UAV
        # in the swarm; the kernel-side ThreatArbiter may declare "this"
        # UAV destroyed.  When `sim:state` reports our own uid status =
        # "destroyed", we MUST stop sending commands immediately.  We do
        # NOT broadcast any "destroyed" message — that's info-isolated.
        # The runner terminates the local process when the self-check
        # flips.
        self_uid = os.environ.get("OPENSIM_SELF_UID", "u001")

        # Wait for the first state frame.
        last_summary = None
        last_state: Optional[SwarmState] = None
        sim_t0 = 0.0
        wall_t0 = time.time()
        target_sim_end = args.duration
        last_print_sim_t = -1.0
        agg = {
            "n_destroyed_peak": 0,
            "n_jammed_peak": 0,
            "samples": 0,
            "avoidance_commands_total": 0,
        }

        # Build one SwarmController per UAV. Each controller drives its
        # UAV through sector-divided cooperative search + track + blind
        # avoidance of published air-defense zones (FR-016). Sector
        # centres are auto-filled from the first frame's UAV centroid so
        # the whole fleet fans out over the same area. Construction is
        # deferred to the first state frame (we need the live uid list).
        controllers: dict[str, SwarmController] = {}
        sector_center_lat: Optional[float] = None
        sector_center_lon: Optional[float] = None
        # Load target trajectories once (Feature 007 auto-static: ground
        # targets only move after a set_trajectory command flips
        # is_navigating). Decoys remain static (speed=0 in scenario).
        target_trajectories = load_target_trajectories(args.scenario)
        targets_activated = False
        try:
            from search_track.config import load_algorithm_config
            _cfg = load_algorithm_config(
                str(EXAMPLE_DIR / "config" / "algorithm.yaml"))
        except Exception as e:  # pragma: no cover
            log(f"[run] WARN: algorithm config load failed ({e}); "
                f"using defaults")
            _cfg = {}

        log(f"[run] control loop running for {args.duration:.1f} sim-seconds")

        while True:
            raw: Optional[dict] = None
            if not args.dry_run:
                msg = pubsub.get_message(timeout=0.1)
                if msg and msg.get("type") == "message":
                    try:
                        raw = json.loads(msg["data"])
                    except Exception as e:
                        log(f"[run] WARN: bad JSON: {e}")
            else:
                # dry-run: synthesise a single static state
                if last_state is None:
                    raw = _dry_run_seed_state()
                else:
                    time.sleep(0.05)
                    raw = None

            if raw is not None:
                state = parse_swarm_state(raw)
                last_state = state
                if sim_t0 == 0.0:
                    sim_t0 = state.sim_time
                    target_sim_end = sim_t0 + args.duration

                # Feature 007 auto-static: activate ground target trajectories
                # on the first state frame so targets begin moving immediately.
                if not targets_activated:
                    n_activated = _inject_target_trajectories(
                        r, state, target_trajectories,
                        dry_run=args.dry_run, log=log)
                    if n_activated > 0:
                        log(f"[run] activated {n_activated} ground target trajectories")
                    targets_activated = True

                # Per-US3 (T054): self-termination when this node is
                # declared destroyed by the kernel.  No broadcast of the
                # "destroyed" event — peer nodes discover the loss via
                # the heartbeat-timeout path (US3 / FR-017, SC-010).
                self_uav = state.uavs.get(self_uid)
                if self_uav is not None and self_uav.destroyed:
                    log(f"[run] self-termination: {self_uid} destroyed at "
                        f"t={state.sim_time:.1f}; stopping local control loop")
                    break

                s = _summarise(state)
                agg["samples"] += 1
                agg["n_destroyed_peak"] = max(agg["n_destroyed_peak"], s["n_destroyed"])
                agg["n_jammed_peak"] = max(agg["n_jammed_peak"], s["n_jammed"])
                last_summary = s

                # First-frame controller construction: now that we have the
                # live uid list + UAV centroid, instantiate one
                # SwarmController per UAV and assign each a stable sector
                # index (its position in the sorted uid list).
                if not controllers:
                    alive_uids = sorted(
                        u for u, v in state.uavs.items() if not v.destroyed)
                    # Sector centre: explicit config wins, else UAV centroid.
                    sector_center_lat = _cfg.get("sector_center_latitude") \
                        if hasattr(_cfg, "get") else None
                    sector_center_lon = _cfg.get("sector_center_longitude") \
                        if hasattr(_cfg, "get") else None
                    if not sector_center_lat or not sector_center_lon:
                        pos = [v for v in state.uavs.values()
                               if not v.destroyed]
                        if pos:
                            sector_center_lat = sum(p.latitude for p in pos) / len(pos)
                            sector_center_lon = sum(p.longitude for p in pos) / len(pos)
                        else:
                            sector_center_lat, sector_center_lon = 27.0, 125.0
                        log(f"[run] sector center auto-filled from UAV "
                            f"centroid: ({sector_center_lat:.6f}, "
                            f"{sector_center_lon:.6f})")
                    n = len(alive_uids)
                    for idx, uid in enumerate(alive_uids):
                        sc = SwarmController(my_uid=uid)
                        sc.configure(_cfg)
                        sc.set_fleet_index(idx, n)
                        sc.set_sector_center(float(sector_center_lat),
                                              float(sector_center_lon))
                        controllers[uid] = sc
                    log(f"[run] built {n} SwarmController(s); "
                        f"sector search over {n} sectors")
                    # Spec 025: cooperative continuous-tracking evaluator.
                    # K adapts to fleet size: ceil(30% of initial UAVs), >=2.
                    eval_K = max(2, math.ceil(len(alive_uids) * 0.3))
                    true_target_uids = set(state.targets)
                    evaluator = CoopTrackingEvaluator(
                        profile_adversarial_swarm_search(
                            duration_s=args.duration, K=eval_K),
                        true_target_uids)
                    score_timeline: list[dict] = []
                    # Spec 025 (sim:score): per-tick live-score publisher
                    # for the front-end.
                    score_pub = ScorePublisher(
                        host=args.redis_host, port=args.redis_port,
                        connect=not args.dry_run,
                    )
                    log(f"[run] evaluation: K={eval_K}, "
                        f"targets={len(true_target_uids)}, dwell=20s, grace=2s")

                # Spec 019: per-UAV cooperative search + track. Drop
                # controllers whose UAV got destroyed (self-termination is
                # per-process, but in this single-process multi-UAV runner
                # we just stop issuing commands for destroyed UAVs and
                # re-assign sectors among the survivors).
                alive_now = sorted(
                    u for u, v in state.uavs.items() if not v.destroyed)
                if len(alive_now) != len(controllers):
                    # Rebuild sector assignment over survivors so coverage
                    # stays even after attrition.
                    for uid in list(controllers):
                        if uid not in alive_now:
                            del controllers[uid]
                    for idx, uid in enumerate(alive_now):
                        sc = controllers.get(uid)
                        if sc is None:
                            sc = SwarmController(my_uid=uid)
                            sc.configure(_cfg)
                            sc.set_sector_center(float(sector_center_lat or 27.0),
                                                  float(sector_center_lon or 125.0))
                            controllers[uid] = sc
                        sc.set_fleet_index(idx, len(alive_now))

                # Spec 025: observe cooperative tracking + live score.
                _uavs, _tts, _dcs = _swarm_to_eval_inputs(state)
                _umap = resolve_uav_to_target(_uavs, _tts, _dcs)
                _destroyed = {u for u, v in state.uavs.items() if v.destroyed}
                evaluator.observe(state.sim_time, _umap, _destroyed)
                _alive_rate = (len(state.uavs) - len(_destroyed)) / max(1, len(state.uavs))
                _snap = evaluator.score({"alive_rate": _alive_rate, "sim_t0": sim_t0})
                score_timeline.append({
                    "sim_time": state.sim_time,
                    "total_score": _snap["total_score"],
                    "completion_rate": _snap["completion_rate"],
                })
                # Spec 025 (sim:score): publish live score for the front-end.
                score_pub.publish(
                    _snap, sim_time=state.sim_time, tick=evaluator.tick_count,
                )


                if controllers:
                    period = 1.0 / 10.0  # 10 Hz control loop
                    # Consume peer broadcasts from each UAV's
                    # comm inbox (info-isolated: payload only, no status).
                    for uid, ent in raw.items():
                        if not isinstance(ent, dict):
                            continue
                        inbox = (ent.get("comm", {}) or {}).get("inbox", []) or []
                        for msg in inbox:
                            sender = msg.get("sender", "")
                            payload = str(msg.get("payload", "")).strip()
                            if not sender or not payload:
                                continue
                            # Two payload formats:
                            #   T:<uid>           — legacy: peer is tracking uid.
                            #   R:<lat>,<lon>     — peer confirmed a REAL target
                            #                       near (lat, lon) (coop summon).
                            if payload.startswith("T:"):
                                tgt = payload[2:].strip()
                                if tgt and tgt != "?":
                                    for sc in controllers.values():
                                        if sc.my_uid != sender:
                                            sc.observe_peer_tracking(sender, tgt)
                            elif payload.startswith("R:"):
                                for sc in controllers.values():
                                    if sc.my_uid != sender:
                                        sc.observe_peer_tracking(sender, payload)
                    cmds: list[dict] = []
                    for uid, sc in controllers.items():
                        cmds.extend(sc.decide(state, period))
                    if cmds and not args.dry_run:
                        agg["avoidance_commands_total"] += len(cmds)
                        for cmd in cmds:
                            r.publish(args.redis_command_channel,
                                      json.dumps(cmd))
                    elif cmds and args.dry_run:
                        agg["avoidance_commands_total"] += len(cmds)

                if state.sim_time - last_print_sim_t >= 1.0:
                    last_print_sim_t = state.sim_time
                    log(f"  t={state.sim_time:6.1f}  alive={s['n_alive']:2d}  "
                        f"destroyed={s['n_destroyed']:2d}  jammed={s['n_jammed']:2d}  "
                        f"tracking={s['n_tracking']:2d}  misid={s['n_misid']:2d}  "
                        f"zones=[ad:{s['n_zones_air_defense']} "
                        f"sj:{s['n_zones_static_jam']} rj:{s['n_zones_random_jam']}]")
                if args.duration > 0 and state.sim_time >= target_sim_end:
                    break
                if state.status == "ended":
                    log("[run] simulator reported status=ended; exiting")
                    break

            # If the sim subprocess exited (crash, or ended without an
            # "ended" frame we caught), finalize now with the data collected
            # so far instead of looping idle to the wall-clock timeout.
            if sim_proc is not None and sim_proc.poll() is not None:
                log(f"[run] opensim-sim exited (code {sim_proc.returncode}); "
                    f"finalizing with data collected so far")
                break
            # Hard wall-clock timeout (4x the sim duration) so dry-run can't loop forever.
            # Spec 024: 仅在有限时长模式下生效。web bridge 强制传 --duration 0
            # (无限运行),此时 args.duration*4+10 == 10s 会在 sim 加载
            # HeightSample.csv(~750MB,需数十秒到 >1 分钟才发首帧)期间
            # 误触发退出 → controller_exited。与上方 :410 的 duration>0 守卫对齐。
            if args.duration > 0 and time.time() - wall_t0 > args.duration * 4 + 10:
                log("[run] wall-clock timeout reached; exiting")
                break

        if last_state is None:
            log("[run] ERROR: no state received; is the sim running?")
            return 3

        # Aggregate discovery + tracking metrics across all controllers.
        discovered: set[str] = set()
        track_duration: dict[str, float] = {}
        for sc in controllers.values():
            discovered |= sc.discovered_targets
            for t_uid, dur in sc.track_duration_s.items():
                track_duration[t_uid] = track_duration.get(t_uid, 0.0) + dur
        n_targets_total = max(1, len(last_state.targets))
        n_discovered = len(discovered & set(last_state.targets))
        n_tracked_2min = sum(1 for d in track_duration.values() if d >= 120.0)

        # Finalize + save metrics.
        out_dir = Path(args.output)
        ts = int(time.time())
        sim_dur = last_state.sim_time - sim_t0
        wall_dur = time.time() - wall_t0
        summary = {
            "controller": "spec-019 swarm_controller (sector search + track)",
            "scenario": str(args.scenario),
            "n_uav": len(last_state.uavs),
            "n_targets": len(last_state.targets),
            "n_decoys": len(last_state.decoys),
            "samples": agg["samples"],
            "n_destroyed_peak": agg["n_destroyed_peak"],
            "n_jammed_peak": agg["n_jammed_peak"],
            "avoidance_commands_total": agg["avoidance_commands_total"],
            "n_targets_discovered": n_discovered,
            "target_discovery_ratio": n_discovered / n_targets_total,
            "n_targets_tracked_2min": n_tracked_2min,
            "track_duration_s": track_duration,
            "sim_duration_s": sim_dur,
            "wall_duration_s": wall_dur,
        }
        # Spec 025: final evaluation snapshot + per-tick score timeline.
        _final_alive_rate = (len(last_state.uavs) - len({
            u for u, v in last_state.uavs.items() if v.destroyed
        })) / max(1, len(last_state.uavs))
        evaluation = evaluator.score({
            "alive_rate": _final_alive_rate,
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
            "SPEC 019 PHASE 5 (DISTRIBUTED COORDINATION) COMPLETE",
            [
                f"  samples             : {summary['samples']}",
                f"  destroyed-peak      : {summary['n_destroyed_peak']}/{summary['n_uav']}",
                f"  jammed-peak         : {summary['n_jammed_peak']}/{summary['n_uav']}",
                f"  avoidance commands  : {summary['avoidance_commands_total']}",
                f"  targets discovered  : {n_discovered}/{len(last_state.targets)} "
                f"({summary['target_discovery_ratio']*100:.0f}%)",
                f"  targets tracked>=2m : {n_tracked_2min}",
                f"  sim duration        : {sim_dur:.1f} s",
                f"  wall duration       : {wall_dur:.1f} s",
                f"  --- EVALUATION (Spec 025) ---",
                f"  total score         : {evaluation['total_score']:.1f} / 100  "
                f"(passed={evaluation['passed']})",
                f"  K/dwell/grace       : {evaluation['K']} / "
                f"{evaluation['dwell_target_s']}s / {evaluation['grace_s']}s",
                f"  completed           : {evaluation['n_completed']}/"
                f"{evaluation['n_targets']}  (rate={evaluation['completion_rate']:.2f})",
                f"  alive rate          : {evaluation['alive_rate']:.2f}",
                f"  evaluation json     : {eval_path}",
                f"  metrics json        : {j_path}",
            ],
            log=log,
        )
        return 0
    finally:
        stop_sim(sim_proc)
        score_pub.close()


if __name__ == "__main__":
    sys.exit(main())
