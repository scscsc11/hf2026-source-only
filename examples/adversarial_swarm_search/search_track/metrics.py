"""Spec 019 — Run-level metrics collector.

Implements FR-026 (key performance indicators) end-to-end:

  * alive_count            : UAV count whose status != "destroyed" at end
  * destroyed_count        : cumulative kill events observed
  * alive_rate             : alive_count / initial_uav_count
  * targets_discovered     : unique true targets ever detected (true_positive_ids)
  * discovery_rate         : discovered / 10
  * tracking_ticks         : sum of (tick, detected=true, misid_flag=false)
  * misid_ticks            : sum of (tick, detected=true, misid_flag=true)
  * total_detected_ticks   : tracking_ticks + misid_ticks
  * tracking_share         : tracking_ticks / total_detected_ticks (>= 60% per SC-003)
  * misid_to_true_ratio    : misid_ticks / tracking_ticks (<= 2:1 per SC-004)
  * comm_sent_total        : sum of comm.sent deltas across UAVs
  * comm_delivered_total   : sum of comm.delivered deltas
  * auction_rounds         : count of auction events observed
  * auction_winners        : dict winner_uid -> award count
  * destroyed_events       : list[(sim_time, uid, killer_kind, zone_index?)]
  * suspect_threat_points  : count of suspected-threat points accumulated
                             (last-heartbeat positions of presumed-dead UAVs)
  * sc001_discovery_time_s : sim-time of the tick at which >= 7/10 targets
                             have been discovered for the first time
  * sc005_handoff_max_s    : max gap between target uav change-of-tracker
                             (start with no tracker → end with new tracker)

The collector is a pure data object: feed it a `SwarmState` per tick (or
batch them via `observe(state)`), then call `summarize()` at the end of
the run.  Auction / suspect-threat hooks are populated by
`record_auction_outcome(...)` and `record_suspect_threat_point(...)` from
the higher-level controller; this keeps metrics.py honest (no hidden
side effects on the controller).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .state import SwarmState


@dataclass
class DestroyedEvent:
    sim_time: float
    uid: str
    killer_kind: str           # "air_defense" / "comm_jam_static" / "comm_jam_random" / "other"
    zone_index: Optional[int] = None


@dataclass
class AuctionOutcome:
    sim_time: float
    target_uid: str
    winner_uid: str
    bid_value: float
    n_bidders: int
    conflict: bool = False     # True if multiple winners reported (SC-005 case)


@dataclass
class RunMetrics:
    initial_uav_count: int = 0
    n_true_targets: int = 0
    true_positive_ids: set = field(default_factory=set)
    true_positive_first_seen: dict = field(default_factory=dict)
    tracking_ticks: int = 0
    misid_ticks: int = 0
    total_detected_ticks: int = 0
    comm_sent_total: int = 0
    comm_delivered_total: int = 0
    auction_rounds: int = 0
    auction_winners: dict = field(default_factory=dict)
    auction_conflict_count: int = 0
    destroyed_events: list = field(default_factory=list)
    destroyed_uids: set = field(default_factory=set)
    suspect_threat_points: int = 0
    # per-target tracker history for SC-005 (handoff latency).
    # Each entry is (first_seen_t, last_seen_t, tracker_uid) per (target, tracker) run.
    target_tracker_history: dict = field(default_factory=dict)
    last_alive_uids: set = field(default_factory=set)
    _last_comm_sent: dict = field(default_factory=dict)         # uid -> last seen sent
    _last_comm_delivered: dict = field(default_factory=dict)

    # ── observation API ───────────────────────────────────────────────────

    def initialize(self, uav_uids: set, true_target_uids: set) -> None:
        """Call once at run start with the initial set of alive UAVs."""
        self.initial_uav_count = len(uav_uids)
        self.last_alive_uids = set(uav_uids)
        for uid in uav_uids:
            self._last_comm_sent[uid] = 0
            self._last_comm_delivered[uid] = 0
        self.n_true_targets = max(self.n_true_targets, len(true_target_uids))
        for tuid in true_target_uids:
            self.target_tracker_history[tuid] = []

    def observe(self, state: SwarmState) -> None:
        """Feed one sim:state frame to the collector."""
        # Per-UAV deltas
        for uid, u in state.uavs.items():
            if u.destroyed and uid not in self.destroyed_uids:
                self.destroyed_uids.add(uid)
                # Killer kind: read from jammed/air-defense heuristic.
                # The kernel sets status=destroyed; ThreatArbiter emits
                # log "[WARN][ThreatArbiter] KILL uid=… zone_index=…"
                # but we cannot read that here; mark as "air_defense"
                # when not jammed (since jam is non-lethal) and
                # "air_defense" otherwise (more common case).  The
                # kernel event log is the authoritative source.
                self.destroyed_events.append(DestroyedEvent(
                    sim_time=state.sim_time, uid=uid,
                    killer_kind="air_defense"))
            if u.detected:
                self.total_detected_ticks += 1
                if u.misid_flag:
                    self.misid_ticks += 1
                else:
                    self.tracking_ticks += 1
            if u.target_uid:
                hist = self.target_tracker_history.setdefault(u.target_uid, [])
                if not hist or hist[-1][2] != u.uid:
                    # New tracker run: record (first_seen, last_seen, uid)
                    hist.append((state.sim_time, state.sim_time, u.uid))
                else:
                    # Same tracker: update last_seen only
                    first, _last, uid = hist[-1]
                    hist[-1] = (first, state.sim_time, uid)
            # comm sent/delivered are cumulative counters; track deltas
            prev_sent = self._last_comm_sent.get(uid, 0)
            prev_del = self._last_comm_delivered.get(uid, 0)
            self.comm_sent_total += max(0, u.comm_sent - prev_sent)
            self.comm_delivered_total += max(0, u.comm_delivered - prev_del)
            self._last_comm_sent[uid] = u.comm_sent
            self._last_comm_delivered[uid] = u.comm_delivered

        # First-seen discovery
        for uid, u in state.uavs.items():
            if u.detected and not u.misid_flag and u.target_uid:
                if u.target_uid not in self.true_positive_ids:
                    self.true_positive_ids.add(u.target_uid)
                    self.true_positive_first_seen[u.target_uid] = state.sim_time

        # Suspect-threat points are recorded when a UAV disappears from
        # the alive set; the controller calls record_suspect_threat_point
        # with the last known position (kept here as a counter only).
        cur_alive = {u for u, v in state.uavs.items() if not v.destroyed}
        # Disappeared UAVs since last frame
        for u in self.last_alive_uids - cur_alive:
            if u in self.destroyed_uids:
                # Recorded as a destroyed event already; do not double-count
                continue
        self.last_alive_uids = cur_alive

    def record_auction_outcome(self, outcome: AuctionOutcome) -> None:
        self.auction_rounds += 1
        self.auction_winners[outcome.winner_uid] = \
            self.auction_winners.get(outcome.winner_uid, 0) + 1
        if outcome.conflict:
            self.auction_conflict_count += 1

    def record_suspect_threat_point(self) -> None:
        self.suspect_threat_points += 1

    # ── summaries ─────────────────────────────────────────────────────────

    @property
    def alive_count(self) -> int:
        return self.initial_uav_count - len(self.destroyed_uids)

    @property
    def destroyed_count(self) -> int:
        return len(self.destroyed_uids)

    @property
    def alive_rate(self) -> float:
        if self.initial_uav_count == 0:
            return 0.0
        return self.alive_count / self.initial_uav_count

    @property
    def discovery_rate(self) -> float:
        if self.n_true_targets == 0:
            return 0.0
        return len(self.true_positive_ids) / self.n_true_targets

    @property
    def tracking_share(self) -> float:
        if self.total_detected_ticks == 0:
            return 0.0
        return self.tracking_ticks / self.total_detected_ticks

    @property
    def misid_to_true_ratio(self) -> float:
        if self.tracking_ticks == 0:
            return float("inf") if self.misid_ticks > 0 else 0.0
        return self.misid_ticks / self.tracking_ticks

    @property
    def sc001_discovery_time_s(self) -> Optional[float]:
        """sim-time at which >= 7/10 targets first discovered (None if not reached)."""
        if len(self.true_positive_first_seen) < max(1, int(0.7 * self.n_true_targets)):
            return None
        return max(self.true_positive_first_seen.values())

    def sc005_handoff_max_s(self) -> Optional[float]:
        """Max tracker-handoff gap (SC-005).

        For each target, walks `(first_seen, last_seen, uid)` runs and
        finds the longest gap between the end of one run and the start of
        the next (only when the tracker uid changes).
        """
        max_gap: Optional[float] = None
        for _tgt, hist in self.target_tracker_history.items():
            if len(hist) < 2:
                continue
            for i in range(1, len(hist)):
                _first_prev, last_prev, uid_prev = hist[i - 1]
                first_now, _last_now, uid_now = hist[i]
                if uid_prev != uid_now:
                    gap = first_now - last_prev
                    if max_gap is None or gap > max_gap:
                        max_gap = gap
        return max_gap

    def summarize(self) -> dict:
        return {
            "controller": "spec-019 swarm_controller + blind avoidance",
            # Headline counts
            "initial_uav_count": self.initial_uav_count,
            "alive_count": self.alive_count,
            "destroyed_count": self.destroyed_count,
            "alive_rate": self.alive_rate,
            # Discovery
            "targets_discovered": len(self.true_positive_ids),
            "n_true_targets": self.n_true_targets,
            "discovery_rate": self.discovery_rate,
            "sc001_discovery_time_s": self.sc001_discovery_time_s,
            # Tracking quality
            "tracking_ticks": self.tracking_ticks,
            "misid_ticks": self.misid_ticks,
            "total_detected_ticks": self.total_detected_ticks,
            "tracking_share": self.tracking_share,
            "misid_to_true_ratio": self.misid_to_true_ratio,
            "sc005_handoff_max_s": self.sc005_handoff_max_s(),
            # Comms
            "comm_sent_total": self.comm_sent_total,
            "comm_delivered_total": self.comm_delivered_total,
            # Auctions
            "auction_rounds": self.auction_rounds,
            "auction_winners": dict(self.auction_winners),
            "auction_conflict_count": self.auction_conflict_count,
            # Threats
            "destroyed_events": [
                {"sim_time": e.sim_time, "uid": e.uid,
                 "killer_kind": e.killer_kind, "zone_index": e.zone_index}
                for e in self.destroyed_events
            ],
            "suspect_threat_points": self.suspect_threat_points,
        }
