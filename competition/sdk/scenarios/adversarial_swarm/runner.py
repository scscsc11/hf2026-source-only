"""Adversarial-swarm scenario runner.

Scenario-specific bits:
  * controllable_types = {"uav"} (10 UAVs)
  * briefing (C3 exposure rules): 精确多边形不再暴露 (known_threats=())；
    静态预知区以近似 bbox+面积形式进 ``approximate_zones``（外扩20%）；
    动态干扰区 (comm_jam_random) 的统计参数 (max_count/radius/lifetime)
    进 ``params``，但位置绝不暴露。玩家通过 obs.self.jammed 感知动态干扰。
  * briefing.target_count: 目标数量（不给位置）
  * scoring: profile_adversarial_swarm_search (completion/track_quality/alive)
  * score_extras: alive_rate
  * resolve_k: fixed K=1 (single-UAV continuous tracking suffices)
  * self-termination: handled in RunnerBase (destroyed agents send nothing)
  * inject_startup: activate each target via A* navigation
    (replaces original set_trajectory waypoint playback with A* path planning)
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from ...core.observation import (
    AreaSpec,
    ApproxZoneSpec,
    MissionBriefing,
)
from ...core.runner import (
    RunnerBase, ScenarioConfig, read_perception_range, read_weather,
    resolve_scenario_seed,
)
from ...core.scoring import ScoringProfile, profile_adversarial_swarm_search
from ...core.world_state import WorldState

# 赛题三评分规则常量：≥ DEFAULT_K 架 UAV 同时盯防同一真目标满 20s 才摧毁。
# 举办方可调此常量（改这一处即可），选手不可通过 CLI/SDK 修改。
DEFAULT_K = 3
from .._astar_navigator import (
    inject_astar_target, inject_astar_decoy, assign_routes, _build_waypoints,
    inject_startup_concurrent,
    make_route_progress_cb, count_injectable_vehicles,
    publish_regenerate_zones,
)


# zone kinds that are static and pre-match-known → allowed in briefing
_STATIC_ZONE_KINDS = {"air_defense", "comm_jam_static", "no_fly"}

# 协同阈值 K(赛题三评分规则): 摧毁一个真目标需 ≥K 架 UAV 同时有效盯防。
# 锁定不可由命令行覆盖(resolve_k 返回 None, honour profile 的 K 作为唯一来源);
# 见 competition/tests/test_scoring.py:test_adversarial_swarm_runner_k_is_three_in_profile。
DEFAULT_K = 3


def _to_approx_zone(z) -> ApproxZoneSpec:
    """精确多边形 → 近似 bbox（外扩 20%）+ 面积。"""
    poly = z.polygon
    if not poly:
        return ApproxZoneSpec(kind=z.kind, bbox=((0.0, 0.0), (0.0, 0.0)),
                              area_m2=0.0, alt_min=z.alt_min, alt_max=z.alt_max)
    lats = [p[0] for p in poly]
    lons = [p[1] for p in poly]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    # 外扩 20%
    dlat = (lat_max - lat_min) * 0.2
    dlon = (lon_max - lon_min) * 0.2
    bbox = ((lat_min - dlat, lon_min - dlon), (lat_max + dlat, lon_max + dlon))
    area = _polygon_area_m2(poly)
    return ApproxZoneSpec(kind=z.kind, bbox=bbox, area_m2=area,
                          alt_min=z.alt_min, alt_max=z.alt_max)


def _polygon_area_m2(poly) -> float:
    """bbox 面积近似（米²），ref_lat 取中点。"""
    if not poly:
        return 0.0
    lats = [p[0] for p in poly]
    lons = [p[1] for p in poly]
    ref_lat = sum(lats) / len(lats)
    dlat = max(lats) - min(lats)
    dlon = max(lons) - min(lons)
    return (dlat * 111320.0 * dlon * 111320.0
            * max(0.1, math.cos(math.radians(ref_lat))))


class AdversarialSwarmRunner(RunnerBase):
    scenario_name = "adversarial_swarm"
    controllable_types = {"uav"}

    def __init__(self, cfg: ScenarioConfig, agent_cls, log=print) -> None:
        super().__init__(cfg, log)
        self.agent_cls = agent_cls
        self._scenario_cfg = self._load_scenario(cfg.scenario_path)
        self._initial_alive: int | None = None
        self._approx_zones: Tuple[ApproxZoneSpec, ...] | None = None
        # _approximate_zones_cache 的签名缓存:zones 不变→命中,变了→刷新。
        # 防止 briefing 永久冻结在 init() 阶段的临时 air_defense 上。
        self._approx_zones_sig: str | None = None
        # uid → 选定路线名（prepare_scenario 选路时记录，inject_startup 用）
        self._route_assignment: Dict[str, str] = {}

    # ── briefing ──────────────────────────────────────────────────────

    def build_briefing(self, world_state: WorldState,
                       entity_uid: str) -> MissionBriefing:
        return MissionBriefing(
            self_uid=entity_uid,
            fleet_size=len(world_state.uavs),
            mission_area=AreaSpec(lat_min=26.95, lat_max=27.05,
                                  lon_min=124.95, lon_max=125.05),
            known_threats=(),   # 精确多边形不再暴露（C3 改用 approximate_zones）
            target_count=len(world_state.targets),
            approximate_zones=self._approximate_zones_cache(world_state),
            params=self._curated_params(),
        )

    def _curated_params(self) -> dict:
        """白名单 params：只放非真值、非精确多边形的参数。"""
        p = {
            "fleet_size": len(self._scenario_cfg.get("entities", [])),
            "mission_area": {"lat_min": 26.95, "lat_max": 27.05,
                             "lon_min": 124.95, "lon_max": 125.05},
        }
        # 动态干扰区统计参数（不含位置）
        for z in self._scenario_cfg.get("zones", []):
            if z.get("type") == "comm_jam_random":
                p["comm_jam_random"] = {
                    "max_count": z.get("max_count"),
                    "radius_m": z.get("radius_m"),
                    "lifetime_s": z.get("lifetime_s"),
                    "spawn_interval_s": z.get("spawn_interval_s"),
                }
                break
        return p

    def _approximate_zones_cache(self, ws: WorldState) -> Tuple[ApproxZoneSpec, ...]:
        """精确多边形→近似 bbox+面积，外扩20%。动态干扰区不进。

        缓存语义:按 ws.zones 的"签名"缓存。同一份 zones 不重算(避免
        per-tick 重复构建);zones 变化时刷新。这覆盖 regenerate_zones 场景:
        引擎 init() 阶段用空 routes 生成临时 air_defense(coverage=random
        退化),runner 注入 A* 路线后发 regenerate_zones,引擎 deferred
        重建出最终 polygon。若首次缓存后永久冻结,选手 briefing 会一直
        暴露临时 polygon 的近似 bbox(与实际杀伤区中心可差 ~1 km),
        导致选手按错误位置避障。按签名缓存:zones 不变→命中(零开销),
        zones 变了→刷新(整局通常只变 1 次)。
        """
        sig = self._zones_signature(ws)
        if self._approx_zones is not None and sig == self._approx_zones_sig:
            return self._approx_zones
        out = []
        for z in ws.zones:
            if z.kind in _STATIC_ZONE_KINDS and not z.is_dynamic:
                out.append(_to_approx_zone(z))
        self._approx_zones = tuple(out)
        self._approx_zones_sig = sig
        return self._approx_zones

    @staticmethod
    def _zones_signature(ws: WorldState) -> str:
        """Stable signature of the static zones in ws. Two ws with the same
        static-zone polygons + alt bands produce the same signature, so the
        cache hits; any change (e.g. regenerate_zones) invalidates it."""
        parts = []
        for z in ws.zones:
            if z.kind in _STATIC_ZONE_KINDS and not z.is_dynamic:
                # polygon + alt band fully determine the approx zone output.
                poly = tuple((round(p[0], 9), round(p[1], 9)) for p in z.polygon)
                parts.append((z.kind, poly, z.alt_min, z.alt_max))
        return repr(sorted(parts, key=str))

    # ── scoring ───────────────────────────────────────────────────────

    def build_scoring(self, world_state: WorldState
                      ) -> Tuple[ScoringProfile, set]:
        # initial fleet size (score_extras 用)；build_scoring 只调一次，在此固定。
        if self._initial_alive is None:
            self._initial_alive = len(world_state.alive_uavs)
        profile = profile_adversarial_swarm_search(
            duration_s=self.cfg.duration_s, K=DEFAULT_K)
        true_targets = set(world_state.targets.keys())
        return profile, true_targets

    def score_extras(self, world_state: WorldState,
                     destroyed_uids: set) -> Dict[str, Any]:
        alive = len(world_state.alive_uavs)
        total = self._initial_alive or max(1, len(world_state.uavs))
        return {"alive_rate": alive / max(1, total)}

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
            self._route_assignment[uid] = route.get("Name", "")
            self.log(f"[adversarial_swarm] 实体 {uid} ({etype}) 起点设为路线 "
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

        # runner.py 在 competition/sdk/scenarios/adversarial_swarm/ 下,
        # parents[4] = 仓库根
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

        # 所有靶标车/诱饵的 set_trajectory 已下发后,通知引擎基于注入后的真实
        # 路线重配静态 zone(击毁区/静态干扰区)。init() 阶段生成的 zone 拿到的
        # 是空 routes(prepare_scenario 已清空预设 waypoints),退化为 random;
        # 此处让 zone 真正落在目标必经路线上。引擎在 sim 线程 deferred 执行,
        # 与 tick 串行,安全。无 generate 块时为 no-op。
        publish_regenerate_zones(client)

    def _load_scenario(self, path: str) -> dict:
        try:
            # utf-8-sig 容忍 UTF-8 BOM(见 search_track/runner.py 同名方法注释)。
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
        max_detection_range_m: float | None = None,
        full_accuracy_range_m: float | None = None,
        yolo_model_path: str = "") -> dict:
    """Convenience entry point. ``seed`` (>0) randomizes the scene + zones.

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
        scenario_name="adversarial_swarm",
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
    return AdversarialSwarmRunner(cfg, agent_cls).run()
