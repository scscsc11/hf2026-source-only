"""Spec 019 US3 — FleetMembership single-source heartbeat (FR-017, FR-020, FR-021).

Tests:
  * Only the absence of a recent heartbeat marks a peer as LOST — never
    the peer's status field (info-isolation, SC-010).
  * When a peer resumes sending heartbeats, the lost callback is reversed
    (peer returns to ACTIVE).
  * heartbeat_timeout_s is honoured: an ACTIVE peer that stops sending
    for longer than the threshold is marked LOST.
  * Multi-peer independence: one peer going lost does not affect the rest.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.fleet_membership import (  # noqa: E402
    FleetMembership, Heartbeat, PeerState,
)


class FleetMembershipBasicTest(unittest.TestCase):
    def test_initial_all_active(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99))
        self.assertEqual(fm.state_of("u2"), PeerState.ACTIVE)

    def test_lost_after_timeout(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99))
        # t=4: still ACTIVE
        fm.tick(sim_time=4.0)
        self.assertEqual(fm.state_of("u2"), PeerState.ACTIVE)
        # t=6: beyond timeout -> LOST
        fm.tick(sim_time=6.0)
        self.assertEqual(fm.state_of("u2"), PeerState.LOST)

    def test_heartbeat_resets_timer(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99))
        fm.tick(sim_time=3.0)
        # u2 sends another heartbeat at t=3 (just before timeout)
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=3.0,
                                       lat=27.01, lon=125.0))
        # t=7: previous timeout was t=5; new deadline is 3+5=8 -> still ACTIVE
        fm.tick(sim_time=7.0)
        self.assertEqual(fm.state_of("u2"), PeerState.ACTIVE)

    def test_recovery_after_lost(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99))
        fm.tick(sim_time=10.0)  # LOST
        self.assertEqual(fm.state_of("u2"), PeerState.LOST)
        # u2 comes back
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=11.0,
                                       lat=27.01, lon=125.0))
        self.assertEqual(fm.state_of("u2"), PeerState.ACTIVE)

    def test_independent_peers(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        for uid in ("u2", "u3", "u4"):
            fm.observe_heartbeat(Heartbeat(uid=uid, sim_time=0.0,
                                           lat=27.0, lon=124.99))
        # u2 keeps heart-beating; u3 goes silent; u4 also silent
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=3.0,
                                       lat=27.0, lon=124.99))
        fm.tick(sim_time=6.0)
        self.assertEqual(fm.state_of("u2"), PeerState.ACTIVE)
        self.assertEqual(fm.state_of("u3"), PeerState.LOST)
        self.assertEqual(fm.state_of("u4"), PeerState.LOST)

    def test_status_field_does_not_influence_membership(self):
        """Info-isolation: passing status='destroyed' must NOT trigger LOST.

        The FleetMembership API must accept a status field on the heartbeat
        and ignore it — loss is decided purely by heartbeat freshness.
        """
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        hb = Heartbeat(uid="u2", sim_time=0.0, lat=27.0, lon=124.99,
                       status="destroyed")
        fm.observe_heartbeat(hb)
        # Even though status is "destroyed", the heartbeat IS recent so the
        # peer should be considered ACTIVE — the algorithm may not read
        # status to decide membership.
        fm.tick(sim_time=1.0)
        self.assertEqual(fm.state_of("u2"), PeerState.ACTIVE)

    def test_lost_callback_fires_once(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        fired = []
        fm.on_lost(lambda uid, pos: fired.append(uid))
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99))
        fm.tick(sim_time=6.0)
        fm.tick(sim_time=7.0)
        fm.tick(sim_time=8.0)
        # Even across multiple ticks, the callback should only fire once
        # until the peer recovers.
        self.assertEqual(fired, ["u2"])

    def test_recovered_callback_fires(self):
        fm = FleetMembership(my_uid="u1", heartbeat_timeout_s=5.0)
        recovered = []
        fm.on_recovered(lambda uid: recovered.append(uid))
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=0.0,
                                       lat=27.0, lon=124.99))
        fm.tick(sim_time=10.0)  # LOST
        fm.observe_heartbeat(Heartbeat(uid="u2", sim_time=11.0,
                                       lat=27.01, lon=125.0))
        self.assertEqual(recovered, ["u2"])


if __name__ == "__main__":
    unittest.main()
