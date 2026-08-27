"""Spec 019 US3 (FR-017, FR-020, FR-021) — FleetMembership.

Each UAV node maintains a "fleet view" of which other UAVs are ACTIVE vs
LOST based purely on the most recent **heartbeat** received from each
peer.  No ground-truth status field is read — the algorithm may not know
the kernel-side status of a peer; it can only know whether the peer has
spoken recently.

Liveness rule (FR-017 / FR-020):

  ACTIVE  ↔  (now - last_heartbeat_sim_time) <= heartbeat_timeout_s
  LOST    ↔  (now - last_heartbeat_sim_time) >  heartbeat_timeout_s

When a peer transitions ACTIVE → LOST, the `on_lost` callback fires with
the peer's uid.  When a previously-lost peer sends a new heartbeat
(recovery), the `on_recovered` callback fires and the peer returns to
ACTIVE.  Callbacks are deduplicated: a single ACTIVE→LOST edge produces
exactly one on_lost invocation, even across multiple ticks.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Optional


class PeerState(str, enum.Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    LOST = "lost"


@dataclass
class Heartbeat:
    """One peer's heartbeat message.  Includes the latest position so
    downstream code (e.g. SuspectThreatPoint) can record the last-known
    position for blind avoidance.

    `status` is a free-form string copied from the peer for diagnostic
    purposes ONLY.  FleetMembership MUST NOT use it to make liveness
    decisions (SC-010 info-isolation).
    """
    uid: str
    sim_time: float
    lat: float
    lon: float
    alt: float = 0.0
    status: str = "active"


class FleetMembership:
    """Per-node fleet view based on heartbeats."""

    def __init__(self, my_uid: str, heartbeat_timeout_s: float = 5.0) -> None:
        self.my_uid = my_uid
        self.heartbeat_timeout_s = heartbeat_timeout_s
        # uid -> (last_heartbeat_sim_time, last_pos, current_state)
        self._peers: dict[str, tuple[float, tuple[float, float, float], PeerState]] = {}
        # uid -> last_known_pos (kept on transition to LOST for downstream)
        self._last_pos: dict[str, tuple[float, float, float]] = {}
        self._on_lost: list[Callable[[str, tuple[float, float, float]], None]] = []
        self._on_recovered: list[Callable[[str], None]] = []
        self._on_state_change: list[Callable[[str, PeerState, PeerState], None]] = []

    # ── callbacks ─────────────────────────────────────────────────────────

    def on_lost(self, cb: Callable[[str, tuple[float, float, float]], None]) -> None:
        self._on_lost.append(cb)

    def on_recovered(self, cb: Callable[[str], None]) -> None:
        self._on_recovered.append(cb)

    def on_state_change(self, cb: Callable[[str, PeerState, PeerState], None]) -> None:
        self._on_state_change.append(cb)

    # ── observation API ───────────────────────────────────────────────────

    def observe_heartbeat(self, hb: Heartbeat) -> None:
        if hb.uid == self.my_uid:
            return  # never self-track
        prev = self._peers.get(hb.uid)
        new_pos = (hb.lat, hb.lon, hb.alt)
        new_state = (prev[2] if prev else PeerState.UNKNOWN)
        # A fresh heartbeat moves a peer to ACTIVE — regardless of any
        # status string the peer happened to send.  (SC-010 info-isolation.)
        new_state = PeerState.ACTIVE
        # Recovery callback only if we previously LOST this peer.
        if prev and prev[2] == PeerState.LOST:
            for cb in self._on_recovered:
                cb(hb.uid)
        old_state = prev[2] if prev else PeerState.UNKNOWN
        if old_state != new_state:
            for cb in self._on_state_change:
                cb(hb.uid, old_state, new_state)
        self._peers[hb.uid] = (hb.sim_time, new_pos, new_state)
        self._last_pos[hb.uid] = new_pos

    def tick(self, sim_time: float) -> list[str]:
        """Walk all known peers; transition any whose last heartbeat is
        older than `heartbeat_timeout_s` to LOST.  Returns the list of
        uids that just transitioned to LOST in this tick.
        """
        newly_lost: list[str] = []
        for uid, (last_t, pos, state) in list(self._peers.items()):
            if state == PeerState.LOST:
                continue
            if (sim_time - last_t) > self.heartbeat_timeout_s:
                self._peers[uid] = (last_t, pos, PeerState.LOST)
                for cb in self._on_state_change:
                    cb(uid, state, PeerState.LOST)
                for cb in self._on_lost:
                    cb(uid, pos)
                newly_lost.append(uid)
        return newly_lost

    # ── queries ───────────────────────────────────────────────────────────

    def state_of(self, uid: str) -> PeerState:
        if uid not in self._peers:
            return PeerState.UNKNOWN
        return self._peers[uid][2]

    def last_position_of(self, uid: str) -> Optional[tuple[float, float, float]]:
        return self._last_pos.get(uid)

    def active_uids(self) -> list[str]:
        return [u for u, (_, _, s) in self._peers.items()
                if s == PeerState.ACTIVE]

    def lost_uids(self) -> list[str]:
        return [u for u, (_, _, s) in self._peers.items()
                if s == PeerState.LOST]
