"""RunnerBase — the platform runtime that drives the control loop.

Responsibilities (FR-008): start/stop the C++ engine, connect Redis,
parse state frames, build per-entity Observations (via isolation), call
each agent's decide() at a fixed rate, publish commands (forcing
unique_id), score, and write evaluation.json.

Players never touch this. Scenario runners subclass :class:`RunnerBase`
and override a small set of hooks (see contracts/extending.md §"新增赛题").
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type

from .agent import Agent
from .client import SimClient
from .commands import Command
from .isolation import _extract_truth as _isolation_extract_truth
from .isolation import build_obs as _build_isolated_obs
from .observation import Detection, MissionBriefing, Observation
from .world_state import WorldState

# Self-contained runtime helpers (vendored from examples/_common so the SDK
# does not depend on examples/ at release time).
from .._vendored.sim_runner import start_sim, stop_sim  # noqa: E402
from .._vendored.metrics_summary import write_json  # noqa: E402
from .._vendored.target_motion import predict_target_position  # noqa: E402


# ── pause 空转检测(纯判定逻辑,便于单元测试)──────────────────────────


def _should_idle(stall_count: int, sim_time: float, last_sim_time: float,
                 threshold: int = 3) -> tuple[bool, int]:
    """Pause 空转检测的纯判定逻辑。

    引擎被 pause 后停止推 sim:state,poll_latest 返回旧帧 → sim_time 冻结。
    连续 ``threshold`` 次相同 sim_time 判定为 paused,应降频 poll 并跳过
    decide/score。resume 后 sim_time 增长,stall_count 重置为 0。

    Args:
        stall_count: 进入本 tick 前已累积的连续停滞次数。
        sim_time: 本 tick poll 到的帧 sim_time。
        last_sim_time: 上一 tick 的帧 sim_time。
        threshold: 判定 paused 所需的连续停滞次数(默认 3)。

    Returns:
        (should_idle, new_stall_count):
        - should_idle — True 表示处于 paused 态,调用方应 sleep+continue。
        - new_stall_count — 更新后的连续停滞计数(供下一 tick 使用)。
    """
    if sim_time == last_sim_time:
        new_count = stall_count + 1
        return (new_count >= threshold, new_count)
    return (False, 0)


# ── scenario declaration ─────────────────────────────────────────────────


def resolve_scenario_seed(cli_seed: int,
                          scenario_cfg: Optional[dict] = None) -> int:
    """解析本局真小车选路种子。

    规则（按需求 2026-07-16）：
      * 前端/CLI ``--seed`` > 0 → 返回该值：真小车按
        ``(seed + offset) % N`` 确定选路，同种子可复现，同次仿真内
        不同车走不同路线（实体数 > 路线数才取模回绕）。
      * 前端不填（0）→ 返回 0：真小车**随机**选路，同次仿真内不同车
        仍互不相同，但每次仿真不同（不可复现）。

    **不再读 scenario.json 的 ``simulation.seed`` 做兜底** —— 前端不填
    即随机。``scenario_cfg`` 入参保留以兼容现有调用签名，但不再使用。

    诱饵与种子无关：诱饵永远用独立未种子化 RNG 随机选路（见各 runner
    的 ``prepare_scenario``），不受本函数返回值影响。
    """
    return int(cli_seed) if cli_seed and int(cli_seed) > 0 else 0


_PHOTO_MODES = ("auto", "on", "off")


def resolve_photo_mode(photo_mode) -> str:
    """校验 ``photo_mode`` 为合法的三态值（auto/on/off），原样返回。

    被三个场景 ``run()``、``ScenarioConfig.__post_init__`` 共用，确保非法值
    在配置入口（而非运行到一半）即被拒绝。
    """
    if photo_mode not in _PHOTO_MODES:
        raise ValueError(
            f"photo_mode must be one of {_PHOTO_MODES}, got {photo_mode!r}")
    return photo_mode


@dataclass
class ScenarioConfig:
    """Static, whole-run scenario parameters resolved at startup."""
    scenario_name: str
    scenario_path: str               # path to scenario.json
    duration_s: float
    control_rate_hz: float = 10.0
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    output_dir: str = "output"
    sim_binary: Optional[str] = None
    start_sim_flag: bool = False
    dry_run: bool = False
    quiet: bool = False
    seed: int = 0                # 0 = fall back to simulation.seed in JSON; >0 = seed-driven
    visualize: bool = False      # start the 3D visualization (bystander)
    viz_dir: Optional[str] = None
    open_browser: bool = True
    extra: dict = field(default_factory=dict)
    # spec 029: 感知层配置
    run_mode: str = "train"          # "train" | "eval"
    # 相机帧拉取模式（spec 029 + 相机流自动启用）：
    #   "auto"（默认）= 非 dry_run 时启动 PhotoCache；Redis 有帧则注入 obs.self.photo，
    #                  无帧安全降级为 None（不报错）。带 UE 的标准环境无需任何额外开关。
    #   "on"  = 显式要求相机（行为同 auto 的非 dry_run 分支；dry_run 仍不启动）
    #   "off" = 不启动 PhotoCache，obs.self.photo 恒 None
    photo_mode: str = "auto"
    accuracy: float = 0.85           # AccuracySimulator 检出概率（钳制上界 0.9）
    noise_sigma_m: float = 50.0      # AccuracySimulator 位置噪声（米，钳制下界 30）
    yolo_model_path: str = ""        # YoloDetector 模型路径（eval 模式）
    weather: str = "Clear_Skies"     # 场景天气（来自 scenario.json），影响 detection 衰减

    # 防真值泄漏钳制：accuracy 上界 0.9（杜绝 acc=1.0 退化等价真值），
    # noise_sigma_m 下界 30m（杜绝 noise=0 位置等于真值）。所有配置入口
    # （ScenarioConfig 构造 / CLI / bridge）构造后一律受此约束。
    ACCURACY_MAX = 0.9
    NOISE_SIGMA_MIN = 30.0
    _PHOTO_MODES = ("auto", "on", "off")

    def __post_init__(self) -> None:
        if self.accuracy > self.ACCURACY_MAX:
            self.accuracy = self.ACCURACY_MAX
        if self.noise_sigma_m < self.NOISE_SIGMA_MIN:
            self.noise_sigma_m = self.NOISE_SIGMA_MIN
        # 校验 photo_mode 合法取值（auto/on/off）。
        self.photo_mode = resolve_photo_mode(self.photo_mode)


def read_weather(scenario_path: Optional[str]) -> str:
    """从 scenario.json 顶层 ``weather.type`` 提取场景天气。

    返回渲染端 6 种天气枚举字符串之一；缺失/读取失败时兜底
    ``"Clear_Skies"``（晴天不衰减）。供各场景 ``run()`` 在构造
    :class:`ScenarioConfig` 前调用，把 bridge 写入的天气接入感知层。
    """
    if not scenario_path:
        return "Clear_Skies"
    try:
        import json as _json
        data = _json.loads(Path(scenario_path).read_text(encoding="utf-8-sig"))
        wtype = (data.get("weather") or {}).get("type")
        return str(wtype) if wtype else "Clear_Skies"
    except Exception:
        return "Clear_Skies"


class RunnerBase:
    """Base runtime. Scenario subclasses override the hooks below.

    Hooks a scenario MUST override:
      * ``scenario_name``        — registry key
      * ``controllable_types``   — which entity types the player controls
      * ``agent_cls`` / ``make_agent_for`` — the player's Agent class
      * ``build_briefing``       — per-entity static MissionBriefing
      * ``build_scoring``        — (ScoringProfile, true_target_uids)
      * ``score_extras``         — per-tick extras dict for the evaluator

    Hooks a scenario MAY override:
      * ``build_obs``            — default: isolated projection (recommended)
      * ``inject_startup``       — e.g. target trajectory activation
      * ``resolve_k``            — cooperative threshold (may be adaptive)
    """

    scenario_name: str = ""
    controllable_types: set = {"uav"}
    # Default agent class for the homogeneous case. Subclasses set this, OR
    # override make_agent_for for heterogeneous dispatch (reserved).
    agent_cls: Optional[Type[Agent]] = None

    def __init__(self, cfg: ScenarioConfig, log: Callable = print) -> None:
        if not self.scenario_name:
            raise ValueError("subclass must set scenario_name")
        self.cfg = cfg
        self.log = (lambda *a, **kw: None) if cfg.quiet else log

    def _build_perception(self, uids):
        """构建感知层组件（spec 029 + spec 032）。返回 (photo_cache, resolver)。

        - PhotoCache 在 photo_mode != "off" 且非 dry_run 时启动
          （dry_run 无 UE 渲染，一律不启动）
        - auto 模式：Redis 有帧则注入 obs.self.photo，无帧 get() 返回 None（安全降级）
        - 默认识别器：eval 模式 + yolo_model_path → YoloDetector(primary)
          + AccuracySimulator(fallback)；否则仅 AccuracySimulator
        - spec 032 渲染门控：eval 模式下，无 photo 的 UAV 自动降级到 fallback
        """
        from .perception import (
            AccuracySimulator, DetectionResolver, PhotoCache, YoloDetector,
        )
        photo_cache = None
        if self.cfg.photo_mode != "off" and not self.cfg.dry_run:
            import redis as redis_lib
            rc = redis_lib.Redis(host=self.cfg.redis_host,
                                 port=self.cfg.redis_port)
            photo_cache = PhotoCache(redis_client=rc, uids=uids)
            photo_cache.start()
        fallback_det = AccuracySimulator(
            accuracy=self.cfg.accuracy, noise_sigma_m=self.cfg.noise_sigma_m,
            weather=self.cfg.weather)
        # 默认识别器：train → AccuracySimulator；eval → YoloDetector + fallback
        if self.cfg.run_mode == "eval" and self.cfg.yolo_model_path:
            default_det = YoloDetector(
                model_path=self.cfg.yolo_model_path, uav_id=uids[0] if uids else "",
                redis_host=self.cfg.redis_host, redis_port=self.cfg.redis_port)
            default_det.start()
            resolver = DetectionResolver(
                default_detector=default_det, fallback_detector=fallback_det)
        else:
            resolver = DetectionResolver(default_detector=fallback_det)
        return photo_cache, resolver

    def _extract_truth(self, ws: WorldState, uid: str) -> Detection:
        """Extract this entity's own engine-geometric detection as an INTERNAL truth source.

        仅供 AccuracySimulator（经 resolver 内部通道）。绝不可进入选手可见的 obs。
        诱饵伪装（misid_flag → ground_vehicle）在此应用。
        """
        me = ws.entities.get(uid)
        if me is None:
            return Detection(detected=False, confidence=0.0)
        return _isolation_extract_truth(me)

    # ── hooks: agents ─────────────────────────────────────────────────

    def make_agent_for(self, entity_type: str, entity_uid: str,
                       world_state: WorldState) -> Agent:
        """Instantiate one agent for one controllable entity.

        Default (homogeneous): every controllable type uses ``agent_cls``.
        Override for heterogeneous dispatch by ``entity_type`` (reserved
        interface — see contracts/extending.md §4).
        """
        if self.agent_cls is None:
            raise ValueError("agent_cls not set (or override make_agent_for)")
        return self.agent_cls(my_uid=entity_uid)

    def agent_config(self) -> Any:
        """Static config dict passed to ``agent.configure()``. Default {}."""
        return {}

    # ── hooks: observation / briefing ─────────────────────────────────

    def build_briefing(self, world_state: WorldState,
                       entity_uid: str) -> MissionBriefing:
        """Build the static MissionBriefing for one entity. MUST override."""
        raise NotImplementedError

    def build_obs(self, world_state: WorldState, entity_uid: str,
                  briefing: MissionBriefing) -> Observation:
        """Project one agent's Observation. Default: isolated projection.

        Scenarios normally keep this default (it enforces isolation). A
        scenario may override to add THIS entity's own extra fields, but
        MUST NOT project other entities' truth.
        """
        return _build_isolated_obs(world_state, entity_uid, briefing)

    # ── hooks: scoring ────────────────────────────────────────────────

    def build_scoring(self, world_state: WorldState):
        """Return (ScoringProfile, set[str] true_target_uids). MUST override."""
        raise NotImplementedError

    def score_extras(self, world_state: WorldState,
                     destroyed_uids: set) -> Dict[str, Any]:
        """Per-tick extras for the evaluator (search_time, alive_rate, ...).

        Default: empty. Scenarios override to supply dimension inputs.
        """
        return {}

    # ── hooks: misc ───────────────────────────────────────────────────

    def inject_startup(self, client: SimClient,
                       first: WorldState) -> None:
        """Called once after the first frame (e.g. activate target routes).

        Default: nothing. Override for trajectory injection etc.
        """
        return None

    def resolve_k(self, world_state: WorldState) -> Optional[int]:
        """Cooperative threshold K, possibly adaptive. None = use profile's K."""
        return None

    def controllable_uids(self, world_state: WorldState) -> List[str]:
        """Uids of entities the player controls this tick."""
        return [uid for uid, e in world_state.entities.items()
                if e.kind in self.controllable_types
                and e.status == "active"]

    # ── main loop ─────────────────────────────────────────────────────

    def run(self) -> dict:
        """Run the full scenario. Returns the final evaluation dict."""
        from .scoring import CoopTrackingEvaluator
        from .._vendored.score_publisher import ScorePublisher

        cfg = self.cfg
        sim_proc = None
        score_pub = ScorePublisher(host=cfg.redis_host, port=cfg.redis_port,
                                   connect=not cfg.dry_run)
        photo_cache = None   # spec 029: 在 try 块开头初始化，便于 finally 安全清理
        resolver = None      # spec 029 C1: 同上，确保 detector 后台资源被释放
        viz_ctx = None
        halted_targets: set = set()
        try:
            # Optional 3D visualization (bystander; started before the engine
            # so it's ready to relay the first sim:state frame).
            if cfg.visualize and not cfg.dry_run:
                from ..visualize import VisualizationSession
                viz_ctx = VisualizationSession.start(
                    viz_dir=cfg.viz_dir, redis_host=cfg.redis_host,
                    redis_port=cfg.redis_port, open_browser=cfg.open_browser,
                    log=self.log)
            # spec 036: 在 spawn 子进程之前建立 Redis 连接并立即 publish 一帧
            # '准备启动' 进度 —— 防止前端 stall timer（5s）误报「卡死」。
            # 同时启动心跳线程,在后续所有等待期间（C++ 引擎读 786MB
            # terrain CSV → 订阅 sim:commands → 第一帧 sim:state）持续发心跳。
            client: Optional[SimClient] = None
            heartbeat_stop = threading.Event()
            heartbeat_thread: Optional[threading.Thread] = None
            if cfg.start_sim_flag and not cfg.dry_run:
                client = SimClient(host=cfg.redis_host, port=cfg.redis_port)
                client.connect()
                self._publish_progress(
                    client, phase="准备启动 C++ 引擎", pct=0.0,
                    detail="spawning opensim-sim"
                )
                heartbeat_thread = self._start_heartbeat(client, heartbeat_stop)

            if cfg.start_sim_flag:
                sim_proc = self._start_engine()
                # 036: _start_engine 在 sim 已订阅 sim:commands 时返回 —— 此时引擎
                # 已经能接收命令,即使第一帧 sim:state 还没来。把心跳停下来交给
                # wait_first_state 处理(它本身极短)。如果 start 失败,清心跳再返回。
                if heartbeat_thread is not None:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=2)
                    heartbeat_thread = None
                if sim_proc is None:
                    return {"error": "engine start failed"}

            if client is None and not cfg.dry_run:
                client = SimClient(host=cfg.redis_host, port=cfg.redis_port)
                client.connect()
            if client is not None and heartbeat_thread is None and not cfg.dry_run:
                # 等待第一帧期间重新启用心跳；必须 clear 之前 set 的 stop_event,
                # 否则新线程会立刻退出。
                heartbeat_stop.clear()
                heartbeat_thread = self._start_heartbeat(client, heartbeat_stop)

            if not cfg.dry_run:
                first = client.wait_first_state(timeout=120.0)
            else:
                first = self._synthetic_first_state()
            # 收到 sim:state 后停止心跳（下一步是 _build_perception → ready 帧 → inject_startup）
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
                heartbeat_thread = None
            # 给一个 phase 提示,告诉前端正在做什么
            self._publish_progress(
                client, phase="初始化仿真", pct=0.0, detail="感知层构建中"
            )
            self.log(f"[{self.scenario_name}] first frame @ sim_time={first.sim_time:.3f}")

            # startup injection (target trajectories, etc.)
            if not cfg.dry_run:
                self.inject_startup(client, first)

            # scoring setup
            profile, true_targets = self.build_scoring(first)
            if self.resolve_k(first) is not None:
                profile = _with_k(profile, self.resolve_k(first))  # type: ignore[arg-type]
            evaluator = CoopTrackingEvaluator(profile, true_targets)

            # build one agent instance per controllable entity
            agents: Dict[str, Agent] = {}
            briefings: Dict[str, MissionBriefing] = {}
            for uid in self.controllable_uids(first):
                a = self.make_agent_for(first.entities[uid].kind, uid, first)
                ac = self.agent_config()
                if hasattr(a, "configure"):
                    a.configure(ac)
                a.reset()
                agents[uid] = a
                briefings[uid] = self.build_briefing(first, uid)
            self.log(f"[{self.scenario_name}] {len(agents)} agent(s) instantiated")

            # spec 029: 构建感知层（PhotoCache + DetectionResolver）
            photo_cache, resolver = self._build_perception(list(agents.keys()))

            # 036: 向 bridge/前端发送最终就绪帧，进度条到达 100% 后进入主循环。
            if not cfg.dry_run and client._redis is not None:
                try:
                    import json as _json_progress
                    client._redis.publish(
                        'sim:progress',
                        _json_progress.dumps({
                            'type': 'load_progress',
                            'phase': '就绪',
                            'pct': 1.0,
                            'detail': '主循环开始',
                        })
                    )
                    self.log(f'[{self.scenario_name}] ready frame published')
                except Exception as e:
                    self.log(f'[{self.scenario_name}] ready frame publish failed: {e}')

            # 启动仿真 (kIdle → kRunning)。scenario.json 的 simulation.auto_start=false
            # 让引擎 init 后停在 kIdle: 整个启动序列 (terrain 加载、首帧、inject_startup
            # 的 A* 路线规划、评分/agent/感知层构建) 期间 sim_time 冻结、UAV/车辆静止,
            # 所以 agent 第一次 decide 时实体仍在 scenario 写定的 initial_lat/lon。
            # 直到所有准备完成、即将进入主循环的此刻才发 start, 引擎开始 tick。
            # 对 auto_start=true 的场景 (默认), 引擎本就在 kRunning, start 是 no-op。
            if not cfg.dry_run:
                try:
                    client.publish_engine("start")
                    self.log(f'[{self.scenario_name}] engine start command sent')
                except Exception as e:
                    self.log(f'[{self.scenario_name}] engine start failed: {e}')

            period = 1.0 / cfg.control_rate_hz
            sim_t0 = first.sim_time
            target_end = sim_t0 + cfg.duration_s if cfg.duration_s > 0 else None
            last = first
            # pause 空转检测:引擎被 pause 后停止推 sim:state,poll_latest 返回
            # 旧帧 → sim_time 不增长 → 这里检测到连续停滞后降频 poll,避免 CPU 忙等。
            # resume 后引擎恢复推帧,sim_time 增长,stall_count 重置。
            stall_count = 0
            STALL_POLL_INTERVAL = 0.5  # 空转时降频 poll(秒)
            score_timeline: List[dict] = []
            prev_snap: Optional[dict] = None   # C4: last tick's score snapshot

            while True:
                if not cfg.dry_run:
                    ws = client.poll_latest(timeout=0.01)
                    if ws is None:
                        ws = last
                else:
                    ws = self._advance_synthetic(last, period)

                # ── pause 空转检测(纯逻辑见 _should_idle)──
                if not cfg.dry_run and last is not None:
                    should_idle, stall_count = _should_idle(
                        stall_count, ws.sim_time, last.sim_time)
                    if should_idle:
                        time.sleep(STALL_POLL_INTERVAL)
                        last = ws
                        continue

                if cfg.duration_s > 0 and ws.sim_time >= (target_end or 1e18):
                    break
                if ws.status == "ended":
                    self.log(f"[{self.scenario_name}] status=ended; exiting")
                    break

                destroyed = {uid for uid, e in ws.entities.items()
                             if e.status == "destroyed"}

                # C4: inject the previous tick's real-time score into each
                # agent's briefing before decide(). Score is computed AFTER
                # decide (below), so the current tick can only see "score as
                # of last tick"; the first tick sees None (no score yet).
                # frozen MissionBriefing → dataclasses.replace per agent.
                if prev_snap is not None:
                    from .observation import ScoreView
                    sv = ScoreView(
                        total_score=float(prev_snap.get("total_score", 0.0)),
                        dimension_scores=tuple(
                            (k, float(v)) for k, v
                            in prev_snap.get("dimension_scores", {}).items()),
                        passed=bool(prev_snap.get("passed", False)),
                        n_destroyed=int(prev_snap.get("n_destroyed", 0)),
                        n_targets=int(prev_snap.get("n_targets", 0)),
                        # 引擎 sim_time 是绝对 epoch（默认基线 -28800）,消费方需要
                        # 「仿真开始后经过的秒数」,此处归一为相对值(>=0)。
                        sim_time=float(max(0.0, ws.sim_time - sim_t0)),
                    )
                    briefings = {
                        uid: dataclasses.replace(b, score_view=sv)
                        for uid, b in briefings.items()
                    }

                # per-entity decide (spec 029: sensor → resolve → decide)
                all_cmds: List[tuple] = []
                for uid, agent in agents.items():
                    if uid in destroyed:
                        continue  # self-termination: dead agents send nothing
                    obs_base = self.build_obs(ws, uid, briefings[uid])
                    # spec 029: 注入 photo
                    photo = photo_cache.get(uid) if photo_cache else None
                    obs_with_photo = dataclasses.replace(
                        obs_base,
                        self=dataclasses.replace(obs_base.self, photo=photo))
                    # 引擎几何真值源（内部通道，绝不放入 obs；obs.self.detection
                    # 是空占位）。仅默认识别器 AccuracySimulator 使用。
                    truth = self._extract_truth(ws, uid)
                    # spec 029: 识别层三态分发
                    detections = resolver.resolve(agent, obs_with_photo, period,
                                                  truth_source=truth)
                    primary = (detections[0] if detections
                               else Detection(detected=False, confidence=0.0))
                    # spec 029 §6: detections（复数，预留字段）仅多目标（YOLO
                    # 多 bbox）时填；单目标走 detection（单数），detections 保持
                    # 默认空 tuple（赛题二/三行为等价现状，spec 029 行为不变契约）。
                    multi = tuple(detections) if detections and len(detections) > 1 else ()
                    obs_prime = dataclasses.replace(
                        obs_with_photo,
                        self=dataclasses.replace(
                            obs_with_photo.self, detection=primary,
                            detections=multi))
                    try:
                        cmds = agent.decide(obs_prime, period) or []
                    except Exception as ex:  # noqa: BLE001
                        self.log(f"[{self.scenario_name}] {uid} decide() error: {ex}")
                        cmds = []
                    for c in cmds:
                        all_cmds.append((uid, c))

                if not cfg.dry_run:
                    for uid, c in all_cmds:
                        if c.verb == "agent.report":
                            continue  # judge-side signal; engine never sees it
                        client.publish(uid, c)

                # scoring (judge side; uses full truth via ws)
                self._observe_scoring(evaluator, ws, sim_t0, destroyed, all_cmds)
                self._halt_destroyed_targets(
                    client if not cfg.dry_run else None,
                    ws, evaluator, halted_targets, period)
                extras = self.score_extras(ws, destroyed)
                snap = evaluator.score(extras)
                prev_snap = snap   # C4: carry full snapshot for next tick's briefing
                # 引擎 sim_time 是绝对 epoch(默认基线 -28800,见 time_util.cc);
                # 前端/timeline/agent 都期望「仿真开始后经过的秒数」,故减去首帧
                # 基线 sim_t0 后发布。注意:evaluator.observe 仍接收原始 sim_time,
                # 因为评分只依赖相邻帧差值(不受基线影响)。
                rel_sim_time = float(max(0.0, ws.sim_time - sim_t0))
                score_timeline.append({
                    "sim_time": rel_sim_time,
                    "total_score": snap["total_score"],
                    "completion_rate": snap["completion_rate"],
                })
                score_pub.publish(snap, sim_time=rel_sim_time,
                                  tick=evaluator.tick_count)

                last = ws
                # wall-clock pacing: run the agent/control loop at
                # control_rate_hz while the engine may publish sim:state at a
                # higher tick rate (typically 60 Hz). The old formula divided
                # sim-time delta by control_rate_hz, so once the state stream
                # was live slack was almost always <= 0 and this loop busy-ran.
                time.sleep(period)

            # finalize
            extras = self.score_extras(last, {uid for uid, e in last.entities.items()
                                              if e.status == "destroyed"})
            evaluation = evaluator.score(extras)
            evaluation["score_timeline"] = score_timeline
            evaluation["scenario"] = self.scenario_name
            run_id = f"{self.scenario_name}_{int(time.time())}"
            eval_path = write_json(evaluation, cfg.output_dir,
                                   f"{run_id}.evaluation.json")
            score_pub.publish_final(evaluation,
                                    sim_time=float(max(0.0, last.sim_time - sim_t0)),
                                    tick=evaluator.tick_count,
                                    evaluation_path=str(eval_path))
            self._banner(evaluation, eval_path)
            return evaluation

        finally:
            if viz_ctx is not None:
                viz_ctx.stop()
            if photo_cache is not None:
                photo_cache.stop()
            if resolver is not None:
                resolver.stop()   # spec 029 C1: 释放 YoloDetector 后台 worker
            if not cfg.dry_run:
                stop_sim(sim_proc)
            score_pub.close()

    # ── scoring helper (judge side; projects full truth for evaluator) ─

    def _observe_scoring(self, evaluator, ws: WorldState, sim_t0: float,
                         destroyed: set, all_cmds: List[tuple] = ()) -> None:
        """Feed the evaluator one tick of UAV→target resolution + reports.

        Uses the engine-published detection (which includes misid) plus
        full truth for nearest-neighbour matching — this is the judge's
        data flow, separate from the player's isolated Observation. Also
        extracts any ``agent.report`` commands emitted this tick and feeds
        them to the evaluator's targeting-accuracy accumulator.
        """
        from .._vendored.uav_target_map import (  # type: ignore
            UavDetection, resolve_uav_to_target,
        )
        uavs: List[UavDetection] = []
        for uid, e in ws.uavs.items():
            gim = e.raw.get("gimbal_tracking", {}) or {}
            det = gim.get("detection", {}) or {}
            tpos = det.get("target_position")
            uavs.append(UavDetection(
                uid=uid,
                detected=bool(det.get("detected", False)),
                target_lat=float(tpos.get("latitude")) if tpos else None,
                target_lon=float(tpos.get("longitude")) if tpos else None,
                target_type=str(det.get("target_type", "")),
                misid_flag=bool(det.get("misid_flag", False)),
                destroyed=(e.status == "destroyed"),
                confidence=float(det.get("confidence", 0.0)),
            ))
        true_targets = {uid: (e.lat, e.lon) for uid, e in ws.targets.items()}
        decoys = {uid: (e.lat, e.lon) for uid, e in ws.decoys.items()}
        uav_map = resolve_uav_to_target(uavs, true_targets, decoys)
        uav_positions = {uid: (e.lat, e.lon) for uid, e in ws.uavs.items()
                         if e.status == "active"}
        evaluator.observe(ws.sim_time, uav_map, destroyed,
                          uav_positions=uav_positions)

        # ── collect player targeting reports emitted this tick ───────────
        # ``all_cmds`` is a list of (uav_uid, Command). A report is a
        # judge-side signal (verb ``agent.report``); the engine never sees
        # it. We match each report to the nearest LIVE true target.
        # spec 032: 每帧每 UAV 仅取最后一条 report（防多报），其余丢弃。
        destroyed_true = {t for t, ts in evaluator.states.items() if ts.destroyed}
        latest_report: dict = {}
        for _uid, c in all_cmds:
            if c.verb == "agent.report":
                if _uid in latest_report:
                    self.log(f"[{self.scenario_name}] {_uid} multiple reports "
                             f"in one tick, keeping last (spec 032)")
                latest_report[_uid] = c
        for _uid, c in latest_report.items():
            evaluator.record_report(
                float(c.params["lat"]), float(c.params["lon"]),
                c.params.get("target_id"),
                true_targets, destroyed_true, ws.sim_time)

    # ── spec 036: progress heartbeat during engine boot ────────────────
    # C++ 引擎启动 ~20s 全用于加载 786MB terrain CSV,期间不会 publish
    # 任何 sim:progress 帧。前端 stall timer 在 5s 无消息时会显示
    # "加载可能卡住"——为消除这一误报,controller 在建立 Redis 连接后即
    # publish 一帧「准备启动」,并起后台心跳线程每 1.5s 推送「等待引擎
    # 就绪」直到收到第一帧 sim:state 后停掉。心跳 pct 从 0.02 缓慢递增到
    # 0.04 —— 总在 [0, 0.85×0.6=0.51] 范围内,不会冲掉真实进度(后到的
    # 真实百分比由高水位 Math.max 自动覆盖)。
    _PROGRESS_PHASE_BOOT = "等待引擎就绪"  # class-level 常量便于可能的测试引用

    def _publish_progress(self, client: "SimClient", phase: str,
                          pct: float, detail: str = "") -> None:
        """向 sim:progress 频道 publish 一帧。client 必须已 connect。"""
        if client is None or client._redis is None:
            return
        try:
            payload = {"type": "load_progress", "phase": phase,
                       "pct": round(float(pct), 4), "detail": detail}
            client._redis.publish("sim:progress", json.dumps(payload))
            self.log(f"[{self.scenario_name}] progress {phase}: {pct*100:.1f}%  ({detail})")
        except Exception as e:
            self.log(f"[{self.scenario_name}] progress publish failed: {e}")

    def _start_heartbeat(self, client: "SimClient",
                         stop_event: threading.Event) -> threading.Thread:
        """启动后台心跳线程,直到 stop_event 触发或发送 30 帧后停止。

        关键设计: 心跳 **pct=0**(不前进) 持续告诉前端"我还活着",
        因为前端用的是 Math.max 高水位。任何 pct>0 的心跳都会把进度条
        卡在心跳值,直到真实进度越过它 —— 这会让用户在 boot 阶段看到
        个假的 2.x% 然后从 0.1% 重新开始(或被高水位卡住)。
        所以 heartbeat 只发 phase + detail,pct=0。
        """
        def _run() -> None:
            t0 = time.time()
            i = 0
            while not stop_event.is_set() and i < 30:
                # phase 文案含秒数,让用户能确定性看到进展
                elapsed = int(time.time() - t0)
                self._publish_progress(
                    client,
                    phase=self._PROGRESS_PHASE_BOOT,
                    pct=0.0,
                    detail=f"已等 {elapsed}s，引擎正在加载 terrain CSV (786MB)",
                )
                if stop_event.wait(1.5):
                    return
                i += 1
        t = threading.Thread(target=_run, name=f"heartbeat-{self.scenario_name}",
                             daemon=True)
        t.start()
        return t

    # ── spec 030: halt destroyed targets at predicted position ────────
    def _halt_destroyed_targets(self, client, ws, evaluator, halted: set,
                                period: float) -> None:
        """Freeze newly-destroyed ground targets via set_position (C++ zero-change).

        For each target the evaluator just marked destroyed (and not yet
        processed this session), send a ``set_position`` to the position the
        target WILL occupy when the command takes effect (one control period
        ahead), so it freezes in place without jitter. Uses publish_raw to
        address the target entity directly (publish() rewrites uid to the
        agent's own). No-op when client is None (dry_run).
        """
        if client is None:       # dry_run: not connected
            return
        # Only halt in scenarios with a real kill/strike dimension. In
        # search_track, dwell_target_s=0.0 marks targets "destroyed" on first
        # detection (meaning "tracking complete", not "killed by strike");
        # halting there would freeze the moving car and break the scenario.
        if "kill" not in (evaluator.profile.weights or {}):
            return
        for uid, ts in evaluator.states.items():
            if not ts.destroyed or uid in halted:
                continue
            truth = ws.targets.get(uid)
            if truth is None:
                continue
            lat, lon = predict_target_position(truth, period)
            client.publish_raw({
                "unique_id": uid,
                "cmd": "set_position",
                "params": {"latitude": lat, "longitude": lon},
            })
            self.log(f"[{self.scenario_name}] target {uid} destroyed → "
                     f"halted at ({lat:.6f}, {lon:.6f})")
            halted.add(uid)

    # ── engine / synthetic helpers ────────────────────────────────────

    def _start_engine(self):
        # 真小车选路种子：仅来自前端/CLI --seed（>0）；前端不填则为 0
        # （随机选路）。不再读 scenario.json 的 simulation.seed。
        # 同 seed → 同真小车路线集合（pick_route_by_seed 取模）；诱饵不受
        # 影响（独立未种子化 RNG，见各 runner 的 prepare_scenario）。
        scenario_cfg = getattr(self, "_scenario_cfg", None)
        self.cfg.seed = resolve_scenario_seed(self.cfg.seed, scenario_cfg)

        # Materialize a randomized scenario if a seed is set (training variety).
        scenario_path = self.cfg.scenario_path
        if self.cfg.seed:
            from .scenario_randomizer import randomize_scenario
            scenario_path = randomize_scenario(
                self.cfg.scenario_path, self.cfg.seed,
                self.scenario_name, out_dir=self.cfg.output_dir)
            self.log(f"[{self.scenario_name}] randomized scene seed={self.cfg.seed} "
                     f"→ {scenario_path}")
            # reload the (possibly randomized) config so briefing/trajectory
            # injection reflect the new scene. Preserve simulation.seed so
            # prepare_scenario / inject_startup keep the same route seed.
            try:
                import json as _json
                self._scenario_cfg = _json.loads(
                    Path(scenario_path).read_text(encoding="utf-8-sig"))
                sim = self._scenario_cfg.setdefault("simulation", {})
                sim["seed"] = int(self.cfg.seed)
            except Exception as e:
                # 不静默: scenario 读错会让 briefing/trajectory 注入用错配置,
                # 且 seed 没设会导致随机性失控。打印警告便于排障。
                import sys as _sys
                print(f"[runner] WARNING: 重载 {scenario_path} 失败: {e!r}",
                      file=_sys.stderr, flush=True)

        # Let the scenario subclass mutate self._scenario_cfg before the
        # engine reads it (e.g. pick random routes for targets and overwrite
        # their initial_lat/lon to match the route Start, so no set_position
        # teleport is needed). The mutated config is written to a copy under
        # output_dir so the original scenario.json is never polluted.
        try:
            self.prepare_scenario()
            import json as _json2
            out = Path(self.cfg.output_dir) / f"scenario_{self.scenario_name}_prepared.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_json2.dumps(self._scenario_cfg, indent=2),
                           encoding="utf-8")
            scenario_path = str(out)
            self.log(f"[{self.scenario_name}] prepared scenario → {scenario_path}")
        except Exception as _e:
            self.log(f"[{self.scenario_name}] prepare_scenario failed: {_e}")

        # Engine binary resolution (release layout): OPENSIM_SIM_BIN env >
        # --sim-binary > opensim-sim(.exe) in the release root (the dir that
        # contains competition/) > repo build/ dir (dev convenience).
        # runner.py is at <root>/competition/sdk/core/, so parents[3] == root.
        root = Path(__file__).resolve().parents[3]
        exe = "opensim-sim.exe" if sys.platform == "win32" else "opensim-sim"
        sibling = root / exe                       # release: beside competition/
        repo_build = root / "build" / exe           # dev: repo build/ dir
        # MSVC multi-config generator puts the binary under build/Release/.
        repo_build_release = root / "build" / "Release" / exe
        binary = (self.cfg.sim_binary
                  or os.environ.get("OPENSIM_SIM_BIN")
                  or (str(sibling) if sibling.exists()
                      else str(repo_build_release) if repo_build_release.exists()
                      else str(repo_build)))
        return start_sim(
            binary, scenario_path, log=self.log,
            redis_host=self.cfg.redis_host, redis_port=self.cfg.redis_port,
            stderr_file=os.environ.get("OPENSIM_SIM_STDERR"))

    def _synthetic_first_state(self) -> WorldState:
        """Minimal fake frame for --dry-run. Override for richer fakes."""
        from .world_state import EntityTruth
        ws = WorldState(sim_time=0.0, status="running")
        ws.entities["uav_1"] = EntityTruth(
            uid="uav_1", kind="uav", name="uav_1",
            lat=27.0, lon=125.0, alt=300.0, heading=0.0, speed=20.0,
            status="active",
            raw={"gimbal_tracking": {"detection": {"detected": False}}},
        )
        return ws

    def _advance_synthetic(self, ws: WorldState, dt: float) -> WorldState:
        ws.sim_time += dt
        return ws

    def _banner(self, evaluation: dict, eval_path: Path) -> None:
        self.log("")
        self.log("=" * 60)
        self.log(f"{self.scenario_name.upper()} COMPLETE")
        self.log(f"  total score : {evaluation['total_score']:.1f} / 100  "
                 f"(passed={evaluation['passed']})")
        # completion_rate is None in scenario-1 accuracy mode (no completion
        # concept, spec 2026-07-15); guard the .2f format to avoid TypeError.
        rate = evaluation['completion_rate']
        rate_str = f"{rate:.2f}" if rate is not None else "N/A"
        self.log(f"  completion  : {evaluation['n_destroyed']}/"
                 f"{evaluation['n_targets']}  "
                 f"(rate={rate_str})")
        self.log(f"  eval json   : {eval_path}")
        self.log("=" * 60)


def _with_k(profile, k: int):
    """Return a copy of ``profile`` with K overridden (frozen dataclass)."""
    from dataclasses import replace
    return replace(profile, K=k)
