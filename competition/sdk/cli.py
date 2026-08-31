"""Command-line entry point.

Usage:
    python -m competition run --scenario search_track --agent my_agent:MyAgent --duration 60
    python -m competition run --scenario coop_decoy --agent my_pkg:MyAgent
    python -m competition run --scenario adversarial_swarm --agent baselines.swarm_distributed:SwarmDistributedAgent --no-start-sim

The ``--agent`` value is ``module.path:ClassName``. The class must be a
subclass of that scenario's Agent base (SearchTrackAgent / CoopAgent /
SwarmAgent).
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path
from typing import Type

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _resolve_redis_port(scenario: str, scenario_json: str | None) -> int:
    """Read redis_port from scenario.json (start.ps1 may have changed it
    via port auto-increment). Falls back to 6379."""
    import json
    if scenario_json:
        sj_path = Path(scenario_json)
    else:
        # parents[1] = competition/ (works in both release and source repo)
        sj_path = Path(__file__).resolve().parents[1] / "scenarios" / scenario / "scenario.json"
    try:
        with open(sj_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        port = cfg.get("simulation", {}).get("redis_port")
        if port:
            return int(port)
    except Exception as e:
        # 不静默吞异常: redis_port 读错会连不上 Redis,根因消失在静默里极难排障。
        # 回退到默认 6379 但打印警告(见 CLAUDE.md 跨平台规范)。
        print(f"[cli] WARNING: 读取 {sj_path} 的 redis_port 失败,回退 6379: {e!r}",
              file=sys.stderr, flush=True)
    return 6379


def _load_agent_class(spec: str):
    if ":" not in spec:
        raise ValueError(f"--agent must be 'module.path:ClassName', got {spec!r}")
    module_path, _, class_name = spec.rpartition(":")
    mod = None
    # Try the path as-is first (absolute import from repo root), then fall
    # back to resolving it under the ``competition`` package so players can
    # write ``baselines.xxx:Cls`` or ``templates.xxx:Cls`` without the prefix.
    for candidate in (module_path, f"competition.{module_path}"):
        try:
            mod = importlib.import_module(candidate)
            break
        except ModuleNotFoundError:
            continue
    if mod is None:
        raise ImportError(f"could not import {module_path!r} (or competition.{module_path})")
    cls = getattr(mod, class_name, None)
    if cls is None:
        raise ImportError(f"{class_name!r} not found in module {module_path!r}")
    return cls


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="competition",
                                description="Competition Agent SDK runner")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run a scenario with a player agent")
    run_p.add_argument("--scenario", required=True,
                       choices=["search_track", "coop_decoy", "adversarial_swarm"])
    run_p.add_argument("--agent", required=True,
                       help="module.path:ClassName")
    run_p.add_argument("--duration", type=float, default=None)
    run_p.add_argument("--scenario-json", default=None,
                       help="path to scenario.json (default: the example's)")
    run_p.add_argument("--seed", type=int, default=0,
                       help="randomize the scene (target routes, threat "
                            "zones, start positions) from this seed; 0 = "
                            "use simulation.seed in scenario.json if set, "
                            "else no randomization")
    run_p.add_argument("--visualize", action="store_true",
                       help="open the 3D visualization in a browser "
                            "(starts the Redis↔WS bridge + a static "
                            "frontend server). Bystander only; does not "
                            "affect scoring. Requires Node.js + the "
                            "visualization/ folder.")
    run_p.add_argument("--viz-dir", default=None,
                       help="path to the visualization/ folder (default: "
                            "<repo>/visualization)")
    run_p.add_argument("--no-browser", action="store_true",
                       help="with --visualize, don't auto-open the browser")
    run_p.add_argument("--output", default="output")
    run_p.add_argument("--start-sim", dest="start_sim", action="store_true",
                       default=True, help="spawn opensim-sim (default)")
    run_p.add_argument("--no-start-sim", dest="start_sim", action="store_false",
                       help="connect to an already-running opensim-sim")
    run_p.add_argument("--dry-run", action="store_true",
                       help="no Redis / no engine (synthetic loop)")
    run_p.add_argument("--mode", choices=["train", "eval"], default="train",
                       help="感知层运行模式：train=AccuracySimulator(概率采样)，"
                            "eval=YoloDetector(真实YOLO)")
    run_p.add_argument("--photo-mode", choices=["auto", "on", "off"],
                       default="auto",
                       help="相机帧拉取模式：auto(默认,非 dry_run 自动拉取 UE 渲染的"
                            "PNG 相机帧)/on(显式启用)/off(禁用,obs.self.photo 恒 None)")
    run_p.add_argument("--accuracy", type=float, default=0.85,
                       help="AccuracySimulator 检出概率 (train 模式)")
    run_p.add_argument("--noise-sigma", type=float, default=50.0,
                       help="AccuracySimulator 位置噪声标准差（米）")
    run_p.add_argument("--max-detection-range", type=float, default=None,
                       help="AccuracySimulator 最大探测距离（米），超出必检不出；"
                            "缺省读 scenario.json perception.max_detection_range_m；"
                            "0=禁用")
    run_p.add_argument("--full-accuracy-range", type=float, default=None,
                       help="满精度距离（米），以内不做距离衰减；"
                            "缺省读 scenario.json perception.full_accuracy_range_m")
    run_p.add_argument("--yolo-model", default="",
                       help="YoloDetector 模型路径 (eval 模式)")
    run_p.add_argument("--quiet", action="store_true")
    run_p.add_argument("--sim-binary", default=None)
    run_p.add_argument("--redis-host", default="127.0.0.1")
    run_p.add_argument("--redis-port", type=int, default=None,
                       help="Redis port (default: read from scenario.json "
                            "or 6379)")
    args = p.parse_args(argv)

    # Resolve redis_port: explicit arg > scenario.json > 6379
    if args.redis_port is None:
        args.redis_port = _resolve_redis_port(args.scenario, args.scenario_json)

    # 防真值泄漏钳制（与 ScenarioConfig.__post_init__ 同口径）：accuracy 上界
    # 0.9、noise_sigma 下界 30m。越界值在此 clamp，杜绝 CLI 传入 acc=1.0 /
    # noise=0 导致退化等价真值。
    if args.accuracy > 0.9:
        print(f"[cli] --accuracy {args.accuracy} exceeds 0.9 cap; clamped to 0.9")
        args.accuracy = 0.9
    if args.noise_sigma < 30.0:
        print(f"[cli] --noise-sigma {args.noise_sigma} below 30m floor; clamped to 30.0")
        args.noise_sigma = 30.0

    # 距离门限：显式负值钳 0；None 透传（run() 内回退读 scenario.json perception 块）。
    if args.max_detection_range is not None and args.max_detection_range < 0:
        print(f"[cli] --max-detection-range {args.max_detection_range} negative; "
              f"clamped to 0 (disabled)")
        args.max_detection_range = 0.0
    if args.full_accuracy_range is not None and args.full_accuracy_range < 0:
        args.full_accuracy_range = 0.0

    # 相机帧模式：单一三态开关 auto/on/off（默认 auto = 非 dry_run 自动拉取 UE 帧）。
    photo_mode = args.photo_mode

    agent_cls = _load_agent_class(args.agent)

    common = dict(
        scenario=args.scenario_json, start_sim=args.start_sim,
        output_dir=args.output, host=args.redis_host, port=args.redis_port,
        dry_run=args.dry_run, quiet=args.quiet, sim_binary=args.sim_binary,
    )

    viz_kwargs = dict(visualize=args.visualize, viz_dir=args.viz_dir,
                      open_browser=not args.no_browser)
    if args.scenario == "search_track":
        from competition.sdk.scenarios.search_track import run
        run(agent_cls, duration=args.duration or 600.0, seed=args.seed,
            mode=args.mode, photo_mode=photo_mode,
            accuracy=args.accuracy, noise_sigma_m=args.noise_sigma,
            yolo_model_path=args.yolo_model,
            max_detection_range_m=args.max_detection_range,
            full_accuracy_range_m=args.full_accuracy_range,
            **viz_kwargs, **common)
    elif args.scenario == "coop_decoy":
        from competition.sdk.scenarios.coop_decoy import run
        run(agent_cls, duration=args.duration or 600.0,
            seed=args.seed,
            mode=args.mode, photo_mode=photo_mode,
            accuracy=args.accuracy, noise_sigma_m=args.noise_sigma,
            yolo_model_path=args.yolo_model,
            max_detection_range_m=args.max_detection_range,
            full_accuracy_range_m=args.full_accuracy_range,
            **viz_kwargs, **common)
    elif args.scenario == "adversarial_swarm":
        from competition.sdk.scenarios.adversarial_swarm import run
        run(agent_cls, duration=args.duration or 600.0, seed=args.seed,
            mode=args.mode, photo_mode=photo_mode,
            accuracy=args.accuracy, noise_sigma_m=args.noise_sigma,
            yolo_model_path=args.yolo_model,
            max_detection_range_m=args.max_detection_range,
            full_accuracy_range_m=args.full_accuracy_range,
            **viz_kwargs, **common)
    return 0


if __name__ == "__main__":
    sys.exit(main())
