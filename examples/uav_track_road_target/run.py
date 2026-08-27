"""Run the UAV-track-road-target example.

A UAV tracks a target vehicle that follows road1 from points.json.
Target navigates via A* (set_goal), UAV chases with gimbal tracking.

Usage:
    python -m examples.uav_track_road_target.run                  # require sim already running
    python -m examples.uav_track_road_target.run --start-sim       # also start opensim-sim
    python -m examples.uav_track_road_target.run --road road2      # use a different road
    python -m examples.uav_track_road_target.run --no-loop         # stop after one lap
    python -m examples.uav_track_road_target.run --dry-run         # don't publish to Redis

Requires:
    - Redis on 127.0.0.1:6379
    - opensim-sim running with examples/uav_track_road_target/config/scenario.json
      (or use --start-sim to spawn it)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE
from examples._common.argparser import bootstrap_paths  # noqa: E402
REPO_ROOT = bootstrap_paths(EXAMPLE_DIR)

from search_track.road_tracker import (  # noqa: E402
    RoadTracker, RoadTrackerConfig, build_waypoint_list, haversine_m, load_road,
)

import redis  # noqa: E402

from examples._common.sim_runner import start_sim, stop_sim  # noqa: E402

CMD_CHANNEL = "sim:commands"
STATE_CHANNEL = "sim:state"
EVENTS_CHANNEL = "sim:events"

TARGET_ID = "10001"
UAV_ID = "10002"

DEFAULT_POINTS = REPO_ROOT / "config" / "points.json"


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="UAV tracks target along A* road waypoints (looping)")
    p.add_argument("--config", type=str,
                   default=str(EXAMPLE_DIR / "config" / "algorithm.yaml"))
    p.add_argument("--scenario", type=str,
                   default=str(EXAMPLE_DIR / "config" / "scenario.json"))
    p.add_argument("--points", type=str, default=str(DEFAULT_POINTS))
    p.add_argument("--road", type=str, default="road1")
    p.add_argument("--no-loop", action="store_true")
    p.add_argument("--duration", type=float, default=0.0,
                   help="Max sim seconds (0 = run until Ctrl+C or done)")
    p.add_argument("--start-sim", action="store_true")
    p.add_argument("--sim-binary", type=str,
                   default=os.environ.get(
                       "OPENSIM_SIM_BIN",
                       str(REPO_ROOT / "build" / (
                           "opensim-sim.exe" if sys.platform == "win32"
                           else "opensim-sim"))))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--redis-host", type=str, default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=6379)
    p.add_argument("--quiet", action="store_true")
    return p


# ── Helpers ──────────────────────────────────────────────────────────────

def _entity(state: dict, eid: str) -> dict:
    return state.get(eid, {})


def _pos(entity: dict) -> dict:
    return entity.get("platform", {}).get("position", {})


def _att(entity: dict) -> dict:
    return entity.get("platform", {}).get("attitude", {})


def _gimbal(entity: dict) -> dict:
    return entity.get("gimbal_tracking", {})


_quiet = False


def publish_cmd(r: redis.Redis, target: str, cmd: str, params: dict,
                dry: bool = False) -> None:
    msg = {"target": target, "cmd": cmd, "params": params, "timestamp": time.time()}
    if not dry:
        r.publish(CMD_CHANNEL, json.dumps(msg))
    if not _quiet:
        # Compact log: only show key params
        if cmd == "set_goal":
            print(f"  [CMD] {target}.{cmd}  lat={params.get('latitude',''):.6f} lon={params.get('longitude',''):.6f}")
        else:
            print(f"  [CMD] {target}.{cmd}")


# ── Main ─────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    global _quiet

    args = build_argparser().parse_args(argv)
    _quiet = args.quiet
    log = (lambda *a, **kw: None) if args.quiet else print

    # 1) Load config & road
    cfg = RoadTrackerConfig.from_yaml(args.config)
    road_name = args.road or "road1"
    if args.no_loop:
        cfg.loop = False

    road = load_road(args.points, road_name)
    waypoints = build_waypoint_list(road)

    log("=" * 70)
    log(f"SCENARIO: UAV track target along {road['Name']} ({road['Description']})")
    log(f"  waypoints     : {len(waypoints)}")
    log(f"  loop          : {cfg.loop}")
    log(f"  target speed  : {cfg.target_speed} m/s")
    log(f"  follow dist   : {cfg.follow_distance} m")
    log(f"  chase alt AGL : {cfg.chase_altitude_agl} m")
    log("=" * 70)

    # 2) Start sim
    sim_proc = None
    if args.start_sim:
        sim_proc = start_sim(args.sim_binary, args.scenario, log=log)
        if sim_proc is None:
            return 2

    # 3) Connect to Redis
    r = redis.Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
    pubsub = r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(STATE_CHANNEL)

    running = True
    def sighandler(signum, frame):
        nonlocal running
        print("\n  Shutting down...")
        running = False
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    try:
        # 4) Wait for first state
        log("\n[phase 1] Waiting for first sim:state...")
        t_lat = t_lon = t_alt = t_heading = None
        u_lat = u_lon = u_alt = u_yaw = None
        deadline = time.time() + 30.0
        while time.time() < deadline:
            msg = pubsub.get_message(timeout=1.0)
            if msg and msg.get("type") == "message":
                try:
                    s = json.loads(msg["data"])
                    t = _entity(s, TARGET_ID)
                    u = _entity(s, UAV_ID)
                    t_lat = _pos(t).get("latitude")
                    t_lon = _pos(t).get("longitude")
                    t_alt = _pos(t).get("altitude", 0.0)
                    t_heading = t.get("heading", 0.0)
                    u_lat = _pos(u).get("latitude")
                    u_lon = _pos(u).get("longitude")
                    u_alt = _pos(u).get("altitude", 0.0)
                    u_yaw = _att(u).get("yaw", 0.0)
                    if t_lat is not None and u_lat is not None:
                        break
                except json.JSONDecodeError:
                    continue

        if t_lat is None or u_lat is None:
            log("ERROR: No sim:state received. Is opensim-sim running?")
            return 3

        g = _gimbal(_entity(s, UAV_ID))
        log(f"  target @ ({t_lat:.5f}, {t_lon:.5f}, {t_alt:.1f}m) hdg={t_heading:.1f}")
        log(f"  uav    @ ({u_lat:.5f}, {u_lon:.5f}, {u_alt:.1f}m) yaw={u_yaw:.1f}")

        # 5) Create tracker
        tracker = RoadTracker(waypoints, cfg)
        sim_t0 = float(s.get("sim_time", 0.0))

        # 6) Main loop
        log("\n[phase 2] A* waypoint navigation + UAV tracking (Ctrl+C to stop)\n")
        t0 = time.time()
        last_print_elapsed = -1.0
        prev_wp_label: str | None = None

        while running:
            msg = pubsub.get_message(timeout=0.05)
            if not (msg and msg.get("type") == "message"):
                continue
            try:
                s = json.loads(msg["data"])
            except json.JSONDecodeError:
                continue

            sim_time = float(s.get("sim_time", 0.0))
            t = _entity(s, TARGET_ID)
            u = _entity(s, UAV_ID)

            t_lat = _pos(t).get("latitude", t_lat)
            t_lon = _pos(t).get("longitude", t_lon)
            t_alt = _pos(t).get("altitude", t_alt or 0.0)
            t_heading = t.get("heading", t_heading or 0.0)
            u_lat = _pos(u).get("latitude", u_lat)
            u_lon = _pos(u).get("longitude", u_lon)
            u_alt = _pos(u).get("altitude", u_alt or 0.0)
            u_yaw = _att(u).get("yaw", u_yaw)

            # Duration check
            if args.duration > 0 and (sim_time - sim_t0) >= args.duration:
                log(f"\n[run] duration limit reached ({args.duration}s)")
                break

            if tracker.phase == "DONE" and not cfg.loop:
                log("\n[run] all waypoints completed (no-loop)")
                break

            # Tracker decide
            wall_time = time.time()
            tracker_cmds = tracker.decide(
                sim_time=sim_time, wall_time=wall_time,
                tgt_lat=t_lat, tgt_lon=t_lon,
                tgt_alt=t_alt, tgt_heading=t_heading,
                uav_lat=u_lat, uav_lon=u_lon,
                uav_alt=u_alt, uav_yaw=u_yaw,
                publish_fn=lambda tgt, cmd, params: publish_cmd(
                    r, tgt, cmd, params, dry=args.dry_run),
            )

            # Log waypoint transitions
            wp = tracker.current_waypoint
            wp_label = wp.label if wp else "END"
            if wp_label != prev_wp_label:
                if prev_wp_label is not None:
                    d = haversine_m(t_lat, t_lon, wp.lat, wp.lon) if wp else 0
                    log(f"  >>> waypoint: {prev_wp_label} → {wp_label}  dist_to_wp={d:.0f}m")
                prev_wp_label = wp_label

            # Status log every sim-second
            elapsed = sim_time - sim_t0
            if int(elapsed) > last_print_elapsed:
                last_print_elapsed = int(elapsed)
                d_h = haversine_m(t_lat, t_lon, u_lat, u_lon)
                phase = tracker.phase
                lap = tracker.lap_count
                log(
                    f"  t={elapsed:6.1f}  phase={phase:4s}  lap={lap}  "
                    f"wp={wp_label:>8s}  "
                    f"tgt=({t_lat:.5f}, {t_lon:.5f})  "
                    f"uav=({u_lat:.5f}, {u_lon:.5f}, {u_alt:.0f}m)  "
                    f"dist={d_h:.0f}m"
                )

        # 7) Finalize
        log("")
        log("=" * 70)
        log("SCENARIO COMPLETE")
        d_h = haversine_m(t_lat, t_lon, u_lat, u_lon)
        log(f"  elapsed       : {time.time() - t0:.1f} s (wall)")
        log(f"  laps          : {tracker.lap_count}")
        log(f"  target        : ({t_lat:.5f}, {t_lon:.5f}, {t_alt:.1f}m)")
        log(f"  uav           : ({u_lat:.5f}, {u_lon:.5f}, {u_alt:.1f}m)")
        log(f"  haversine dist: {d_h:.1f} m")
        log("=" * 70)

    finally:
        pubsub.close()
        stop_sim(sim_proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
