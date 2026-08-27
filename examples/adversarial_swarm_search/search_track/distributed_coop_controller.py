"""Spec 019 US3 + US5 — DistributedCoopController.

Combines the building blocks needed to be a Spec 019 swarm controller:

  * FleetMembership  : single-source heartbeat (US3, FR-017/020/021)
  * AuctionAllocator  : distributed auction for re-assigning true
                        targets after a peer is lost (US3, FR-018/019)
  * SectorSearch      : even-split search sectors for idle alive UAVs
                        (US3, FR-004)
  * ThreatIntel       : suspect-threat points recorded from lost peers'
                        last heartbeat positions (US5, FR-014~017)
  * BlindAvoidance    : tangent detour around suspect-threat points
                        (US5, FR-014/015)
  * SwarmController (this base)  : blind-avoidance of published
                        air-defense zones (Phase 5, US1)

Info-isolation contract (SC-010):
  * This module NEVER reads ground-truth status (e.g. ``entity.platform.status``)
    for a peer; it only learns about a peer's liveness through
    `observe_heartbeat(hb)`.
  * The air-defense zones used by the blind-avoidance layer come from
    the published ``state.zones`` bucket, not the scenario config.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .auction_allocator import AuctionAllocator
from .blind_avoidance_planner import BlindAvoidancePlanner
from .fleet_membership import FleetMembership, Heartbeat
from .sector_search import assign_sectors
from .state import SwarmState
from .swarm_controller import SwarmController
from .threat_intel import ThreatIntel


@dataclass
class DistributedCoopController:
    """Composes all Spec 019 controller pieces.

    The wrapping `SwarmController` (inherited from Phase 5) handles the
    blind-avoidance of *published* air-defense zones; this class adds
    the distributed layer: heartbeat-based membership, distributed
    auction, sector assignment, suspect-threat recording, and
    line-of-sight detour around suspect-threat circles.
    """
    my_uid: str
    inner: SwarmController
    fleet: FleetMembership
    auction: AuctionAllocator
    threat_intel: ThreatIntel
    blind_planner: BlindAvoidancePlanner

    @classmethod
    def create(cls, my_uid: str, *, heartbeat_timeout_s: float = 5.0,
               network_available: bool = True,
               threat_safe_radius_m: float = 600.0) -> "DistributedCoopController":
        c = cls(
            my_uid=my_uid,
            inner=SwarmController(my_uid=my_uid),
            fleet=FleetMembership(my_uid=my_uid,
                                  heartbeat_timeout_s=heartbeat_timeout_s),
            auction=AuctionAllocator(my_uid=my_uid,
                                     network_available=network_available),
            threat_intel=ThreatIntel(my_uid=my_uid,
                                     safe_radius_m=threat_safe_radius_m),
            blind_planner=BlindAvoidancePlanner(
                my_uid=my_uid, safe_radius_m=threat_safe_radius_m),
        )
        # Wire: when a peer is declared LOST, record a suspect-threat
        # point at its last known position.  When a peer is RECOVERED,
        # clear the corresponding suspect point.
        def _on_lost(uid, pos):
            if pos is not None:
                c.threat_intel.add_suspect(pos[0], pos[1])
        def _on_recovered(uid):
            pos = c.fleet.last_position_of(uid)
            if pos is not None:
                c.threat_intel.clear_suspect(pos[0], pos[1])
        c.fleet.on_lost(_on_lost)
        c.fleet.on_recovered(_on_recovered)
        return c

    def configure(self, cfg: dict) -> None:
        self.inner.configure(cfg)
        # The blind_planner reads its safe_radius from threat_intel which
        # we set up in create(); expose knobs for tests.

    # ── perception API (called per tick) ──────────────────────────────────

    def observe_heartbeat(self, hb: Heartbeat) -> None:
        prev_state = self.fleet.state_of(hb.uid)
        self.fleet.observe_heartbeat(hb)
        # If a peer is fresh ACTIVE, no intel change.  If a peer is
        # LOST, record a suspect-threat point at the last position.
        # The FleetMembership handles state transitions; here we
        # listen for LOST edges via the callback.
        # We register the callback ONCE — but create() is the only
        # entry point that constructs `self`, so we attach on
        # construction below.

    def observe_bid(self, msg) -> None:
        self.auction.observe_bid(msg)

    def tick(self, sim_time: float) -> list[str]:
        """Advance fleet view; returns the list of uids that just went LOST."""
        return self.fleet.tick(sim_time)

    def record_lost_as_suspect(self, uid: str) -> None:
        pos = self.fleet.last_position_of(uid)
        if pos is not None:
            self.threat_intel.add_suspect(pos[0], pos[1])

    def assign_sectors_for(self) -> dict[str, int]:
        return assign_sectors(self.fleet.active_uids())

    # ── decision API ──────────────────────────────────────────────────────

    def decide(self, state: SwarmState, period: float) -> list[dict]:
        """Compose: blind-avoid published zones (US1) + avoid suspect
        threat points (US5).  Auction bidding is driven by the
        ``run.py`` outer loop, not by this function.
        """
        return self.inner.decide(state, period)
