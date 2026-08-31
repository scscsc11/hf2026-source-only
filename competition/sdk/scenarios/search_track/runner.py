"""Search-track scenario runner.

Wires the SearchTrackAgent into RunnerBase. Scenario-specific bits:
  * controllable_types = {"uav"} (single UAV)
  * briefing: fleet_size=1, mission_area from scenario.json
  * scoring: profile_uav_search_track_car (K=1, dwell=300s linear)
  * score_extras: search_time + track_in_view_fraction (stability dims)

Spec 037 (engine-side route spawn)：真目标选路与 UAV 出生锚定已迁入 C++ 引擎
（scenario.json 声明 engine_route_spawn + spawn_anchor，引擎按 simulation.seed
选路并锚定）。本 runner 不再做任何出生位置决策——只保留观测/评分/Agent 调用。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from ...core.observation import AreaSpec, MissionBriefing
from ...core.runner import (
    RunnerBase, ScenarioConfig, read_perception_range, read_weather,
    resolve_scenario_seed,
)
from ...core.scoring import (
    ScoringProfile, profile_uav_search_track_car,
)
from ...core.world_state import WorldState


class SearchTrackRunner(RunnerBase):
    scenario_name = "search_track"
    controllable_types = {"uav"}

    def __init__(self, cfg: ScenarioConfig, agent_cls, log=print) -> None:
        super().__init__(cfg, log)
        self.agent_cls = agent_cls
        self._scenario_cfg = self._load_scenario(cfg.scenario_path)

    # ── briefing ──────────────────────────────────────────────────────

    def build_briefing(self, world_state: WorldState,
                       entity_uid: str) -> MissionBriefing:
        area = self._mission_area()
        # Spec 037：出生由引擎决定，scenario.json 静态 initial_* 已不代表
        # 实际出生点。目标初始位置改读首帧真值（引擎选路后在 kIdle 也会
        # 发布状态，world_state.targets 即路线起点）。
        target_pos = None
        target_speed = 8.0
        for tgt in world_state.targets.values():
            target_pos = (tgt.lat, tgt.lon)
            break
        for ent in self._scenario_cfg.get("entities", []):
            if ent.get("type") in ("TargetVehicle", "ground_vehicle"):
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
        """Spec 037：出生决策已迁引擎，这里只解析并固化 seed。

        seed 语义不变：前端/CLI --seed > 0 → 可复现（引擎按 (seed+0)%N 选路
        + splitmix64 锚定）；seed == 0 → 每局真随机。resolve 后写回 cfg.seed，
        供 core/runner 的 randomize_scenario 与引擎 simulation.seed 使用。
        """
        seed = resolve_scenario_seed(getattr(self.cfg, "seed", 0) or 0,
                                     getattr(self, "_scenario_cfg", None))
        self.cfg.seed = seed
        self.log(f"[search_track] seed={seed}；选路与 UAV 锚定由引擎完成"
                 f"（engine_route_spawn）")

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
        max_detection_range_m: float | None = None,
        full_accuracy_range_m: float | None = None,
        yolo_model_path: str = "") -> dict:
    """Convenience entry point for players.

    ``seed`` (>0) randomizes the scene (target route, and the UAV+target
    location together so their relative distance is preserved).
    ``visualize`` opens the 3D view (bystander; needs Node.js).

    spec 029 perception params (all default to train-mode baseline):
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
        scenario_name="search_track",
        scenario_path=scenario_path,
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
        weather=read_weather(scenario_path),
        max_detection_range_m=max_detection_range_m,
        full_accuracy_range_m=full_accuracy_range_m,
    )
    return SearchTrackRunner(cfg, agent_cls).run()
