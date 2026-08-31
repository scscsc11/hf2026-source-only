"""Coop-decoy scenario runner.

Scenario-specific bits:
  * controllable_types = {"uav"} (3 UAVs)
  * briefing: fleet_size=3, mission_area, sector center (static, pre-match)
  * scoring: profile_multi_uav_coop_decoy (K, misid/comm/full_coop dims)
  * score_extras: comm_sent/comm_delivered/sim_t0
  * inject_startup: activate each target via A* navigation
    (replaces original set_trajectory waypoint playback with A* path planning)
  * resolve_k: honour a configurable K (default 2)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from ...core.observation import AreaSpec, GeoPoint, MissionBriefing
from ...core.runner import (
    RunnerBase, ScenarioConfig, read_perception_range, read_weather,
    resolve_scenario_seed,
)
from ...core.scoring import ScoringProfile, profile_multi_uav_coop_decoy
from ...core.world_state import WorldState
from .._astar_navigator import (
    inject_astar_target, inject_astar_decoy, assign_routes, _build_waypoints,
    inject_startup_concurrent,
    make_route_progress_cb, count_injectable_vehicles,
    publish_regenerate_zones,
)


# 赛题二评分规则常量：≥ DEFAULT_K 架 UAV 同时盯防同一真目标满 20s 才摧毁。
# 这是举办方可调的难度旋钮（改这一处即可），选手不可通过 CLI/SDK 修改——
# 仅通过 briefing.params["coop_k"] 向 agent 只读暴露当前取值。
DEFAULT_K = 2


class CoopDecoyRunner(RunnerBase):
    scenario_name = "coop_decoy"
    controllable_types = {"uav"}

    def __init__(self, cfg: ScenarioConfig, agent_cls, log=print) -> None:
        super().__init__(cfg, log)
        self.agent_cls = agent_cls
        self._scenario_cfg = self._load_scenario(cfg.scenario_path)
        self._sector_center = self._resolve_sector_center()
        # uid → 选定路线名（prepare_scenario 选路时记录，inject_startup 用）
        self._route_assignment: Dict[str, str] = {}

    # ── briefing ──────────────────────────────────────────────────────

    def build_briefing(self, world_state: WorldState,
                       entity_uid: str) -> MissionBriefing:
        return MissionBriefing(
            self_uid=entity_uid,
            fleet_size=len(world_state.uavs),
            mission_area=AreaSpec(lat_min=26.98, lat_max=27.02,
                                  lon_min=124.98, lon_max=125.02),
            known_threats=(),
            target_count=len(world_state.targets),
            params={
                "coop_k": DEFAULT_K,
                "sector_center_lat": self._sector_center[0],
                "sector_center_lon": self._sector_center[1],
            },
        )

    def _resolve_sector_center(self) -> Tuple[float, float]:
        # static default (pre-match known); UAV centroid would be dynamic
        return (27.0, 125.0)

    # ── scoring ───────────────────────────────────────────────────────

    def build_scoring(self, world_state: WorldState
                      ) -> Tuple[ScoringProfile, set]:
        profile = profile_multi_uav_coop_decoy(
            duration_s=self.cfg.duration_s, K=DEFAULT_K)
        true_targets = set(world_state.targets.keys())
        return profile, true_targets

    def score_extras(self, world_state: WorldState,
                     destroyed_uids: set) -> Dict[str, Any]:
        # Overhauled scoring: kill/accuracy/misid_penalty are all computed
        # inside the evaluator (accuracy from reports collected in
        # _observe_scoring; misid from per-tick decoy tracking). No extras.
        return {}

    # ── startup injection ─────────────────────────────────────────────

    def prepare_scenario(self) -> None:
        """引擎启动前：为每个真目标和诱饵选路线，把路线 Start 写入 initial_*。

        选路规则（需求 2026-07-16）：
          * **真小车**：前端填种子（seed>0）→ ``(seed+offset)%N`` 确定选路，
            可复现；前端不填（seed==0）→ 随机，每次仿真不同。同次仿真内
            多个真小车走不同路线（实体数>路线数才回绕重复）。
          * **诱饵**：永远与种子无关 —— 用独立未种子化 RNG 随机选路，
            每次仿真不同，同次仿真内不同诱饵走不同路线。

        把路线 Start 写进 initial_*，引擎直接把车辆生成在路线起点，
        不需要 set_position 瞬移，也不会在可视化上留下瞬移的红色连线。
        选定的路线名记到 _route_assignment，inject_startup 按名取路。
        """
        import random as _random
        seed = resolve_scenario_seed(getattr(self.cfg, "seed", 0) or 0,
                                     getattr(self, "_scenario_cfg", None))
        self.cfg.seed = seed
        # 真小车 RNG：seed>0 种子化（确定），seed==0 未种子化（随机）。
        rng = _random.Random(seed) if seed > 0 else _random.Random()
        # 诱饵 RNG：永远未种子化，与种子无关，每次仿真随机。
        decoy_rng = _random.Random()

        repo_root = Path(__file__).resolve().parents[4]
        target_routes_path = str(repo_root / "config" / "points.json")
        decoy_routes_path = str(repo_root / "config" / "random_routes_20.json")

        ents = self._scenario_cfg.get("entities", [])
        n_targets = sum(1 for e in ents
                        if e.get("type") in ("TargetVehicle", "ground_vehicle"))
        n_decoys = sum(1 for e in ents if e.get("type") == "DecoyVehicle")
        # 真小车：seed 驱动（确定/随机）；诱饵：永远 seed=0 随机。
        target_routes = assign_routes(target_routes_path, n_targets,
                                      seed=seed, rng=rng)
        decoy_routes = assign_routes(decoy_routes_path, n_decoys,
                                     seed=0, rng=decoy_rng)

        target_idx = 0
        decoy_idx = 0
        for ent in ents:
            etype = ent.get("type")
            if etype in ("TargetVehicle", "ground_vehicle"):
                route = (target_routes[target_idx]
                         if target_idx < len(target_routes) else None)
                target_idx += 1
            elif etype == "DecoyVehicle":
                route = (decoy_routes[decoy_idx]
                         if decoy_idx < len(decoy_routes) else None)
                decoy_idx += 1
            else:
                continue
            if not route:
                continue
            wps = _build_waypoints(route)
            if not wps:
                continue
            start = wps[0]
            uid = str(ent.get("id") or ent.get("name") or "")
            ent.setdefault("params", {})
            ent["params"]["initial_latitude"] = start["lat"]
            ent["params"]["initial_longitude"] = start["lon"]
            ent["params"]["initial_altitude"] = 0.0
            # 清空预设 waypoints，避免可视化显示原来的红色路线
            traj = ent.get("components", {}).get("trajectory", {})
            traj.setdefault("params", {})["waypoints"] = []

            # 添加 astar_planner 组件，用于执行 astar_plan 命令
            ent.setdefault("components", {})["astar_planner"] = {
                "type": "AStarPlannerComponent",
                "enabled": True,
                "params": {
                    "grid_csv_path": "config/GridDataAll_18.csv"
                }
            }

            self._route_assignment[uid] = route.get("Name", "")
            self.log(f"[coop_decoy] 实体 {uid} ({etype}) 起点设为路线 "
                     f"'{route.get('Name', '')}' 的 Start "
                     f"({start['lat']:.6f}, {start['lon']:.6f})")

    def inject_startup(self, client, first: WorldState) -> None:
        """Activate each target and decoy via A* navigation (并发).

        真目标和诱饵都按 prepare_scenario 选定的路线（_route_assignment）
        注入 A* 分段导航。不同实体并发规划（实体内段间仍串行）以缩短启动耗时。
        """
        import os
        import random as _random
        seed = getattr(self.cfg, "seed", 0) or 0

        repo_root = Path(__file__).resolve().parents[4]
        target_routes_path = str(repo_root / "config" / "points.json")
        decoy_routes_path = str(repo_root / "config" / "random_routes_20.json")

        # routes_by_type: 按实体类型选路文件 (真小车 points.json, 诱饵 decoys)。
        routes_by_type = {
            "TargetVehicle": target_routes_path,
            "ground_vehicle": target_routes_path,
            "DecoyVehicle": decoy_routes_path,
        }
        entities = self._scenario_cfg.get("entities", [])
        # 合计所有可注入车辆数 → 进度回调的分母。批量版每车一次 astar_plan_batch,
        # 进度按"每完成一辆车 +1"上报, 故分母用车辆数而非段数。
        total_units = count_injectable_vehicles(entities, routes_by_type,
                                                self._route_assignment)
        # 构造线程安全的车辆级进度回调 (total_units<=0 时为 no-op)。
        progress_cb = make_route_progress_cb(client, total_units, log=self.log)

        def _inject_one(ent: dict) -> None:
            etype = ent.get("type")
            uid = str(ent.get("id") or ent.get("name") or "")
            route_name = self._route_assignment.get(uid)
            # 每实体独立 RNG: random.Random 非线程安全, 并发下避免共享状态竞态。
            # seed>0 时派生可复现 (uid 偏移); seed==0 时各自真随机。
            local_seed = (seed + hash(uid)) & 0xFFFFFFFF if seed > 0 else None
            local_rng = _random.Random(local_seed)
            if etype in ("TargetVehicle", "ground_vehicle"):
                inject_astar_target(
                    client=client, entity=ent,
                    routes_path=target_routes_path,
                    rng=local_rng, log=self.log,
                    route_name=route_name,
                    progress_cb=progress_cb,
                )
            elif etype == "DecoyVehicle":
                inject_astar_decoy(
                    client=client, entity=ent,
                    routes_path=decoy_routes_path,
                    rng=local_rng, log=self.log,
                    route_name=route_name,
                    progress_cb=progress_cb,
                )

        workers = int(os.environ.get("OPENSIM_INJECT_WORKERS", "8"))
        inject_startup_concurrent(
            client, self._scenario_cfg.get("entities", []),
            inject_fn=_inject_one, max_workers=workers, log=self.log)

        # 通知引擎基于注入后的真实路线重配静态 zone(详见 _astar_navigator
        # publish_regenerate_zones 文档)。无 generate 块时为 no-op。
        publish_regenerate_zones(client)

    def _load_scenario(self, path: str) -> dict:
        try:
            # utf-8-sig 容忍 UTF-8 BOM(见 search_track/runner.py 同名方法注释)。
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[{self.scenario_name}] WARNING: failed to load scenario "
                  f"{path}: {e!r}", file=sys.stderr, flush=True)
            return {}


def run(agent_cls, *, duration: float = 600.0,
        scenario: str | None = None, start_sim: bool = True,
        output_dir: str = "output", host: str = "127.0.0.1", port: int = 6379,
        dry_run: bool = False, quiet: bool = False,
        sim_binary: str | None = None, seed: int = 0,
        visualize: bool = False, viz_dir: str | None = None,
        open_browser: bool = True,
        mode: str = "train", photo_mode: str = "auto",
        accuracy: float = 0.85, noise_sigma_m: float = 50.0,
        max_detection_range_m: float | None = None,
        full_accuracy_range_m: float | None = None,
        yolo_model_path: str = "") -> dict:
    """Convenience entry point for players. ``seed`` (>0) randomizes the scene.

    spec 032 perception params (与赛题一 search_track.run() 完全一致):
      * ``mode`` — "train" (AccuracySimulator) | "eval" (YoloDetector)
      * ``photo_mode`` — 相机帧拉取模式：auto(默认)/on/off（见 ScenarioConfig）
      * ``accuracy`` / ``noise_sigma_m`` — AccuracySimulator params
      * ``max_detection_range_m`` / ``full_accuracy_range_m`` — 距离门限
        （None = 读 scenario.json perception 块；max 0 = 禁用，full 0 = 从 0 起衰减）
      * ``yolo_model_path`` — YOLO model path (eval mode)
    """
    from . import DEFAULT_SCENARIO_JSON
    scenario_path = scenario or DEFAULT_SCENARIO_JSON
    pr = read_perception_range(scenario_path)
    if max_detection_range_m is None:
        max_detection_range_m = pr["max_detection_range_m"]
    if full_accuracy_range_m is None:
        full_accuracy_range_m = pr["full_accuracy_range_m"]
    cfg = ScenarioConfig(
        scenario_name="coop_decoy",
        scenario_path=scenario_path,
        duration_s=duration,
        redis_host=host, redis_port=port,
        output_dir=output_dir, sim_binary=sim_binary,
        start_sim_flag=start_sim, dry_run=dry_run, quiet=quiet,
        seed=seed, visualize=visualize, viz_dir=viz_dir,
        open_browser=open_browser,
        run_mode=mode,
        photo_mode=photo_mode,
        accuracy=accuracy, noise_sigma_m=noise_sigma_m,
        yolo_model_path=yolo_model_path,
        weather=read_weather(scenario_path),
        max_detection_range_m=max_detection_range_m,
        full_accuracy_range_m=full_accuracy_range_m,
    )
    return CoopDecoyRunner(cfg, agent_cls).run()
