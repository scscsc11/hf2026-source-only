"""Spec 019 US3 (FR-018, FR-019, FR-020, FR-021) — AuctionAllocator.

Distributed auction for re-assigning TRUE-target tracking when a peer is
declared LOST (or when a new true target is discovered).  Only true
targets are auctioned — search sectors are NOT auctioned, they are
evenly split among idle ALIVE UAVs (handled in sector_search.py).

Per FR-018:

  * Each survivor observes a `Bid` from every other survivor.  Bids are
    computed locally based on the bidder's own view of distance, current
    task load, and threat cost.  The allocator picks the highest bid as
    the winner.
  * Auctions are keyed by `auction_id` (UUID-like, monotonically
    increasing per (target, round)).  Two concurrent auctions on
    different targets do not interfere.
  * FR-020 conflict arbitration: if two survivors both report winning
    in a re-broadcast round (e.g. messages collided), the next round
    re-tabulates from the combined bid set and emits a single winner
    with `conflict=False`.  A round that ends in a tie is flagged
    `conflict=True` so the higher layer can immediately request a
    re-broadcast.
  * FR-021: when a previously-lost peer recovers, the next round on the
    same target treats the recovered peer as a fresh bidder — the
    previous winner is NOT sticky.  Higher bids win regardless of
    "previous owner".
  * Communication degradation: when the local node's comm is jammed
    (e.g. `network_available=False`), incoming bids are ignored and
    `outcome_for()` returns None (deferred).  The higher layer is
    expected to retry on the next tick.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class AllocatorState(str, enum.Enum):
    PENDING = "pending"   # auction started, no bids yet OR network down
    RESOLVED = "resolved"  # single winner
    CONFLICT = "conflict"  # tie in this round; need re-broadcast


@dataclass
class AuctionMessage:
    kind: str                 # "bid" | "outcome" | "request_rebroadcast"
    auction_id: str
    round: int
    target_uid: str
    bidder_uid: str = ""
    bid_value: float = 0.0
    n_active_peers: int = 0


@dataclass
class AuctionOutcome:
    auction_id: str
    target_uid: str
    round: int
    winner_uid: str
    bid_value: float
    n_bidders: int
    conflict: bool = False

    def to_auction_message(self) -> AuctionMessage:
        return AuctionMessage(
            kind="outcome",
            auction_id=self.auction_id,
            round=self.round,
            target_uid=self.target_uid,
            bidder_uid=self.winner_uid,
            bid_value=self.bid_value,
            n_active_peers=self.n_bidders,
        )


class Bid:
    """Helper that computes a local bid for a (target, self) pair.

    The bid function is intentionally simple here — production code can
    plug in a richer bid function via `AuctionAllocator.set_bid_fn`.
    """
    @staticmethod
    def default(self_lat: float, self_lon: float,
                target_lat: float, target_lon: float,
                current_task_load: int,
                threat_cost: float) -> float:
        # Inverse distance + load penalty + threat penalty
        d = max(1.0, _haversine(self_lat, self_lon, target_lat, target_lon))
        return 1.0 / (d / 1000.0) - 0.1 * current_task_load - 0.5 * threat_cost


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Quick haversine in metres (matches the kernel's approximation)."""
    import math
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


class AuctionAllocator:
    def __init__(self, my_uid: str, network_available: bool = True) -> None:
        self.my_uid = my_uid
        self._network = network_available
        # auction_id -> {round, target, bids: {bidder_uid: bid_value}}
        self._auctions: dict[str, dict] = {}
        self._outcomes: dict[str, AuctionOutcome] = {}

    def set_network_available(self, ok: bool) -> None:
        self._network = ok

    def start_auction(self, target_uid: str, auction_id: str, round: int) -> None:
        """Begin (or re-open) an auction.  Each call must carry the round
        number so re-broadcasts can be distinguished.
        """
        if auction_id not in self._auctions:
            self._auctions[auction_id] = {
                "target": target_uid,
                "round": round,
                "bids": {},
            }
        else:
            # Re-opening for a new round — KEEP existing bids so the
            # arbitration can compare across rounds (FR-020).
            self._auctions[auction_id]["round"] = round
        # Drop any previous outcome; we're re-deciding.
        self._outcomes.pop(auction_id, None)

    def observe_bid(self, msg: AuctionMessage) -> None:
        if not self._network:
            return
        if msg.kind != "bid":
            return
        if msg.auction_id not in self._auctions:
            return
        if msg.bidder_uid == self.my_uid and msg.bid_value == 0.0:
            return
        self._auctions[msg.auction_id]["bids"][msg.bidder_uid] = msg.bid_value
        # Invalidate any previously-resolved outcome; new bids may shift.
        self._outcomes.pop(msg.auction_id, None)

    def mark_conflict(self, auction_id: str) -> None:
        if auction_id not in self._auctions:
            return
        # Flag the current outcome (if any) as conflict; higher layer
        # is expected to call start_auction with a higher `round` to
        # trigger a re-broadcast.
        out = self._resolve_one_(auction_id)
        if out is not None:
            out.conflict = True
            self._outcomes[auction_id] = out

    def state_of(self, auction_id: str) -> AllocatorState:
        if auction_id not in self._auctions:
            return AllocatorState.PENDING
        bids = self._auctions[auction_id]["bids"]
        if not bids:
            return AllocatorState.PENDING
        # If all bids are equal, that's a conflict.
        vals = list(bids.values())
        if len(vals) >= 2 and max(vals) == min(vals):
            return AllocatorState.CONFLICT
        return AllocatorState.RESOLVED

    def outcome_for(self, auction_id: str) -> Optional[AuctionOutcome]:
        if auction_id not in self._auctions:
            return None
        if not self._network:
            return None
        if auction_id in self._outcomes:
            return self._outcomes[auction_id]
        out = self._resolve_one_(auction_id)
        if out is not None:
            self._outcomes[auction_id] = out
        return out

    def _resolve_one_(self, auction_id: str) -> Optional[AuctionOutcome]:
        a = self._auctions[auction_id]
        bids: dict = a["bids"]
        if not bids:
            return None
        vals = list(bids.values())
        max_bid = max(vals)
        winners = [uid for uid, b in bids.items() if b == max_bid]
        # Single winner -> resolved; multiple winners -> conflict, but
        # the OUTCOME object still reports a winner for the higher layer
        # to use as a tie-breaker.
        conflict = len(winners) > 1
        winner = winners[0]
        return AuctionOutcome(
            auction_id=auction_id,
            target_uid=a["target"],
            round=a["round"],
            winner_uid=winner,
            bid_value=max_bid,
            n_bidders=len(bids),
            conflict=conflict,
        )
