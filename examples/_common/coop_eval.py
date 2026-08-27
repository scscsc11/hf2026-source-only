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

from .uav_target_map import TargetMatch


# ── per-target state machine ─────────────────────────────────────────────


@dataclass
class _TargetState:
    """Continuous-tracking state for one real target."""
    dwell_accumulated: float = 0.0       # interruption-tolerant continuous dwell
    interruption_accumulated: float = 0.0
    has_dwell: bool = False              # True once an attempt has accumulated dwell
    completed: bool = False
    completed_at: Optional[float] = None
    resets: int = 0                      # attempts ended by a >grace interruption
    coop_ticks: int = 0
    max_dwell_run: float = 0.0           # best dwell_accumulated ever seen
    first_coop_at: Optional[float] = None
    cur_trackers: int = 0                # effective trackers this tick (diagnostic)


# ── scoring profile ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScoringProfile:
    """Per-example scoring configuration.

    ``weights`` selects which dimensions feed the total score
    (``total = Σ weights[k] * dimension_scores[k]``). Dimensions not in
    ``weights`` may still be computed for diagnostics but do not affect the
    total. ``full_coop_K`` (multi example) enables a "full-cooperation"
    bonus dimension counting ticks where any target is co-tracked by
    ``>= full_coop_K`` UAVs.
    """
    name: str
    K: int                              # cooperative threshold
    dwell_target_s: float = 20.0
    grace_s: float = 2.0
    duration_s: float = 60.0            # time budget for "latency" dimensions
    weights: dict[str, float] = field(default_factory=dict)
    full_coop_K: Optional[int] = None
    linear_completion: bool = False   # single-UAV: scale completion by dwell/dwell_target
    # pass/fail gates
    pass_completion: float = 1.0        # required completion_rate
    pass_score: float = 70.0
    pass_alive_rate: Optional[float] = None   # adversarial only


def profile_uav_search_track_car(duration_s: float = 60.0) -> ScoringProfile:
    """Single-UAV search-and-track (K=1, no decoys).

    Continuous-track completion is benchmarked at **5 minutes (300s)**: a
    full 5-minute uninterrupted track scores 100 on the completion
    dimension, scaled linearly below that (``100 * max_dwell_run / 300``).
    The default ``--duration 60`` is too short to complete — run with
    ``--duration 360+`` to evaluate completion.
    """
    return ScoringProfile(
        name="uav_search_track_car",
        K=1, dwell_target_s=300.0, grace_s=2.0, duration_s=duration_s,
        linear_completion=True,
        weights={
            "search": 0.20,        # faster first detection
            "completion": 0.30,    # reach 5-min continuous track
            "track_quality": 0.25, # keep target centered (detection.confidence)
            "stability": 0.15,     # few resets, high in-view fraction
            "time_to_all": 0.10,   # achieve quickly
        },
        pass_completion=1.0, pass_score=70.0,
    )


def profile_multi_uav_coop_decoy(duration_s: float = 120.0,
                                 K: int = 1) -> ScoringProfile:
    """3-UAV cooperative decoy scenario. Default K=1 (single UAV suffices);
    pass K>1 for a stricter cooperative gate."""
    return ScoringProfile(
        name="multi_uav_coop_decoy",
        K=K, dwell_target_s=20.0, grace_s=2.0, duration_s=duration_s,
        full_coop_K=3,
        weights={
            "completion": 0.30,
            "track_quality": 0.20,
            "time_to_all": 0.20,
            "misid": 0.10,
            "comm": 0.10,
            "full_coop": 0.10,
        },
        pass_completion=1.0, pass_score=75.0,
    )


def profile_adversarial_swarm_search(duration_s: float = 60.0,
                                     K: int = 1) -> ScoringProfile:
    """Adversarial swarm. Total blends completion / track-quality / alive
    as 0.5 / 0.2 / 0.3."""
    return ScoringProfile(
        name="adversarial_swarm_search",
        K=K, dwell_target_s=20.0, grace_s=2.0, duration_s=duration_s,
        weights={
            "completion": 0.5,
            "track_quality": 0.2,
            "alive": 0.3,
        },
        pass_completion=1.0, pass_score=70.0, pass_alive_rate=0.5,
    )


# ── evaluator ─────────────────────────────────────────────────────────────


