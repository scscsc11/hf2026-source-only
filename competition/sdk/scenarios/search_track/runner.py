"""Search-track scenario runner.

Wires the SearchTrackAgent into RunnerBase. Scenario-specific bits:
  * controllable_types = {"uav"} (single UAV)
  * briefing: fleet_size=1, mission_area from scenario.json
  * scoring: profile_uav_search_track_car (K=1, dwell=300s linear)
  * score_extras: search_time + track_in_view_fraction (stability dims)
  * inject_startup: activate the target vehicle via A* navigation
    (replaces original set_trajectory waypoint playback with A* path planning)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from ...core.observation import AreaSpec, MissionBriefing
from ...core.runner import RunnerBase, ScenarioConfig, read_weather, resolve_scenario_seed
from ...core.scoring import (
    ScoringProfile, profile_uav_search_track_car,
)
from ...core.world_state import WorldState
from .._astar_navigator import (
    inject_astar_target, assign_routes, _build_waypoints, inject_startup_concurrent,
    make_route_progress_cb, count_injectable_vehicles, publish_regenerate_zones,
)


class SearchTrackRunner(RunnerBase):
    scenario_name = "search_track"
    controllable_types = {"uav"}

    def __init__(self, cfg: ScenarioConfig, agent_cls, log=print) -> None:
        super().__init__(cfg, log)
        self.agent_cls = agent_cls
        self._scenario_cfg = self._load_scenario(cfg.scenario_path)
        # uid → 选定路线名（prepare_scenario 选路时记录，inject_startup 用）
        self._route_assignment: Dict[str, str] = {}

    # ── briefing ──────────────────────────────────────────────────────

    def build_briefing(self, world_state: WorldState,
                       entity_uid: str) -> MissionBriefing:
        area = self._mission_area()
        # C2/C3: 白名单 params，不转储整份 scenario；赛题一给目标初始位置
        target_pos = None
        target_speed = 8.0
        for ent in self._scenario_cfg.get("entities", []):
            if ent.get("type") in ("TargetVehicle", "ground_vehicle"):
                p = ent.get("params", {}) or {}
                lat = p.get("initial_latitude")
                lon = p.get("initial_longitude")
                if lat is not None and lon is not None:
                    target_pos = (float(lat), float(lon))
                tp = (ent.get("components", {}) or {}).get("trajectory", {}).get("params", {}) or {}
                target_speed = float(tp.get("speed", 8.0))
                break
        return MissionBriefing(
            self_uid=entity_uid,
            fleet_size=1,
            mission_area=area,
            known_threats=(),   # no threats in this scenario
            target_initial_pos=target_pos,
            params={"target_speed": target_speed},
        )

    def _mission_area(self) -> AreaSpec | None:
        sim = self._scenario_cfg.get("simulation", {})
        # scenario.json doesn't carry an explicit area; use a sensible
        # default around the UAV's home (27.0, 125.0).
        return AreaSpec(lat_min=26.98, lat_max=27.02,
                        lon_min=124.98, lon_max=125.02)

    # ── scoring ───────────────────────────────────────────────────────

    def build_scoring(self, world_state: WorldState
                      ) -> Tuple[ScoringProfile, set]:
        profile = profile_uav_search_track_car(
            duration_s=self.cfg.duration_s)
        true_targets = set(world_state.targets.keys())
        return profile, true_targets

    def score_extras(self, world_state: WorldState,
                     destroyed_uids: set) -> Dict[str, Any]:
        # Overhauled scoring: scenario 1's single "completion" dimension
        # (accumulated in-view time / duration) is computed entirely inside
        # the evaluator from the per-tick UAV→target map, so no extras are
        # needed. Return an empty dict.
        return {}

    # ── startup injection ─────────────────────────────────────────────

    def prepare_scenario(self) -> None:
        """引擎启动前：为真目标选路线，把路线 Start 写入 initial_*。

        真小车选路规则（需求 2026-07-16）：
          * 前端填了种子（seed>0）→ ``(seed+offset)%N`` 确定选路，可复现；
          * 前端不填（seed==0）→ 随机选路，每次仿真不同。
        同一次仿真内只有一个真目标，无需考虑多实体互不相同。
        把路线 Start 写进 initial_*，引擎直接把真目标生成在路线起点，
        不需要 set_position 瞬移，也不会在可视化上留下瞬移的红色连线。
        选定的路线名记到 _route_assignment，inject_startup 按名取路。
        """
        import random as _random
        seed = resolve_scenario_seed(getattr(self.cfg, "seed", 0) or 0,
                                     getattr(self, "_scenario_cfg", None))
        self.cfg.seed = seed
        rng = _random.Random(seed) if seed > 0 else _random.Random()

        repo_root = Path(__file__).resolve().parents[4]
        routes_path = str(repo_root / "config" / "points.json")

        # 真小车选路：seed>0 确定；seed==0 随机（每次仿真不同）。
        n_targets = sum(1 for e in self._scenario_cfg.get("entities", [])
                        if e.get("type") in ("TargetVehicle", "ground_vehicle"))
        target_routes = assign_routes(routes_path, n_targets,
                                       seed=seed, rng=rng)

        target_idx = 0
        for ent in self._scenario_cfg.get("entities", []):
            if ent.get("type") not in ("TargetVehicle", "ground_vehicle"):
                continue
            route = (target_routes[target_idx]
                     if target_idx < len(target_routes) else None)
            target_idx += 1
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
            self._route_assignment[uid] = route.get("Name", "")
            self.log(f"[search_track] 实体 {uid} 起点设为路线 "
                     f"'{route.get('Name', '')}' 的 Start "
                     f"({start['lat']:.6f}, {start['lon']:.6f})")
            break  # search_track 只有一个真目标

    def inject_startup(self, client, first: WorldState) -> None:
        """Activate the target vehicle via A* navigation (并发入口统一).

        真目标按 prepare_scenario 选定的路线（_route_assignment）注入 A*
        分段导航。search_track 仅 1 真目标, 并发收益小但入口统一 (DRY)。
        """
        import os
        import random as _random
        seed = getattr(self.cfg, "seed", 0) or 0

        # runner.py 在 competition/sdk/scenarios/search_track/ 下,
        # parents[4] = 仓库根
        repo_root = Path(__file__).resolve().parents[4]
        routes_path = str(repo_root / "config" / "points.json")

        # routes_by_type: search_track 只有真小车, 统一 points.json。
        routes_by_type = {
            "TargetVehicle": routes_path,
            "ground_vehicle": routes_path,
        }
        entities = self._scenario_cfg.get("entities", [])
        # 合计所有可注入车辆数 → 进度回调的分母。批量版每车一次 astar_plan_batch,
        # 进度按"每完成一辆车 +1"上报, 故分母用车辆数而非段数。
        total_units = count_injectable_vehicles(entities, routes_by_type,
                                                self._route_assignment)
        # 构造线程安全的车辆级进度回调 (total_units<=0 时为 no-op)。
        progress_cb = make_route_progress_cb(client, total_units, log=self.log)

        def _inject_one(ent: dict) -> None:
            if ent.get("type") in ("TargetVehicle", "ground_vehicle"):
                uid = str(ent.get("id") or ent.get("name") or "")
                route_name = self._route_assignment.get(uid)
                # 每实体独立 RNG: random.Random 非线程安全, 并发下避免共享状态竞态。
                local_seed = (seed + hash(uid)) & 0xFFFFFFFF if seed > 0 else None
                local_rng = _random.Random(local_seed)
                inject_astar_target(
                    client=client, entity=ent,
                    routes_path=routes_path,
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
        # NOTE: 以下 set_speed/set_trajectory 兜底是重构前的死代码——
        # wps / target_uid / speed 三个变量在新版 inject_astar_target 路径下
        # 已不再定义，执行到此必抛 NameError。注入逻辑现由上面的
        # inject_astar_target 全权负责，故注释禁用。若后续要恢复直发
        # waypoints 的降级兜底，需重新从 scenario.json 取 wps/speed/uid。
        # if wps:
        #     client.publish_raw({
        #         "unique_id": target_uid or "10001",
        #         "cmd": "set_speed", "params": {"speed": speed}})
        #     client.publish_raw({
        #         "unique_id": target_uid or "10001",
        #         "cmd": "set_trajectory", "params": {"waypoints": wps}})

    # ── helpers ───────────────────────────────────────────────────────

    def _load_scenario(self, path: str) -> dict:
        try:
            # utf-8-sig 容忍 UTF-8 BOM: scenario.json 会被 start.ps1/编辑器
            # 改写,PS 5.1 的 -Encoding UTF8 与部分 Windows 编辑器会写 BOM,
            # 而 json.loads 不容忍 BOM(抛 JSONDecodeError → 静默返回 {} →
            # 引擎报 "no entity" 启动失败)。
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[{self.scenario_name}] WARNING: failed to load scenario "
                  f"{path}: {e!r}", file=sys.stderr, flush=True)
            return {}


def run(agent_cls, *, duration: float = 600.0, scenario: str | None = None,
        start_sim: bool = True, output_dir: str = "output",
        host: str = "127.0.0.1", port: int = 6379, dry_run: bool = False,
        quiet: bool = False, sim_binary: str | None = None,
        seed: int = 0, visualize: bool = False, viz_dir: str | None = None,
        open_browser: bool = True,
        mode: str = "train", photo_mode: str = "auto",
        accuracy: float = 0.85, noise_sigma_m: float = 50.0,
        yolo_model_path: str = "") -> dict:
    """Convenience entry point for players.

    ``seed`` (>0) randomizes the scene (target route, and the UAV+target
    location together so their relative distance is preserved).
    ``visualize`` opens the 3D view (bystander; needs Node.js).

    spec 029 perception params (all default to train-mode baseline):
      * ``mode`` — "train" (AccuracySimulator) | "eval" (YoloDetector)
      * ``photo_mode`` — 相机帧拉取模式：auto(默认)/on/off（见 ScenarioConfig）
      * ``accuracy`` / ``noise_sigma_m`` — AccuracySimulator params
      * ``yolo_model_path`` — YOLO model path (eval mode)
    """
    from . import DEFAULT_SCENARIO_JSON
    cfg = ScenarioConfig(
        scenario_name="search_track",
        scenario_path=scenario or DEFAULT_SCENARIO_JSON,
        duration_s=duration,
        redis_host=host, redis_port=port,
        output_dir=output_dir,
        sim_binary=sim_binary,
        start_sim_flag=start_sim,
        dry_run=dry_run, quiet=quiet,
        seed=seed, visualize=visualize, viz_dir=viz_dir,
        open_browser=open_browser,
        run_mode=mode,
        photo_mode=photo_mode,
        accuracy=accuracy, noise_sigma_m=noise_sigma_m,
        yolo_model_path=yolo_model_path,
    )
    return SearchTrackRunner(cfg, agent_cls).run()
