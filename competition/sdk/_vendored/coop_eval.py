"""Cooperative continuous-tracking evaluator (Spec 025).

The C++ engine publishes only a per-tick boolean ``detected`` (target inside
the camera FOV); it does **not** maintain any "continuous track duration" or
"cooperative lock" state. This module builds that semantics on top, feeding
on the per-tick UAV->target resolution from :mod:`uav_target_map`.

Core idea — one continuous-tracking state machine **per real target**:

  * "effective tracker" of target *t* at a tick = a UAV whose
    ``resolve_uav_to_target`` match is effective (real target) and not
    destroyed.
  * "cooperative now" for *t* = ``#effective trackers >= K``.
  * dwell accumulates while cooperative; brief interruptions (<= ``grace_s``)
    are tolerated — they do NOT reset the run, and the lost time is
    back-filled into the continuous total when cooperation resumes.
  * an interruption longer than ``grace_s`` ends the attempt (reset).
  * a target is **completed** once its (interruption-tolerant) continuous
    dwell reaches ``dwell_target_s`` (20 s). Completion is permanent
    (locked) — a target that was achieved stays achieved.

``score(profile, extras)`` is a **stateless pure function**: it only reads
the accumulated state and the caller-supplied ``extras`` (search time,
track-in-view fraction, comm stats, alive rate, ...), so it can be called
every control tick to render a live "score over time" curve and again at
run end for the final score. The total score is **not** monotonic by
design — it reflects "quality up to now" and can drop (more misid, UAVs
destroyed, a reset). Only ``completion_rate`` is monotonic (progress).

Dimension weights are example-specific (see ``profile_*`` factories). The
adversarial example blends ``0.7*completion + 0.3*alive`` into the total.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .geometry import haversine_m as _haversine_m
from .uav_target_map import TargetMatch


# ── per-target state machine ─────────────────────────────────────────────


@dataclass
class _TargetState:
    """Per real-target destroy state machine (overhauled scoring)."""
    dwell_accumulated: float = 0.0       # cooperative dwell toward destroy
    interruption_accumulated: float = 0.0
    has_dwell: bool = False
    destroyed: bool = False              # killed (≥1 UAV for dwell_target_s)
    destroyed_at: Optional[float] = None
    resets: int = 0
    coop_ticks: int = 0
    cur_trackers: int = 0
    # scenario-1 in-view accounting (only used when profile.K==1 and no decoys)
    in_view_seconds: float = 0.0


@dataclass
class _DecoyState:
    """Per decoy identification state (overhauled scoring).

    A decoy is "identified" after ≥1 UAV tracks it for dwell_target_s; once
    identified it stops contributing to the misid-penalty dimension.
    """
    dwell_accumulated: float = 0.0
    interruption_accumulated: float = 0.0
    has_dwell: bool = False
    identified: bool = False
    identified_at: Optional[float] = None
    resets: int = 0
    cur_trackers: int = 0


# ── scoring profile ───────────────────────────────────────────────────────

# Fallback terrain bbox used only if the CSV cannot be read. Matches the
# HeightSample.csv rectangle to ~3 decimals. Real value is derived below.
_TERRAIN_BBOX_FALLBACK: tuple[tuple[float, float], tuple[float, float]] = (
    (26.982, 124.980), (27.025, 125.020))


def _terrain_csv_path() -> "Path":
    """Resolve the terrain CSV path.

    Preference order: ``OPENSIM_TERRAIN_CSV`` env var, then the repo-rooted
    ``config/HeightSample.csv``. The repo root is derived from this file's
    location (``competition/sdk/_vendored/`` → 3 levels up).
    """
    import os
    from pathlib import Path
    env = os.environ.get("OPENSIM_TERRAIN_CSV")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "config" / "HeightSample.csv"


def _terrain_bbox_from_csv(csv_path: "Path | None" = None
                           ) -> tuple[tuple[float, float], tuple[float, float]]:
    """Stream-scan the terrain CSV and return its WGS84 bbox.

    Returns ``((lat_min, lon_min), (lat_max, lon_max))``. The CSV header is
    ``Longitude,Latitude,Height(m)`` — i.e. lon is column 0, lat is column 1
    — and is parsed strictly by header name (not column index) so a column
    re-ordering cannot silently flip the axes.

    On any error (file missing / unparseable / empty) it falls back to
    :data:`_TERRAIN_BBOX_FALLBACK` so scoring never crashes.
    """
    import csv as _csv
    from pathlib import Path
    path = Path(csv_path) if csv_path is not None else _terrain_csv_path()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = _csv.reader(fh)
            header = next(reader)
            # Locate columns by header name (case-insensitive) to be robust
            # against column re-ordering. Require both axes present.
            lower = [h.strip().lower() for h in header]
            lon_idx = next((i for i, h in enumerate(lower)
                            if h.startswith("lon")), None)
            lat_idx = next((i for i, h in enumerate(lower)
                            if h.startswith("lat")), None)
            if lon_idx is None or lat_idx is None:
                raise ValueError(f"CSV header {header!r} missing lat/lon cols")
            lat_min = lat_max = lon_min = lon_max = None
            n = 0
            for row in reader:
                if not row:
                    continue
                lat = float(row[lat_idx])
                lon = float(row[lon_idx])
                if n == 0:
                    lat_min = lat_max = lat
                    lon_min = lon_max = lon
                else:
                    if lat < lat_min: lat_min = lat
                    elif lat > lat_max: lat_max = lat
                    if lon < lon_min: lon_min = lon
                    elif lon > lon_max: lon_max = lon
                n += 1
            if n == 0:
                raise ValueError("CSV has no data rows")
        return ((lat_min, lon_min), (lat_max, lon_max))
    except Exception as exc:  # noqa: BLE001 — scoring must not crash
        import sys
        print(f"[coop_eval] WARNING: terrain bbox from CSV failed ({exc!r}); "
              f"falling back to hardcoded value", file=sys.stderr)
        return _TERRAIN_BBOX_FALLBACK


def _terrain_bbox_cache_path() -> "Path":
    """Resolve the JSON cache path for the terrain bbox."""
    from pathlib import Path
    return Path(__file__).resolve().parents[3] / "config" / "terrain_bbox.json"


def _load_terrain_bbox_cache() -> "tuple[tuple[float, float], tuple[float, float]] | None":
    """Return cached bbox if the JSON cache is valid, else None."""
    import json
    path = _terrain_bbox_cache_path()
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return (
            (float(data["lat_min"]), float(data["lon_min"])),
            (float(data["lat_max"]), float(data["lon_max"])),
        )
    except Exception:
        return None


def _save_terrain_bbox_cache(bbox: "tuple[tuple[float, float], tuple[float, float]]") -> None:
    """Persist the computed bbox to JSON. Best-effort; failures are non-fatal."""
    import json
    (lat_min, lon_min), (lat_max, lon_max) = bbox
    data = {
        "lat_min": lat_min,
        "lon_min": lon_min,
        "lat_max": lat_max,
        "lon_max": lon_max,
    }
    path = _terrain_bbox_cache_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        import sys
        print(f"[coop_eval] WARNING: failed to write terrain bbox cache: {e!r}",
              file=sys.stderr)


# Single source of truth for the map extent. Derived lazily from the terrain
# bbox JSON cache (preferred) or the terrain CSV (same file the C++ engine
# loads) on first use, then cached in memory. Format:
# ((lat_min, lon_min), (lat_max, lon_max)). Used as the boundary-excursion
# penalty's mission_bbox; UAVs may fly up to ``penalty_boundary_margin_m``
# (500 m) outside this rectangle without penalty.
_TERRAIN_BBOX: Optional[tuple[tuple[float, float], tuple[float, float]]] = None


def _get_terrain_bbox() -> tuple[tuple[float, float], tuple[float, float]]:
    """Lazily derive the terrain bbox from the JSON cache first, then the CSV."""
    global _TERRAIN_BBOX
    if _TERRAIN_BBOX is not None:
        return _TERRAIN_BBOX
    cached = _load_terrain_bbox_cache()
    if cached is not None:
        _TERRAIN_BBOX = cached
        return _TERRAIN_BBOX
    _TERRAIN_BBOX = _terrain_bbox_from_csv()
    _save_terrain_bbox_cache(_TERRAIN_BBOX)
    return _TERRAIN_BBOX


@dataclass(frozen=True)
class ScoringProfile:
    """Per-scenario scoring configuration (overhauled scoring system).

    ``weights`` selects which dimensions feed the total score
    (``total = Σ weights[k] * dimension_scores[k]``). New dimension keys:
      * ``completion``   — scenario 1: accumulated in-view fraction.
      * ``kill``         — scenarios 2/3: fraction of true targets destroyed.
      * ``accuracy``     — scenarios 2/3: targeting RMSE vs D_max_m.
      * ``misid_penalty``— scenarios 2/3: 1 - (undestroyed-decoy track s)/misid_cap_s.
      * ``mission_time`` — scenario 3 only: T_done vs T0_s/T_flex_s.
      * ``alive``        — scenario 3 only: surviving UAV fraction.
    """
    name: str
    K: int                              # cooperative threshold (trackers needed)
    dwell_target_s: float = 20.0        # destroy/identify threshold
    grace_s: float = 2.0                # interruption tolerance before reset
    duration_s: float = 60.0            # run duration (for completion ratio)
    weights: dict[str, float] = field(default_factory=dict)
    # targeting-accuracy dimension (scenarios 2/3)
    D_max_m: Optional[float] = None     # RMSE at which accuracy scores 0
    # misid-penalty dimension (scenarios 2/3)
    misid_cap_s: Optional[float] = None  # undestroyed-decoy track seconds -> 0
    # mission-time dimension (scenario 3 only)
    T0_s: Optional[float] = None        # time-to-full-kill for full marks
    T_flex_s: Optional[float] = None    # linear decay window past T0
    # pass/fail gates
    pass_completion: float = 1.0        # required completion/kill rate
    pass_score: float = 70.0
    pass_alive_rate: Optional[float] = None   # scenario 3 only
    # post-hoc penalty deductions (NOT part of the weighted dimension sum).
    # Applied as: total_score = clamp(base_score - penalty, 0, 100).
    # All zero/None => penalty logic is fully disabled (short-circuit).
    penalty_proximity_min_m: float = 0.0        # 0 = disabled; >0 = UAV-too-close threshold
    penalty_boundary_margin_m: float = 0.0      # 0 = disabled; >0 = allowed excursion beyond bbox
    penalty_per_violation: float = 0.0          # 0 = whole penalty subsystem disabled
    penalty_cap: float = 0.0                    # 0 = no cap (but also no penalty when per_violation=0)
    mission_bbox: Optional[tuple[tuple[float, float], tuple[float, float]]] = None


def profile_uav_search_track_car(duration_s: float = 60.0) -> ScoringProfile:
    """Scenario 1: continuous designation accuracy (no strike capability).

    Single dimension: per-tick soft-hit accuracy of the player's 1Hz
    target-coordinate reports. Each second the judge samples the player's
    report; D_t = haversine distance to the nearest live true target, and
    p_t = clamp(1 - D_t/D_max, 0, 1). Missed reports score p_t = 0. Within
    each fixed 20s window the 2 lowest p_t are dropped (judging-style
    "discard two lowest scores"). The dimension score is 100 × mean of the
    kept p_t over the whole run.
    """
    return ScoringProfile(
        name="uav_search_track_car",
        # dwell_target_s: 赛题一无打击能力,目标永不被「摧毁」。原来传 0.0 是
        # bug —— observe() 里 `dwell_accumulated(>=0) >= 0.0` 在第一帧检出目标
        # 即摧毁,导致 record_report 因「无存活目标」(best_uid=None)丢弃选手
        # 全部目指上报 → n_reports=0、score=0。
        # 用一个大但有限的数(1e18)而非 inf:dwell_accumulated 永远达不到它,
        # 语义等价「永不摧毁」;但它是合法 JSON 数字。inf 会让 score_publisher
        # 的 json.dumps 产出非标准 token 'Infinity',前端 JSON.parse 抛
        # SyntaxError 被静默吞掉 → onScoreCb 永不触发 → 面板一直 '--'。
        K=1, dwell_target_s=1e18, grace_s=2.0, duration_s=duration_s,
        weights={"accuracy": 1.0},
        D_max_m=30.0,
        pass_completion=0.8, pass_score=60.0,   # pass_completion 在 score() 中被跳过(见 §4.3)
    )


def profile_multi_uav_coop_decoy(duration_s: float = 120.0,
                                 K: int = 1) -> ScoringProfile:
    """Scenario 2: targeting info + strike effect (with strike capability).

    Destroy all 3 real targets (track each >=20s with >=K UAVs), report
    accurate target coordinates, and finish fast. Decoy misid penalty has
    been REMOVED (decoys no longer affect the score); the decoy-identify
    state machine still runs harmlessly. Two post-hoc penalty deductions
    apply: UAV proximity (<200m) and boundary excursion (>500m outside the
    terrain bbox).
    """
    return ScoringProfile(
        name="multi_uav_coop_decoy",
        K=K, dwell_target_s=20.0, grace_s=2.0, duration_s=duration_s,
        weights={"kill": 0.50, "accuracy": 0.30, "mission_time": 0.20},
        D_max_m=120.0, misid_cap_s=30.0,
        T0_s=240.0, T_flex_s=180.0,
        pass_completion=2.0/3.0, pass_score=70.0,
        penalty_proximity_min_m=200.0,
        penalty_boundary_margin_m=500.0,
        penalty_per_violation=2.0,
        penalty_cap=15.0,
        mission_bbox=_get_terrain_bbox(),
    )


def profile_adversarial_swarm_search(duration_s: float = 60.0,
                                     K: int = 1) -> ScoringProfile:
    """Scenario 3: targeting + strike + mission time (adversarial).

    Destroy all 10 real targets fast, report accurately, survive. Decoy
    misid penalty REMOVED; decoy-identify machine kept (harmless). Mission
    time rewards finishing all real-target kills quickly (T0=360s full
    marks, linear decay to 0 by T0+T_flex=540s). Two post-hoc penalty
    deductions apply: UAV proximity (<200m) and boundary excursion (>500m
    outside the terrain bbox). Cooperative threshold K is injected at
    runtime via resolve_k() (=3 for the default adversarial scenario).
    """
    return ScoringProfile(
        name="adversarial_swarm_search",
        K=K, dwell_target_s=20.0, grace_s=2.0, duration_s=duration_s,
        weights={
            "kill": 0.40, "accuracy": 0.25, "mission_time": 0.25,
            "alive": 0.10,
        },
        D_max_m=150.0, misid_cap_s=60.0,
        T0_s=360.0, T_flex_s=180.0,
        pass_completion=0.7, pass_score=70.0, pass_alive_rate=0.5,
        penalty_proximity_min_m=200.0,
        penalty_boundary_margin_m=500.0,
        penalty_per_violation=2.0,
        penalty_cap=15.0,
        mission_bbox=_get_terrain_bbox(),
    )


# ── evaluator ─────────────────────────────────────────────────────────────


class CoopTrackingEvaluator:
    """Accumulates per-tick destroy/identify state and scores it (overhauled).

    Usage:
        ev = CoopTrackingEvaluator(profile, true_target_uids)
        for each tick:
            ev.observe(sim_time, uav_target_map, destroyed_uids)
            ev.record_report(...)         # for each player report this tick
            live = ev.score(extras)       # optional, for a live curve
        final = ev.score(extras)          # terminal score
    """

    def __init__(self, profile: ScoringProfile,
                 true_target_uids) -> None:
        self.profile = profile
        self.targets: set[str] = set(true_target_uids)
        self.states: dict[str, _TargetState] = {
            t: _TargetState() for t in self.targets
        }
        self.decoy_states: dict[str, _DecoyState] = {}
        # global accumulators
        self.undestroyed_decoy_misid_seconds: float = 0.0
        self.tick_count: int = 0
        self._last_sim_time: Optional[float] = None
        self.destroyed_uids: set[str] = set()      # destroyed UAVs
        # targeting-report accumulators (scenarios 2/3) — per-target buckets
        self._sum_d_sq: dict[str, float] = {t: 0.0 for t in self.targets}
        self._n_reports: dict[str, int] = {t: 0 for t in self.targets}
        self._last_report_time: dict[str, float] = {}  # rate-limit per resolved uid
        # scenario-1 in-view (sum over ticks where any controllable detects)
        self._s1_in_view_seconds: float = 0.0
        # scenario-1 continuous-designation timeseries (spec 2026-07-15):
        # list of (sim_t_s, D_t_m) per 1Hz sample; missed seconds are NOT
        # recorded here (they are synthesized as p_t=0 at scoring time from
        # the expected 1Hz grid up to duration_s).
        self._s1_samples: list[tuple[float, float]] = []
        # scenario-1 时间基准: observe() 第一帧捕获,record_report 存入
        # _s1_samples 时把绝对 epoch sim_time 归一成「仿真开始后秒数」,
        # 与 _score_s1_accuracy 的 1Hz 网格 [0, duration_s) 对齐。否则引擎
        # 绝对 epoch(如 1782432000)永远落不进 0..duration-1 的 slot,
        # 所有拍被当成漏报 → accuracy=0。
        self._s1_t0: Optional[float] = None
        # post-hoc penalty accumulators (edge-triggered violation counts)
        self.proximity_violations: int = 0
        self.boundary_violations: int = 0
        self._prev_proximity_pairs: frozenset[tuple[str, str]] = frozenset()
        self._prev_oob_uavs: frozenset[str] = frozenset()

    @property
    def n_reports(self) -> int:
        """Total reports ingested across all targets (read-only aggregate)."""
        return sum(self._n_reports.values())

    # ── per-tick observation ──────────────────────────────────────────

    def observe(self, sim_time: float,
                uav_target_map: dict[str, TargetMatch],
                destroyed_uids,
                uav_positions: Optional[dict[str, tuple[float, float]]] = None,
                ) -> None:
        """Advance the destroy/identify state machines by one tick.

        Args:
            sim_time: current sim time (s); dt is derived from the previous
                tick (clamped to >=0 to dodge the sim_time baseline bug).
            uav_target_map: ``resolve_uav_to_target(...)`` output for this
                tick — ``{uav_uid: TargetMatch}``.
            destroyed_uids: UAVs whose ``platform.status == "destroyed"``.
            uav_positions: optional ``{uav_uid: (lat, lon)}`` of ACTIVE UAV
                positions, used by the post-hoc penalty dimensions (proximity
                & boundary excursion). When None or when the profile has
                penalties disabled (``penalty_per_violation == 0``), penalty
                state is untouched (backward compatible).
        """
        if self._last_sim_time is None:
            dt = 0.0
        else:
            dt = sim_time - self._last_sim_time
            if dt < 0.0:
                dt = 0.0
        self._last_sim_time = sim_time
        # 赛题一归一化基准: 首帧 sim_time(绝对 epoch)。record_report 用它把
        # 样本时间归一成 [0, duration_s) 相对秒,与评分 1Hz 网格对齐。
        if self._s1_t0 is None:
            self._s1_t0 = sim_time
        self.tick_count += 1
        self.destroyed_uids |= set(destroyed_uids)
        dead_uavs = set(destroyed_uids)

        # ── scenario 1: accumulated in-view (single UAV, K=1, no decoys) ──
        # If this profile has only the "completion" weight and K==1, count
        # in-view seconds from any effective detection.
        if (tuple(self.profile.weights.keys()) == ("completion",)
                and self.profile.K == 1):
            any_eff = any(m.is_effective for u, m in uav_target_map.items()
                          if u not in dead_uavs)
            if any_eff:
                self._s1_in_view_seconds += dt

        # ── per real-target destroy state machine (scenarios 2/3) ─────────
        for t, ts in self.states.items():
            if ts.destroyed:
                ts.cur_trackers = 0
                continue
            trackers_t = {
                u for u, m in uav_target_map.items()
                if m.is_effective and m.target_uid == t and u not in dead_uavs
            }
            n = len(trackers_t)
            ts.cur_trackers = n
            coop_now = n >= self.profile.K
            if coop_now:
                if ts.has_dwell:
                    # Resume: back-fill the tolerated interruption.
                    ts.dwell_accumulated += ts.interruption_accumulated + dt
                else:
                    ts.dwell_accumulated += dt
                    ts.has_dwell = True
                ts.interruption_accumulated = 0.0
                ts.coop_ticks += 1
            else:
                ts.interruption_accumulated += dt
                if ts.has_dwell and ts.interruption_accumulated > self.profile.grace_s:
                    ts.dwell_accumulated = 0.0
                    ts.interruption_accumulated = 0.0
                    ts.has_dwell = False
                    ts.resets += 1
            if (not ts.destroyed
                    and ts.dwell_accumulated >= self.profile.dwell_target_s):
                ts.destroyed = True
                ts.destroyed_at = sim_time

        # ── per decoy identification state machine (scenarios 2/3) ────────
        # Group misid matches by decoy_uid to advance each decoy's dwell.
        decoy_trackers: dict[str, set[str]] = {}
        for u, m in uav_target_map.items():
            if u in dead_uavs or not m.was_misid or m.decoy_uid is None:
                continue
            decoy_trackers.setdefault(m.decoy_uid, set()).add(u)
        for d_uid, trackers in decoy_trackers.items():
            ds = self.decoy_states.setdefault(d_uid, _DecoyState())
            if ds.identified:
                ds.cur_trackers = 0
                continue
            n = len(trackers)
            ds.cur_trackers = n
            coop_now = n >= self.profile.K
            if coop_now:
                if ds.has_dwell:
                    ds.dwell_accumulated += ds.interruption_accumulated + dt
                else:
                    ds.dwell_accumulated += dt
                    ds.has_dwell = True
                ds.interruption_accumulated = 0.0
            else:
                ds.interruption_accumulated += dt
                if ds.has_dwell and ds.interruption_accumulated > self.profile.grace_s:
                    ds.dwell_accumulated = 0.0
                    ds.interruption_accumulated = 0.0
                    ds.has_dwell = False
                    ds.resets += 1
            if (not ds.identified
                    and ds.dwell_accumulated >= self.profile.dwell_target_s):
                ds.identified = True
                ds.identified_at = sim_time

        # ── misid-penalty accumulator: only UNIDENTIFIED decoys count ─────
        # Each unidentified decoy that is being tracked this tick contributes
        # one dt (NOT per-UAV — the penalty is "time a decoy was tracked",
        # not "UAV-seconds"), so a decoy watched by 2 UAVs counts the same as
        # one watched by 1.
        for d_uid in decoy_trackers:
            ds = self.decoy_states.get(d_uid)
            if ds is None or ds.identified:
                continue
            self.undestroyed_decoy_misid_seconds += dt

        # ── post-hoc penalty: proximity & boundary (edge-triggered) ────────
        # Only active when the profile enables penalties. A violation is
        # counted ONCE when a UAV/pair ENTERS a violating state; sustained
        # violation across ticks adds nothing. Leaving + re-entering counts
        # again. Destroyed UAVs are excluded.
        if (self.profile.penalty_per_violation > 0.0
                and uav_positions is not None):
            active = {u: pos for u, pos in uav_positions.items()
                      if u not in dead_uavs}
            # ─ proximity: pairs closer than penalty_proximity_min_m ──
            if self.profile.penalty_proximity_min_m > 0.0:
                cur_pairs: set[tuple[str, str]] = set()
                items = list(active.items())
                for i in range(len(items)):
                    u1, (la1, lo1) = items[i]
                    for j in range(i + 1, len(items)):
                        u2, (la2, lo2) = items[j]
                        if _haversine_m(la1, lo1, la2, lo2) < self.profile.penalty_proximity_min_m:
                            cur_pairs.add((u1, u2) if u1 < u2 else (u2, u1))
                cur_pairs_fz = frozenset(cur_pairs)
                new_pairs = cur_pairs_fz - self._prev_proximity_pairs
                self.proximity_violations += len(new_pairs)
                self._prev_proximity_pairs = cur_pairs_fz
            # ─ boundary: UAVs > penalty_boundary_margin_m outside the bbox ──
            if (self.profile.penalty_boundary_margin_m > 0.0
                    and self.profile.mission_bbox is not None):
                cur_oob: set[str] = set()
                for u, (la, lo) in active.items():
                    d_out = self._distance_outside_bbox_m(la, lo)
                    if d_out > self.profile.penalty_boundary_margin_m:
                        cur_oob.add(u)
                cur_oob_fz = frozenset(cur_oob)
                new_oob = cur_oob_fz - self._prev_oob_uavs
                self.boundary_violations += len(new_oob)
                self._prev_oob_uavs = cur_oob_fz

    def _distance_outside_bbox_m(self, lat: float, lon: float) -> float:
        """Perpendicular distance (m) of a point OUTSIDE the mission bbox.

        Returns 0.0 if the point is inside the bbox. Uses a clamp + haversine
        to the nearest bbox edge point (good enough for a 500m threshold;
        not a geodesic-exact projection, but the error is << the margin at
        these latitudes).
        """
        bbox = self.profile.mission_bbox
        if bbox is None:
            return 0.0
        (lat_min, lon_min), (lat_max, lon_max) = bbox
        clat = min(max(lat, lat_min), lat_max)
        clon = min(max(lon, lon_min), lon_max)
        if clat == lat and clon == lon:
            return 0.0
        return _haversine_m(lat, lon, clat, clon)

    # ── targeting-report ingestion (scenarios 2/3) ──────────────────────

    def record_report(self, lat: float, lon: float, target_id,
                      true_target_positions: dict[str, tuple[float, float]],
                      destroyed_true_targets: set,
                      sim_time: float) -> None:
        """Ingest one player report; accumulate per-target RMSE.

        Resolves the report to the nearest LIVE true target (best_uid). If the
        report is actually closer to a DESTROYED target (player reporting a
        "corpse"), the whole report is dropped — it does not pollute any live
        target's bucket.
        Rate-limits to 1 report per resolved true-target per second (key =
        best_uid; the player-supplied ``target_id`` is only an audit label and
        does NOT participate in rate limiting or scoring).
        """
        if not true_target_positions:
            return
        best_uid = None
        live_d = None
        for t_uid, (tlat, tlon) in true_target_positions.items():
            if t_uid in destroyed_true_targets:
                continue  # destroyed targets handled separately below
            d = _haversine_m(lat, lon, tlat, tlon)
            if live_d is None or d < live_d:
                live_d, best_uid = d, t_uid
        if best_uid is None:
            return  # no live target to score against → ignore
        # Drop reports that are actually aimed at an already-destroyed target
        # (player is reporting a "corpse"). Determined by relative
        # nearest-neighbour: if the closest destroyed target is nearer than the
        # closest live one, discard the whole report — it must not pollute any
        # live target's bucket.
        dead_d = None
        for t_uid, (tlat, tlon) in true_target_positions.items():
            if t_uid not in destroyed_true_targets:
                continue
            d = _haversine_m(lat, lon, tlat, tlon)
            if dead_d is None or d < dead_d:
                dead_d = d
        if dead_d is not None and dead_d < live_d:
            return  # reporting a destroyed target → drop
        # rate limit per resolved true-target (best_uid), NOT per the
        # player-supplied target_id label — otherwise rotating arbitrary
        # target_id strings on the same target would bypass the 1/sec limit.
        last = self._last_report_time.get(best_uid)
        if last is not None and sim_time - last < 1.0:
            return
        self._last_report_time[best_uid] = sim_time
        self._sum_d_sq[best_uid] += live_d * live_d
        self._n_reports[best_uid] += 1
        # scenario-1 continuous-designation timeseries (spec 2026-07-15):
        # record this 1Hz sample so _dimension("accuracy") can apply the
        # per-20s-window "drop 2 lowest" rule. K==1 + single-target mode.
        # 时间归一成相对秒(减去首帧 _s1_t0),与 _score_s1_accuracy 的 1Hz
        # 网格 [0, duration_s) 对齐 —— 引擎 sim_time 是绝对 epoch,直接存入
        # 会落不进任何 slot,整段被当成漏报 → accuracy=0。
        # _s1_t0 通常由 observe() 首帧设置;若调用方未先 observe(如单元测试
        # 直接 record_report),则在首条 report 时惰性固定,保证同一 evaluator
        # 全程用同一基准。
        if self.profile.K == 1 and tuple(self.profile.weights.keys()) == ("accuracy",):
            if self._s1_t0 is None:
                self._s1_t0 = sim_time
            rel_t = sim_time - self._s1_t0
            if rel_t < 0.0:
                rel_t = 0.0
            self._s1_samples.append((rel_t, live_d))

    # ── query helpers ───────────────────────────────────────────────────

    def is_destroyed(self, target_uid: str) -> bool:
        ts = self.states.get(target_uid)
        return ts is not None and ts.destroyed

    def is_decoy_identified(self, decoy_uid: str) -> bool:
        ds = self.decoy_states.get(decoy_uid)
        return ds is not None and ds.identified

    @property
    def kill_rate(self) -> float:
        """Fraction of true targets destroyed (scenarios 2/3)."""
        if not self.targets:
            return 0.0
        return sum(1 for ts in self.states.values() if ts.destroyed) / len(self.targets)

    @property
    def completion_rate(self) -> Optional[float]:
        """Back-compat: scenario-1 in-view fraction; scenarios 2/3 kill rate.

        Scenario 1 in continuous-designation mode (accuracy weight, spec
        2026-07-15) has no completion concept — returns None to avoid
        misreporting kill_rate=0 (which would mislead the passed gate and
        front-end diagnostics).
        """
        if (self.profile.K == 1
                and tuple(self.profile.weights.keys()) == ("accuracy",)):
            return None
        if (tuple(self.profile.weights.keys()) == ("completion",)
                and self.profile.K == 1):
            dur = self.profile.duration_s
            return min(1.0, self._s1_in_view_seconds / dur) if dur > 0 else 0.0
        return self.kill_rate

    @property
    def mission_done_time(self) -> Optional[float]:
        """Sim time when the last true target was destroyed, or None if not all."""
        if any(not ts.destroyed for ts in self.states.values()):
            return None
        done = [ts.destroyed_at for ts in self.states.values()
                if ts.destroyed and ts.destroyed_at is not None]
        return max(done) if done else None

    # ── scoring (pure function) ───────────────────────────────────────

    def _dimension(self, key: str, extras: dict[str, Any]) -> float:
        """One dimension's 0..100 score (overhauled dimensions)."""
        p = self.profile
        if key == "completion":
            # scenario 1: accumulated in-view fraction
            dur = p.duration_s
            if dur <= 0:
                return 0.0
            return 100.0 * min(1.0, self._s1_in_view_seconds / dur)
        if key == "kill":
            return 100.0 * self.kill_rate
        if key == "accuracy":
            # ── scenario 1: continuous designation accuracy (spec 2026-07-15)
            # Per-tick soft-hit mean with per-20s-window "drop 2 lowest".
            if (self.profile.K == 1
                    and tuple(self.profile.weights.keys()) == ("accuracy",)):
                return self._score_s1_accuracy()
            # ── scenarios 2/3: per-target RMSE (unchanged) ──
            if p.D_max_m is None or not self.targets:
                return 0.0
            total = 0.0
            for uid in self.targets:
                n = self._n_reports.get(uid, 0)
                if n == 0:
                    acc_t = 0.0  # unreported target scores 0 (anti-specialization: can't skip targets)
                else:
                    rmse = (self._sum_d_sq[uid] / n) ** 0.5
                    acc_t = 100.0 * max(0.0, 1.0 - rmse / p.D_max_m)
                total += acc_t
            return total / len(self.targets)
        if key == "misid_penalty":
            if p.misid_cap_s is None:
                return 0.0
            cap = p.misid_cap_s
            return 100.0 * max(0.0, 1.0 - self.undestroyed_decoy_misid_seconds / cap)
        if key == "mission_time":
            if p.T0_s is None or p.T_flex_s is None:
                return 0.0
            t_done = self.mission_done_time
            if t_done is None:
                return 0.0
            if t_done <= p.T0_s:
                return 100.0
            if t_done <= p.T0_s + p.T_flex_s:
                return 100.0 * (1.0 - (t_done - p.T0_s) / p.T_flex_s)
            return 0.0
        if key == "alive":
            return 100.0 * float(extras.get("alive_rate", 0.0))
        return 0.0

    # ── scenario-1 continuous-designation accuracy (spec 2026-07-15) ────
    _S1_WINDOW_S: float = 20.0
    _S1_DROP_PER_WINDOW: int = 2

    def _score_s1_accuracy(self) -> float:
        """Per-tick soft-hit mean with per-20s-window "drop 2 lowest".

        Builds the expected 1Hz grid over [0, duration_s): each integer second
        is a sample slot. A slot with a recorded report → D_t = its distance;
        a slot with no report (missed) → D_t = D_max (p_t = 0). Slots are
        grouped into fixed 20s windows; within each window the 2 lowest p_t
        are dropped (not counted in numerator or denominator). Returns
        100 × mean of kept p_t. Returns 0 when no kept samples exist.
        """
        p = self.profile
        if p.D_max_m is None or p.D_max_m <= 0 or p.duration_s <= 0:
            return 0.0
        d_max = float(p.D_max_m)
        win_s = self._S1_WINDOW_S
        drop = self._S1_DROP_PER_WINDOW

        # map recorded samples by their integer-second slot
        reported: dict[int, float] = {}
        for sim_t, d_t in self._s1_samples:
            slot = int(sim_t)
            # keep the last report within a slot (1Hz grid; rate-limit already
            # prevents >1/sec, but be defensive against float edges)
            reported[slot] = d_t

        # build per-window buckets of p_t over the expected 1Hz grid
        buckets: dict[int, list[float]] = {}
        n_slots = int(p.duration_s)  # slots 0..n_slots-1
        for slot in range(n_slots):
            d_t = reported.get(slot, d_max)   # missed slot → D=D_max → p=0
            p_t = max(0.0, 1.0 - d_t / d_max)
            buckets.setdefault(slot // int(win_s), []).append(p_t)

        numerator = 0.0
        denominator = 0
        for ps in buckets.values():
            if len(ps) <= drop:
                continue              # too few samples in window → exempt whole window
            ps_sorted = sorted(ps)
            kept = ps_sorted[drop:]   # drop the `drop` lowest
            numerator += sum(kept)
            denominator += len(kept)
        if denominator == 0:
            return 0.0
        return 100.0 * numerator / denominator

    def score(self, extras: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Compute the score snapshot. Pure: does not mutate state, so it is
        safe to call every tick for a live curve and once more at the end."""
        extras = extras or {}
        dim_scores = {k: self._dimension(k, extras) for k in self.profile.weights}
        base = sum(self.profile.weights[k] * dim_scores[k]
                   for k in self.profile.weights)
        base = max(0.0, min(100.0, base))

        # post-hoc penalty (excluded from the weighted dimension sum).
        if self.profile.penalty_per_violation > 0.0:
            raw_pen = (self.proximity_violations + self.boundary_violations) \
                      * self.profile.penalty_per_violation
            penalty = min(self.profile.penalty_cap, raw_pen) \
                if self.profile.penalty_cap > 0.0 else raw_pen
        else:
            penalty = 0.0
        total = max(0.0, min(100.0, base - penalty))

        comp = self.completion_rate
        # passed gate uses BASE (task achievement), not penalized total.
        # Scenario 1 in continuous-designation mode (accuracy weight) has no
        # completion concept — skip the completion gate, judge only on base.
        if comp is None:
            passed = base >= self.profile.pass_score
        else:
            passed = (comp >= self.profile.pass_completion
                      and base >= self.profile.pass_score)
        if self.profile.pass_alive_rate is not None:
            passed = passed and float(extras.get("alive_rate", 0.0)) >= self.profile.pass_alive_rate

        per_target = {
            t: {
                "destroyed": ts.destroyed,
                "destroyed_at_s": ts.destroyed_at,
                "dwell_accumulated_s": ts.dwell_accumulated,
                "resets": ts.resets,
                "coop_ticks": ts.coop_ticks,
            }
            for t, ts in self.states.items()
        }
        per_decoy = {
            d: {"identified": ds.identified, "identified_at_s": ds.identified_at}
            for d, ds in self.decoy_states.items()
        }
        # Diagnostic global-pooled RMSE (absolute mean targeting error across
        # all targets). NOTE: this is NOT how the accuracy *dimension* is
        # computed — that uses per-target averaging with unreported=0 (see
        # _dimension). Kept for backward-compatible diagnostics.
        _total_sq = sum(self._sum_d_sq.values())
        _total_n = self.n_reports
        rmse = ((_total_sq / _total_n) ** 0.5 if _total_n else None)

        return {
            "profile": self.profile.name,
            "K": self.profile.K,
            "dwell_target_s": self.profile.dwell_target_s,
            "grace_s": self.profile.grace_s,
            "n_targets": len(self.targets),
            "n_destroyed": sum(1 for ts in self.states.values() if ts.destroyed),
            "completion_rate": comp,
            "kill_rate": self.kill_rate,
            "per_target": per_target,
            "per_decoy": per_decoy,
            "mission_done_time_s": self.mission_done_time,
            "undestroyed_decoy_misid_s": self.undestroyed_decoy_misid_seconds,
            "targeting_rmse_m": rmse,
            "n_reports": self.n_reports,
            "alive_rate": extras.get("alive_rate"),
            "tick_count": self.tick_count,
            "dimension_scores": {k: round(v, 2) for k, v in dim_scores.items()},
            "base_score": round(base, 2),
            "penalty": round(penalty, 2),
            "penalty_breakdown": {
                "proximity": {
                    "count": self.proximity_violations,
                    "unit": self.profile.penalty_per_violation,
                },
                "boundary": {
                    "count": self.boundary_violations,
                    "unit": self.profile.penalty_per_violation,
                },
                "cap": self.profile.penalty_cap,
            },
            "total_score": round(total, 2),
            "passed": passed,
        }