class CoopTrackingEvaluator:
    """Accumulates per-tick cooperative-tracking state and scores it.

    Usage:
        ev = CoopTrackingEvaluator(profile, true_target_uids)
        for each tick:
            ev.observe(sim_time, uav_target_map, destroyed_uids)
            live = ev.score(extras)            # optional, for a live curve
        final = ev.score(extras)               # terminal score
    """

    def __init__(self, profile: ScoringProfile,
                 true_target_uids) -> None:
        self.profile = profile
        self.targets: set[str] = set(true_target_uids)
        self.states: dict[str, _TargetState] = {
            t: _TargetState() for t in self.targets
        }
        # global accumulators
        self.misid_ticks: int = 0
        self.total_detected_ticks: int = 0
        self.full_coop_ticks: int = 0
        self.quality_sum: float = 0.0    # Σ confidence over effective-track ticks
        self.quality_count: int = 0
        self.tick_count: int = 0
        self._last_sim_time: Optional[float] = None
        self.destroyed_uids: set[str] = set()

    # ── per-tick observation ──────────────────────────────────────────

    def observe(self, sim_time: float,
                uav_target_map: dict[str, TargetMatch],
                destroyed_uids) -> None:
        """Advance the state machines by one tick.

        Args:
            sim_time: current sim time (s); dt is derived from the previous
                tick (clamped to >=0 to dodge the sim_time baseline bug).
            uav_target_map: ``resolve_uav_to_target(...)`` output for this
                tick — ``{uav_uid: TargetMatch}``.
            destroyed_uids: UAVs whose ``platform.status == "destroyed"``.
        """
        if self._last_sim_time is None:
            dt = 0.0
        else:
            dt = sim_time - self._last_sim_time
            if dt < 0.0:
                dt = 0.0
        self._last_sim_time = sim_time
        self.tick_count += 1
        self.destroyed_uids |= set(destroyed_uids)

        destroyed = set(destroyed_uids)

        # Per-target cooperative-tracking state machine.
        any_full_coop = False
        for t, ts in self.states.items():
            if ts.completed:
                ts.cur_trackers = 0
                continue
            trackers_t = {
                u for u, m in uav_target_map.items()
                if m.is_effective and m.target_uid == t and u not in destroyed
            }
            n_trackers = len(trackers_t)
            ts.cur_trackers = n_trackers
            coop_now = n_trackers >= self.profile.K

            if (self.profile.full_coop_K is not None
                    and n_trackers >= self.profile.full_coop_K):
                any_full_coop = True

            if coop_now:
                if ts.has_dwell:
                    # Resume / continue an active attempt: back-fill the
                    # tolerated interruption into the continuous total.
                    ts.dwell_accumulated += ts.interruption_accumulated + dt
                else:
                    # Fresh attempt (first cooperation, or after a reset).
                    ts.dwell_accumulated += dt
                    ts.first_coop_at = ts.first_coop_at or sim_time
                    ts.has_dwell = True
                ts.interruption_accumulated = 0.0
                ts.coop_ticks += 1
                if ts.dwell_accumulated > ts.max_dwell_run:
                    ts.max_dwell_run = ts.dwell_accumulated
            else:
                ts.interruption_accumulated += dt
                if ts.has_dwell and ts.interruption_accumulated > self.profile.grace_s:
                    # Interruption exceeded the tolerance window: end attempt.
                    ts.dwell_accumulated = 0.0
                    ts.interruption_accumulated = 0.0
                    ts.has_dwell = False
                    ts.resets += 1

            if (not ts.completed
                    and ts.dwell_accumulated >= self.profile.dwell_target_s):
                ts.completed = True
                ts.completed_at = sim_time

        if any_full_coop:
            self.full_coop_ticks += 1

        # Misid / total-detected accounting (destroyed UAVs ignored).
        for u, m in uav_target_map.items():
            if u in destroyed:
                continue
            if m.was_misid:
                self.misid_ticks += 1
                self.total_detected_ticks += 1
            elif m.is_effective:
                self.total_detected_ticks += 1
                self.quality_sum += m.confidence
                self.quality_count += 1

    # ── derived properties ────────────────────────────────────────────

    @property
    def completed_targets(self) -> set[str]:
        return {t for t, ts in self.states.items() if ts.completed}

    @property
    def completion_rate(self) -> float:
        if not self.targets:
            return 0.0
        return len(self.completed_targets) / len(self.targets)

    @property
    def misid_rate(self) -> float:
        if self.total_detected_ticks == 0:
            return 0.0
        return self.misid_ticks / self.total_detected_ticks

    @property
    def time_to_all_completed(self) -> Optional[float]:
        """Sim time (absolute) when the last target completed, or None if
        not all completed."""
        if len(self.completed_targets) != len(self.targets):
            return None
        done = [ts.completed_at for ts in self.states.values()
                if ts.completed and ts.completed_at is not None]
        return max(done) if done else None

    # ── scoring (pure function) ───────────────────────────────────────

    def _dimension(self, key: str, extras: dict[str, Any]) -> float:
        """One dimension's 0..100 score."""
        p = self.profile
        if key == "completion":
            if self.profile.linear_completion:
                # single-UAV: linear in best continuous dwell vs target.
                best = max((ts.max_dwell_run for ts in self.states.values()),
                           default=0.0)
                return 100.0 * min(1.0, best / self.profile.dwell_target_s)
            return 100.0 * self.completion_rate
        if key == "track_quality":
            if self.quality_count == 0:
                return 0.0
            return 100.0 * (self.quality_sum / self.quality_count)
        if key == "alive":
            return 100.0 * float(extras.get("alive_rate", 0.0))
        if key == "search":
            # duration_s<=0(无限运行,web bridge 强制 --duration 0)时,
            # "search_time 占总时长比例"无定义 → 不评估该维度,返回 0.0。
            if p.duration_s <= 0:
                return 0.0
            st = float(extras.get("search_time", p.duration_s))
            return 100.0 * max(0.0, 1.0 - st / p.duration_s)
        if key == "stability":
            total_resets = sum(ts.resets for ts in self.states.values())
            reset_pen = 100.0 * (1.0 - min(1.0, total_resets / 5.0))
            ivf = float(extras.get("track_in_view_fraction", 0.0))
            return 0.5 * reset_pen + 0.5 * 100.0 * max(0.0, min(1.0, ivf))
        if key == "time_to_all":
            t_all = self.time_to_all_completed
            if t_all is None:
                return 0.0
            # duration_s<=0(无限运行)时,elapsed/duration_s 无定义 → 返回 0.0。
            if p.duration_s <= 0:
                return 0.0
            t0 = float(extras.get("sim_t0", 0.0))
            elapsed = max(0.0, t_all - t0)
            return 100.0 * max(0.0, 1.0 - elapsed / p.duration_s)
        if key == "misid":
            return 100.0 * max(0.0, 1.0 - self.misid_rate / 0.3)
        if key == "comm":
            sent = float(extras.get("comm_sent", 0))
            delivered = float(extras.get("comm_delivered", 0))
            return 100.0 * (delivered / sent) if sent > 0 else 0.0
        if key == "full_coop":
            if self.tick_count == 0:
                return 0.0
            return 100.0 * (self.full_coop_ticks / self.tick_count)
        return 0.0

    def score(self, extras: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Compute the score snapshot. Pure: does not mutate state, so it is
        safe to call every tick for a live curve and once more at the end."""
        extras = extras or {}
        dim_scores = {k: self._dimension(k, extras) for k in self.profile.weights}
        total = sum(self.profile.weights[k] * dim_scores[k]
                    for k in self.profile.weights)
        total = max(0.0, min(100.0, total))

        t_all = self.time_to_all_completed
        passed = (self.completion_rate >= self.profile.pass_completion
                  and total >= self.profile.pass_score)
        if self.profile.pass_alive_rate is not None:
            passed = passed and float(extras.get("alive_rate", 0.0)) >= self.profile.pass_alive_rate

        per_target = {
            t: {
                "completed": ts.completed,
                "completed_at_s": ts.completed_at,
                "max_dwell_run_s": ts.max_dwell_run,
                "resets": ts.resets,
                "coop_ticks": ts.coop_ticks,
            }
            for t, ts in self.states.items()
        }

        return {
            "profile": self.profile.name,
            "K": self.profile.K,
            "dwell_target_s": self.profile.dwell_target_s,
            "grace_s": self.profile.grace_s,
            "n_targets": len(self.targets),
            "n_completed": len(self.completed_targets),
            "completion_rate": self.completion_rate,
            "per_target": per_target,
            "time_to_all_completed_s": t_all,
            "misid_ticks": self.misid_ticks,
            "total_detected_ticks": self.total_detected_ticks,
            "misid_rate": self.misid_rate,
            "alive_rate": extras.get("alive_rate"),
            "tick_count": self.tick_count,
            "dimension_scores": {k: round(v, 2) for k, v in dim_scores.items()},
            "total_score": round(total, 2),
            "passed": passed,
        }
