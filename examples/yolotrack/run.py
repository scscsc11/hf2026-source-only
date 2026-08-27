"""Run the yolotrack example (赛题四).

与 uav_search_track_car 99% 对齐，差异：
  - controller 默认为 YoloSearchTrackController
  - algorithm.yaml 多一个 yolo 块
  - run.py 启动前把 model_path 解析为绝对路径
  - run.py 退出时关闭 YoloVisionWorker 后台线程
  - 加 --no-yolo flag：等同于原 FSM 行为

Usage:
    # CLI 模式（含子进程启动 sim）
    python -m examples.yolotrack.run --start-sim --duration 60

    # 前端模式（前端已 spawn sim + controller，本进程只连 redis 收 sim:state）
    python -m examples.yolotrack.run --duration 0

    # 关闭 YOLO 走原 FSM（不加载模型 / 不订阅相机帧）
    python -m examples.yolotrack.run --no-yolo --start-sim --duration 30

Requires:
    - Redis on 127.0.0.1:6379
    - opensim-sim running (use --start-sim to spawn)
    - UE renderer pushing camera frames to sync_camera:{uav_id}:frame:* (spec/022)

环境变量（可选）：
    YOLO_SAVE_MISS_DIR  未识别帧保存目录（PNG）。覆盖下方默认值。
    YOLOTRACK_LOG_LEVEL  logger 级别，默认 INFO。可设 DEBUG 看每帧 HIT。
"""
from __future__ import annotations

# === 调试：未识别帧默认保存路径 ===
# 前端模式（spec/024）spawn controller 时不会传环境变量，所以这里硬编码
# 兜底。仍可用 YOLO_SAVE_MISS_DIR 环境变量覆盖。
import os as _os
_DEFAULT_MISS_DIR = (
    "/home/lpwang/ZCodeProject/opensim/examples/yolotrack/output/yolo_miss"
)
if not _os.environ.get("YOLO_SAVE_MISS_DIR"):
    _os.environ["YOLO_SAVE_MISS_DIR"] = _DEFAULT_MISS_DIR
# 确保目录存在
_os.makedirs(_DEFAULT_MISS_DIR, exist_ok=True)

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# 让本 example 既可作为 `python -m examples.yolotrack.run` 跑，也可 cd 进去跑
HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE

from examples._common.argparser import (
    bootstrap_paths,
    build_standard_parser,
)
REPO_ROOT = bootstrap_paths(EXAMPLE_DIR)

# search_track 子包在兄弟 example 目录（uav_search_track_car/）。
# 我们继承 FsmSearchTrackController，需要把它也加到 sys.path。
# （注：未来如果 search_track 被提到 examples/_common/，可去掉。）
sys.path.insert(0, str(REPO_ROOT / "examples" / "uav_search_track_car"))

