"""Spec 019 problem-4 acceptance test — cooperative search coverage.

Validates the reworked SwarmController actually drives the fleet to scan
the whole area and discover + track real targets, instead of loitering
in place (the original bug: decide() returned [] when no avoidance was
needed, so UAVs never moved).

Scenario (deterministic, no Redis, no sim):
  * 10 UAVs clustered at a takeoff point.
  * 10 stationary real targets spread across a 5 km box.
  * 20 decoys (ignored by the tracker — must not count as discoveries).
  * No air-defense zones (pure coverage test).

Simulator model:
  * Each tick the runner "flies" each UAV towards its last commanded
    set_destination at a fixed cruise speed. When a UAV gets within
    ``acquire_range_m`` of a real target, the synthetic gimbal reports
    detected=True so the controller locks on.
  * sim_time advances by ``period`` each tick (decoupled from wall time
    so the test runs in milliseconds, not 10 minutes).

Acceptance goals (from the user's requirement #4):
  * Fleet covers >=50% of the search box (waypoints land in >=50% of a
    coarse grid).
  * >=50% of real targets discovered within 600 sim-seconds.
  * Each discovered target accumulates >=120 s of cumulative track.
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE_DIR = HERE.parent
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from search_track.swarm_controller import SwarmController  # noqa: E402
from search_track.state import (  # noqa: E402
    SwarmState, UavView, GroundView,
)


# ── scenario constants ─────────────────────────────────────────────────────

CENTER_LAT, CENTER_LON = 27.0, 125.0
N_UAV = 10
N_TARGET = 10
N_DECOY = 20
SEARCH_RADIUS_M = 2500.0
ACQUIRE_RANGE_M = 600.0
CRUISE_SPEED_MPS = 60.0        # how fast the runner flies UAVs to cmds
CONTROL_PERIOD_S = 1.0 / 10.0  # 10 Hz
SIM_DURATION_S = 600.0


def _haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def _destination(lat, lon, bearing_deg, dist_m):
    R = 6_371_000.0
    delta = dist_m / R
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    cd, sd = math.cos(delta), math.sin(delta)
    phi2 = math.asin(math.sin(phi1) * cd + math.cos(phi1) * sd * math.cos(theta))
    lam2 = lam1 + math.atan2(math.sin(theta) * sd * math.cos(phi1),
                             cd - math.sin(phi1) * math.sin(phi2))
    return math.degrees(phi2), ((math.degrees(lam2) + 540.0) % 360.0) - 180.0


def _build_targets():
    """10 real targets evenly spaced on a ring around the centre."""
    out = {}
    for i in range(N_TARGET):
        brng = 360.0 * i / N_TARGET
        lat, lon = _destination(CENTER_LAT, CENTER_LON, brng, 1800.0)
        out[f"T{i:03d}"] = GroundView(uid=f"T{i:03d}", name=f"target_{i}",
                                       latitude=lat, longitude=lon)
    return out


def _build_decoys():
    """20 decoys scattered (must NOT be counted as discoveries)."""
    out = {}
    for i in range(N_DECOY):
        brng = 17.0 * i + 5.0
        lat, lon = _destination(CENTER_LAT, CENTER_LON, brng, 1200.0)
        out[f"D{i:03d}"] = GroundView(uid=f"D{i:03d}", name=f"decoy_{i}",
                                       latitude=lat, longitude=lon,
                                       is_decoy=True)
    return out


def _build_uavs():
    out = {}
    for i in range(N_UAV):
        uid = f"U{i:03d}"
        out[uid] = UavView(uid=uid, name=f"uav_{i}",
                           latitude=CENTER_LAT, longitude=CENTER_LON,
                           altitude=600.0)
    return out


class CooperativeSearchCoverageTest(unittest.TestCase):
    """Drives the full 10-UAV fleet for 600 sim-seconds and checks the
    discovery + track goals."""

    def _make_cfg(self):
        return {
            "search_radius": SEARCH_RADIUS_M,
            "search_altitude_agl": 600.0,
            "expand_time": 40.0,
            "sector_angular_speed_dps": 40.0,
            "initial_radius_frac": 0.1,
            "radius_dither_frac": 0.08,
            "sector_center_latitude": CENTER_LAT,
            "sector_center_longitude": CENTER_LON,
            "blind_avoidance_enabled": False,  # no zones in this test
            "avoidance_margin_m": 0.0,
            "advanced": {"acquire_range_m": ACQUIRE_RANGE_M},
        }

    def _fly_towards(self, uav, dest_lat, dest_lon, dist_m):
        """Move ``uav`` towards (dest) by at most ``dist_m`` metres."""
        d = _haversine_m(uav.latitude, uav.longitude, dest_lat, dest_lon)
        if d <= dist_m or d < 1e-6:
            uav.latitude = dest_lat
            uav.longitude = dest_lon
            return
        # bearing uav -> dest
        phi1, phi2 = math.radians(uav.latitude), math.radians(dest_lat)
        dl = math.radians(dest_lon - uav.longitude)
        y = math.sin(dl) * math.cos(phi2)
        x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
        brng = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
        lat, lon = _destination(uav.latitude, uav.longitude, brng, dist_m)
        uav.latitude = lat
        uav.longitude = lon

    def _run_simulation(self):
        uavs = _build_uavs()
        targets = _build_targets()
        decoys = _build_decoys()
        cfg = self._make_cfg()
        alive = sorted(uavs)
        controllers = {}
        for idx, uid in enumerate(alive):
            sc = SwarmController(my_uid=uid)
            sc.configure(cfg)
            sc.set_fleet_index(idx, len(alive))
            sc.set_sector_center(CENTER_LAT, CENTER_LON)
            controllers[uid] = sc

        n_ticks = int(SIM_DURATION_S / CONTROL_PERIOD_S)
        per_tick_dist = CRUISE_SPEED_MPS * CONTROL_PERIOD_S
        waypoint_samples = []  # (lat, lon) of every commanded destination
        discovered_history = []  # set of discovered target uids per second

        for tick in range(n_ticks):
            sim_t = tick * CONTROL_PERIOD_S
            # Update detection flags: a UAV "detects" iff within acquire
            # range of ANY real target. The controller then picks the
            # nearest unclaimed real target.
            for uid, u in uavs.items():
                u.detected = False
                for t in targets.values():
                    if _haversine_m(u.latitude, u.longitude,
                                     t.latitude, t.longitude) <= ACQUIRE_RANGE_M:
                        u.detected = True
                        break
            state = SwarmState(sim_time=sim_t, uavs=uavs, targets=targets,
                                decoys=decoys, zones=[])
            for uid, sc in controllers.items():
                cmds = sc.decide(state, CONTROL_PERIOD_S)
                # Apply the set_destination by flying the UAV towards it.
                for c in cmds:
                    if c["cmd"] == "set_destination":
                        p = c["params"]
                        self._fly_towards(uavs[uid], p["latitude"],
                                          p["longitude"], per_tick_dist)
                        waypoint_samples.append((p["latitude"], p["longitude"]))
            # Cross-controller cooperation: when a UAV is tracking T, tell
            # peers (simulating the broadcast).
            for sc in controllers.values():
                if sc._tracked_uid:
                    for other in controllers.values():
                        if other.my_uid != sc.my_uid:
                            other.observe_peer_tracking(sc.my_uid, sc._tracked_uid)
            if tick % 10 == 0:  # once per sim-second
                disc = set()
                for sc in controllers.values():
                    disc |= sc.discovered_targets
                discovered_history.append((sim_t, disc))

        return uavs, targets, controllers, waypoint_samples, discovered_history

    def test_discovers_at_least_half_of_targets_within_10min(self):
        _, _, controllers, _, history = self._run_simulation()
        # Find the first sim_time at which >=50% were discovered.
        half = N_TARGET // 2
        first_t = None
        for t, disc in history:
            real_disc = disc & set(f"T{i:03d}" for i in range(N_TARGET))
            if len(real_disc) >= half:
                first_t = t
                break
        self.assertIsNotNone(first_t,
                             "Never discovered >=50% of real targets")
        self.assertLessEqual(first_t, SIM_DURATION_S,
                             f"Discovery of 50% happened at {first_t}s > 600s")

    def test_each_discovered_target_tracked_at_least_2min(self):
        _, _, controllers, _, _ = self._run_simulation()
        # Aggregate track duration per target across all controllers.
        track_dur = {}
        for sc in controllers.values():
            for t_uid, dur in sc.track_duration_s.items():
                track_dur[t_uid] = track_dur.get(t_uid, 0.0) + dur
        discovered = set()
        for sc in controllers.values():
            discovered |= sc.discovered_targets
        discovered_real = discovered & set(f"T{i:03d}" for i in range(N_TARGET))
        self.assertGreaterEqual(len(discovered_real), N_TARGET // 2,
                                "Fewer than half of targets discovered")
        under_tracked = [t for t in discovered_real
                         if track_dur.get(t, 0.0) < 120.0]
        self.assertEqual(under_tracked, [],
                         f"Targets tracked < 120s: {under_tracked} "
                         f"(durations: "
                         f"{ {t: round(track_dur.get(t,0),1) for t in under_tracked} })")

    def test_fleet_covers_at_least_half_of_search_box(self):
        _, _, _, samples, _ = self._run_simulation()
        # Coarse 5x5 grid over the search box (±2500 m ≈ ±0.0225 deg).
        box_half_deg = SEARCH_RADIUS_M / 111_320.0
        grid_n = 5
        covered = set()
        for lat, lon in samples:
            gx = int((lon - (CENTER_LON - box_half_deg))
                     / (2 * box_half_deg) * grid_n)
            gy = int((lat - (CENTER_LAT - box_half_deg))
                     / (2 * box_half_deg) * grid_n)
            gx = max(0, min(grid_n - 1, gx))
            gy = max(0, min(grid_n - 1, gy))
            covered.add((gx, gy))
        coverage = len(covered) / (grid_n * grid_n)
        self.assertGreaterEqual(coverage, 0.5,
                                f"Fleet only covered {coverage*100:.0f}% "
                                f"of the search box (samples={len(samples)})")

    def test_decoys_never_counted_as_discoveries(self):
        _, _, controllers, _, _ = self._run_simulation()
        all_disc = set()
        for sc in controllers.values():
            all_disc |= sc.discovered_targets
        decoy_uids = set(f"D{i:03d}" for i in range(N_DECOY))
        self.assertEqual(all_disc & decoy_uids, set(),
                         "Decoys leaked into discovered_targets")


if __name__ == "__main__":
    unittest.main()
