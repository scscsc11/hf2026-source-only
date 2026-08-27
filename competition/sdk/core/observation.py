"""Observation data model — what a player agent sees each decide() call.

This module defines the **player-visible** data structures. Per the
isolation contract (contracts/isolation.md), an Observation only ever
contains:

  * ``self``        — the agent's own physically-perceivable state (SelfView)
  * ``comm_inbox``  — messages teammates sent to this agent
  * ``briefing``    — pre-match static mission info (constant for the whole run)

No other entity's pose/truth/detection is ever projected into an
Observation. This is enforced at construction time in
:mod:`competition.sdk.core.isolation`, not by access control.

Stability promise (contracts/extending.md §"core 稳定性承诺"):
  * The three top-level fields of ``Observation`` (self/comm_inbox/briefing)
    will never be added to or removed from.
  * ``MissionBriefing`` fields are append-only and every new field MUST
    carry a default value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple


# ── shared geo / spec primitives ──────────────────────────────────────────


@dataclass(frozen=True)
class GeoPoint:
    """A WGS84 latitude/longitude point (altitude optional)."""
    lat: float
    lon: float
    alt: float = 0.0


@dataclass(frozen=True)
class AreaSpec:
    """A rectangular mission area boundary (degrees)."""
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


@dataclass(frozen=True)
class ZoneSpec:
    """A pre-match known static threat/no-fly zone.

    Only **static, pre-match-known** zones belong here (e.g. a fixed SAM
    site, a fixed comm-jam region). Dynamic zones (e.g. a randomly-spawning
    jam region) are NEVER placed in the briefing — the player must sense
    them via ``SelfView.jammed`` and share awareness via comms.
    """
    kind: str                       # "air_defense" | "comm_jam_static" | "no_fly" | ...
    polygon: Tuple[Tuple[float, float], ...]   # [(lat, lon), ...]
    alt_min: float = 0.0
    alt_max: float = 1e9


@dataclass(frozen=True)
class ApproxZoneSpec:
    """参赛者可见的近似区域(不是精确多边形)。

    粗框 bbox + 面积 + 类型 + 高度带。bbox 已外扩(比真区域大一圈)，
    area_m2 是真实面积。参赛者据此知道"哪片、多大、什么威胁、
    飞多高能避开"，但拿不到精确边界。
    """
    kind: str
    bbox: Tuple[Tuple[float, float], Tuple[float, float]]  # ((lat_min,lon_min),(lat_max,lon_max))
    area_m2: float
    alt_min: float = 0.0
    alt_max: float = 1e9


@dataclass(frozen=True)
class ScoreView:
    """参赛者可见的本局实时得分(只读快照,每拍由 runner 更新)。不含真值。"""
    total_score: float
    dimension_scores: Tuple[Tuple[str, float], ...]
    passed: bool
    n_destroyed: int
    n_targets: int
    sim_time: float


# spec 029: sensor() 返回哨兵。端到端选手显式返回此值表示"不需要 detection，
# 跳过默认识别器"。未覆盖 sensor / 返回 None 则走默认识别器。
SKIP_DETECTION: Any = object()

# ── self view ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Detection:
    """识别层产出的检测结果（不保证真值）。

    检测结果的来源（runner 内部概念，非本结构体字段）取决于选手如何实现
    sensor()：选手 sensor 自研 / 训练态 AccuracySimulator / 验证态
    YoloDetector。target_lat/target_lon 是识别位置，可能含噪声或漏检。
    诱饵仍可能被误识别为 ground_vehicle（多帧运动一致性判断）。
    """
    detected: bool
    confidence: float                       # [0, 1] = 1 - offset/half_fov
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    azimuth_error_deg: Optional[float] = None
    target_type: str = ""                   # "ground_vehicle" | "decoy_vehicle" | ""


@dataclass(frozen=True)
class CommStats:
    """This agent's own communication statistics for the current tick.

    These are legitimate self-perceivable signals: ``rejected_jam`` /
    ``delivered`` / ``sent`` indirectly reflect whether this agent is
    inside a (possibly dynamic) jam region.
    """
    sent: int = 0
    delivered: int = 0
    received: int = 0
    rejected_bytes: int = 0
    rejected_rate: int = 0
    rejected_range: int = 0
    rejected_jam: int = 0


@dataclass(frozen=True)
class SelfView:
    """This agent's own physically-perceivable state.

    Everything here is about THIS entity (``uid == agent.my_uid``). No
    other entity's state is reachable through SelfView.
    """
    uid: str
    lat: float
    lon: float
    alt: float
    heading_deg: float
    speed: float
    # gimbal / camera
    gimbal_pan: float
    gimbal_tilt: float
    gimbal_fov_deg: float
    detection: Detection
    # spec 029: photo — 最新相机帧 PNG bytes（UE 渲染，redis sync_camera）。
    # 默认 photo_mode=auto：非 dry_run 且 Redis 有帧时由 PhotoCache 注入；
    # 无渲染（dry_run、UE 未 assign、Redis 暂无帧）时为 None。
    photo: Optional[bytes] = None
    # spec 029 预留：多目标检测列表（赛题二/三将来上视觉通路时用）。默认空。
    detections: Tuple["Detection", ...] = ()
    # self-perception signals (the only legal channel for sensing dynamic
    # threats — see contracts/isolation.md §4)
    status: str = "active"                  # "active" | "destroyed"
    jammed: bool = False                    # is this agent currently comm-jammed
    comm_stats: CommStats = field(default_factory=CommStats)


# ── messaging ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Message:
    """One message received from a teammate this tick.

    ``payload`` is a player-defined string (≤ 50 bytes). The SDK does not
    interpret it; players agree on their own format (e.g. ``"R:27.0,125.0"``
    for a rendezvous call). Only the sender's uid is exposed — never the
    sender's pose (that would leak another entity's truth).
    """
    sender_uid: str
    payload: str
    recv_time: float


# ── briefing ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MissionBriefing:
    """Pre-match static mission info. Constant for the whole run.

    Built once by the runner at startup and shared (same reference) across
    every decide() call. Only **pre-match-known, whole-run-constant**
    information belongs here. Dynamic information (random jam positions,
    teammate poses, target truth) must NOT be placed here — see the
    isolation contract's allow/deny table.

    Extensibility: new scenarios add fields here. Every new field MUST
    have a default value so existing scenario runners keep working.
    """
    self_uid: str
    fleet_size: int
    mission_area: Optional[AreaSpec] = None
    known_threats: Tuple[ZoneSpec, ...] = ()
    params: dict = field(default_factory=dict)
    target_initial_pos: Optional[Tuple[float, float]] = None   # 仅赛题一
    target_count: Optional[int] = None                         # 赛题二三
    approximate_zones: Tuple[ApproxZoneSpec, ...] = ()
    score_view: Optional[ScoreView] = None                     # 每拍更新


# ── observation ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Observation:
    """What one agent sees in one decide() call.

    Top-level shape is fixed (stability promise): ``self`` / ``comm_inbox``
    / ``briefing``. Scenario-specific observation subclasses (e.g.
    ``SearchTrackObs``) inherit this and MAY add fields that describe THIS
    agent's own extra state — never another entity's.
    """
    self: SelfView
    comm_inbox: Tuple[Message, ...]
    briefing: MissionBriefing