# 配置 yolotrack logger 级别（默认 WARNING 太安静）
import logging as _logging
_yolotrack_log_level = os.environ.get("YOLOTRACK_LOG_LEVEL", "INFO").upper()
_logging.basicConfig(
    level=_yolotrack_log_level,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

from search_track.client import SimClient  # noqa: E402
from search_track.commands import ControlCommand  # noqa: E402
from search_track.controller import load_controller  # noqa: E402
from search_track.metrics import MetricsRecorder  # noqa: E402

from examples._common.sim_runner import start_sim  # noqa: E402
from examples._common.metrics_summary import write_json, print_completion_banner  # noqa: E402

from yolotrack.yolo_controller import YoloSearchTrackController  # noqa: E402


def build_argparser() -> argparse.ArgumentParser:
    extra = [
        (["--config"], {"type": str,
                        "default": str(EXAMPLE_DIR / "config" / "algorithm.yaml")}),
        (["--controller"], {"type": str, "default": None,
                            "help": "override controller spec"}),
        (["--no-yolo"], {"action": "store_true",
                         "help": "disable YOLO vision (fallback to original FSM)"}),
        (["--road"], {"type": str, "default": "road1",
                      "help": "A* road name from config/points.json (road1/road2/...)"}),
    ]
    return build_standard_parser(
        description="YOLOv8-driven gimbal tracker (赛题四)",
        example_dir=EXAMPLE_DIR,
        default_duration=60.0,
        extra=extra,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    log = (lambda *a, **kw: None) if args.quiet else print

    # 1) 加载 algorithm config（yaml）。
    # 注意：使用 yaml.safe_load 直接读为 dict，因为 AlgorithmConfig 是
    # dataclass（无 yolo 字段）；dict 才能完整携带 yolo 块给 controller。
    import yaml
    with open(args.config, "r", encoding="utf-8-sig") as f:
        cfg = yaml.safe_load(f)
    if args.controller:
        cfg["controller"] = args.controller
    if args.no_yolo:
        cfg.setdefault("yolo", {})["enabled"] = False
        log("[run] --no-yolo: YOLO 已禁用，走原 FSM 行为")
    log(f"[run] algorithm config: {args.config}")
    log(f"[run] controller: {cfg['controller']}")

    # 把 yolo.model_path 解析为绝对路径（避免 worker 找不到）
    _resolve_model_path(cfg, EXAMPLE_DIR, log)

    # 2) 可选启动 opensim-sim
    sim_proc = None
    if args.start_sim:
        sim_proc = start_sim(args.sim_binary, args.scenario, log=log)
        if sim_proc is None:
            return 2

    # 3) 连接
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
            # A* waypoints 在下面注入给 controller（controller 自己发 set_goal）
        else:
            # dry-run: 合成 first state
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

        # 4) 构造 controller
        controller = load_controller(cfg["controller"])
        if hasattr(controller, "configure"):
            controller.configure(cfg)
        controller.reset()

        # 4.5) 注入 A* waypoints 给 controller（前端模式也能自动发 set_goal）
        road_wps = _load_road_waypoints(args.road, REPO_ROOT, log)
        if road_wps and hasattr(controller, "set_astar_waypoints"):
            controller.set_astar_waypoints(road_wps, arrival_m=30.0)
            log(f"[run] A* waypoints 已注入 controller: {len(road_wps)} 个")

        # 5) 主循环
        recorder = MetricsRecorder()
        rate = float(_get_cfg(cfg, "control_rate_hz", 10))
        period = 1.0 / rate
        sim_t0 = first.sim_time
        target_sim_end = sim_t0 + args.duration
        log(f"[run] control loop @ {rate} Hz for {args.duration:.1f} sim-seconds")
        log(f"[run] mode=SEARCH (initial)")

        prev_mode = getattr(controller, "mode", "SEARCH")
        last_state = first
        start_wall = time.time()
        # dry-run 保护：state.sim_time 永远不推进，所以加 wall-time 兜底
        dry_run_wall_budget_s = 2.0 if args.dry_run else None

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
                # dry-run wall-time 兜底（state.sim_time 永不变）
                if dry_run_wall_budget_s is not None:
                    if time.time() - start_wall > dry_run_wall_budget_s:
                        log("[run] dry-run wall-time budget reached, exiting")
                        break

                safe_state = state.without_truth()
                # 注入目标真值给 controller（供 A* 到达检测）
                if state.target_truth is not None and hasattr(controller, "set_last_truth"):
                    controller.set_last_truth(
                        state.target_truth.position.latitude,
                        state.target_truth.position.longitude,
                    )
                cmds = controller.decide(safe_state, period)

                if not args.dry_run:
                    for cmd in cmds:
                        # set_goal 要发给 "target" entity，不是 UAV
                        # controller 用 CommandTarget.UAV 占位，这里改 target
                        if cmd.cmd == "set_goal":
                            client.publish_dict({
                                "target": "target",
                                "cmd": "set_goal",
                                "params": dict(cmd.params),
                            })
                        else:
                            client.publish(cmd)

                # 模式切换日志
                cur_mode = getattr(controller, "mode", "?")
                if cur_mode != prev_mode:
                    log(f"[run] t={state.sim_time:.1f}  mode {prev_mode} -> {cur_mode}")
                    prev_mode = cur_mode

                # 每秒打印一行状态 + 关键诊断信息
                if int(state.sim_time) > int(last_state.sim_time):
                    yolo_hits = getattr(controller, "yolo_hits", None)
                    yolo_fb = getattr(controller, "yolo_fallbacks", None)
                    extra = ""
                    if yolo_hits is not None:
                        extra = f"  yolo_hits={yolo_hits}  yolo_fallbacks={yolo_fb}"
                    g = state.gimbal
                    log(f"  t={state.sim_time:6.1f}  mode={cur_mode:6s}  "
                        f"detected={state.detection.detected}  "
                        f"gimbal=({g.pan_angle:6.1f},{g.tilt_angle:6.1f}){extra}")

                last_state = state

        finally:
            # 关闭 controller 资源（YoloVisionWorker 后台线程）
            if hasattr(controller, "shutdown"):
                controller.shutdown()
            log("[run] controller resources released")

        # 6) 输出
        total_wall = time.time() - start_wall
        log(f"[run] loop ended: {total_wall:.1f}s wall-time")
        if hasattr(controller, "yolo_hits"):
            log(f"[run] YOLO 命中={controller.yolo_hits}, fallback={controller.yolo_fallbacks}")
        summary = {
            "controller": cfg["controller"],
            "duration_s": args.duration,
            "wall_time_s": total_wall,
            "yolo_enabled": bool(_get_cfg(cfg, "yolo.enabled", False)),
            "yolo_hits": getattr(controller, "yolo_hits", 0),
            "yolo_fallbacks": getattr(controller, "yolo_fallbacks", 0),
        }
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = time.strftime("%Y%m%d_%H%M%S")
        write_json(summary, out_dir, f"run_{run_id}.json")
        log(f"[run] summary: {out_dir}/run_{run_id}.json")
        return 0

    finally:
        if sim_proc is not None:
            from examples._common.sim_runner import stop_sim
            stop_sim(sim_proc)


# ------------------------------------------------------------------
# 工具
# ------------------------------------------------------------------

def _resolve_model_path(cfg, example_dir: Path, log) -> None:
    """如果 yolo.model_path 是相对路径，转成 example_dir 下的绝对路径。"""
    yolo_cfg = _get_cfg(cfg, "yolo", None)
    if yolo_cfg is None:
        return
    model_path = _get_cfg(yolo_cfg, "model_path", None)
    if not model_path:
        return
    p = Path(model_path)
    if p.is_absolute():
        return
    abs_path = str((example_dir / p).resolve())
    _set_cfg(yolo_cfg, "model_path", abs_path)
    log(f"[run] yolo.model_path resolved to: {abs_path}")


def _get_cfg(cfg, key: str, default):
    """通用 getter：dict 或对象。key 支持 'a.b.c' 嵌套。"""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        if "." in key:
            cur = cfg
            for k in key.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur
        return cfg.get(key, default)
    # 对象
    if "." in key:
        parts = key.split(".")
        cur = cfg
        for p in parts:
            cur = getattr(cur, p, None)
            if cur is None:
                return default
        return cur
    return getattr(cfg, key, default)


def _set_cfg(cfg, key: str, value) -> None:
    """通用 setter：dict 或对象。"""
    if isinstance(cfg, dict):
        cfg[key] = value
    else:
        setattr(cfg, key, value)


def _load_road_waypoints(road_name: str, repo_root: Path, log) -> list:
    """从 config/points.json 读指定 road 的 waypoints。

    Returns:
        [(lat, lon), ...] 列表。找不到返回 []。
    """
    import json
    points_path = repo_root / "config" / "points.json"
    if not points_path.exists():
        log(f"[run] WARN: points.json 不存在: {points_path}")
        return []
    try:
        data = json.loads(points_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log(f"[run] WARN: 解析 points.json 失败: {e}")
        return []
    for road in data.get("Paths", []):
        if road.get("Name") == road_name:
            wps = []
            for w in road.get("Waypoints", []):
                wps.append((float(w["Latitude"]), float(w["Longitude"])))
            log(f"[run] road={road_name}: {len(wps)} 个 waypoints")
            return wps
    log(f"[run] WARN: road={road_name!r} 在 points.json 中未找到")
    return []


def _publish_set_goal(client, waypoint, *, dry_run: bool, log) -> None:
    """发 set_goal 给 target，启动 A* 路径规划。

    Args:
        waypoint: (lat, lon) 元组
    """
    lat, lon = waypoint
    msg = {
        "target": "target",
        "cmd": "set_goal",
        "params": {"latitude": lat, "longitude": lon},
    }
    if dry_run:
        log(f"[run] (dry-run) set_goal: lat={lat:.6f}, lon={lon:.6f}")
    else:
        client.publish_dict(msg)
        log(f"[run] set_goal published: lat={lat:.6f}, lon={lon:.6f}")


if __name__ == "__main__":
    sys.exit(main())
