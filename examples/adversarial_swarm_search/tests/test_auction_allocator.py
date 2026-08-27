"""Spec 019 US3 — AuctionAllocator (FR-018, FR-019, FR-020, FR-021).

Tests:
  * Bids produce a single winner (highest bid wins).
  * Auction id isolation — two concurrent auctions don't cross-contaminate.
  * Conflict arbitration: when two nodes both think they won the same
    auction_id in a re-broadcast round, the next round picks a fresh
    winner from the combined bid set.
  * Communication degradation: if the network is jammed, the allocator
    returns "deferred" rather than raising.
  * FR-021: when a peer re-joins after being marked lost, the new round
    lets the recovered peer re-compete for the same target.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.auction_allocator import (  # noqa: E402
    AuctionAllocator, AuctionMessage, AuctionOutcome, Bid,
    AllocatorState,
)


def _bid(bidder: str, target: str, value: float,
         auction_id: str = "a1") -> AuctionMessage:
    return AuctionMessage(kind="bid", auction_id=auction_id, round=1,
                          target_uid=target, bidder_uid=bidder,
                          bid_value=value, n_active_peers=2)


class AuctionAllocatorBasicTest(unittest.TestCase):
    def test_no_bids_yields_no_winner(self):
        a = AuctionAllocator(my_uid="u1")
        a.start_auction("t1", auction_id="a1", round=1)
        self.assertIsNone(a.outcome_for("a1"))

    def test_highest_bid_wins(self):
        a = AuctionAllocator(my_uid="u1")
        a.start_auction("t1", auction_id="a1", round=1)
        a.observe_bid(_bid("u1", "t1", 0.5))
        a.observe_bid(_bid("u2", "t1", 0.9))
        out = a.outcome_for("a1")
        self.assertIsNotNone(out)
        self.assertEqual(out.winner_uid, "u2")
        self.assertAlmostEqual(out.bid_value, 0.9)
        self.assertFalse(out.conflict)

    def test_single_bid_wins(self):
        a = AuctionAllocator(my_uid="u1")
        a.start_auction("t1", auction_id="a1", round=1)
        a.observe_bid(_bid("u1", "t1", 0.3))
        out = a.outcome_for("a1")
        self.assertEqual(out.winner_uid, "u1")
        self.assertFalse(out.conflict)

    def test_auction_id_isolation(self):
        a = AuctionAllocator(my_uid="u1")
        a.start_auction("t1", auction_id="a1", round=1)
        a.start_auction("t2", auction_id="a2", round=1)
        a.observe_bid(_bid("u1", "t1", 0.5, auction_id="a1"))
        a.observe_bid(_bid("u2", "t1", 0.7, auction_id="a1"))  # higher for t1
        a.observe_bid(_bid("u1", "t2", 0.9, auction_id="a2"))
        out1 = a.outcome_for("a1")
        out2 = a.outcome_for("a2")
        self.assertEqual(out1.winner_uid, "u2")
        self.assertEqual(out2.winner_uid, "u1")

    def test_conflict_in_rebroadcast(self):
        """If two nodes both report winning in a re-broadcast, the allocator
        must re-tally bids and pick the higher.  FR-020: 'in a new round,
        arbitrate and converge'.
        """
        a = AuctionAllocator(my_uid="u1")
        a.start_auction("t1", auction_id="a1", round=1)
        a.observe_bid(_bid("u1", "t1", 0.5))
        a.observe_bid(_bid("u2", "t1", 0.7))
        # Both nodes think they won (e.g. due to message collision).  Start
        # a NEW round with both bids retained, but both sides think they won.
        a.mark_conflict("a1")
        out_round1 = a.outcome_for("a1")
        self.assertTrue(out_round1.conflict)
        # Round 2 — re-broadcast
        a.start_auction("t1", auction_id="a1", round=2)
        # u2 raises bid
        a.observe_bid(AuctionMessage(kind="bid", auction_id="a1", round=2,
                                     target_uid="t1", bidder_uid="u2",
                                     bid_value=0.95, n_active_peers=2))
        a.observe_bid(AuctionMessage(kind="bid", auction_id="a1", round=2,
                                     target_uid="t1", bidder_uid="u1",
                                     bid_value=0.6, n_active_peers=2))
        out_round2 = a.outcome_for("a1")
        self.assertEqual(out_round2.winner_uid, "u2")
        self.assertAlmostEqual(out_round2.bid_value, 0.95)
        self.assertFalse(out_round2.conflict)

    def test_network_jammed_returns_deferred(self):
        a = AuctionAllocator(my_uid="u1", network_available=False)
        a.start_auction("t1", auction_id="a1", round=1)
        a.observe_bid(_bid("u1", "t1", 0.5))
        # Cannot collect from u2: state remains PENDING.
        self.assertEqual(a.state_of("a1"), AllocatorState.PENDING)
        # When network returns, can collect again.
        a.set_network_available(True)
        a.observe_bid(_bid("u2", "t1", 0.9))
        out = a.outcome_for("a1")
        self.assertEqual(out.winner_uid, "u2")

    def test_recovered_peer_can_recompete(self):
        """FR-021: previously-lost peer recovers, gets to bid in a fresh
        round for the same target.  The recovered peer's bid is
        considered alongside others; higher bid wins regardless of
        'previous owner' tag.
        """
        a = AuctionAllocator(my_uid="u1")
        a.start_auction("t1", auction_id="a1", round=1)
        a.observe_bid(_bid("u1", "t1", 0.5))  # u1 wins round 1
        out = a.outcome_for("a1")
        self.assertEqual(out.winner_uid, "u1")
        # u2 (recovered) bids higher in a fresh round
        a.start_auction("t1", auction_id="a1", round=2)
        a.observe_bid(AuctionMessage(kind="bid", auction_id="a1", round=2,
                                     target_uid="t1", bidder_uid="u2",
                                     bid_value=0.95, n_active_peers=2))
        a.observe_bid(AuctionMessage(kind="bid", auction_id="a1", round=2,
                                     target_uid="t1", bidder_uid="u1",
                                     bid_value=0.5, n_active_peers=2))
        out2 = a.outcome_for("a1")
        self.assertEqual(out2.winner_uid, "u2")
        self.assertFalse(out2.conflict)


if __name__ == "__main__":
    unittest.main()
