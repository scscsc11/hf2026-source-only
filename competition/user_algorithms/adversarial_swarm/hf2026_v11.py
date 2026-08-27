"""
HF2026 V11 — pod-based global sweep + fixed-500m threat avoidance

Built from the V10 evaluation.  V11 fixes the search-coverage architecture:
changes the two parts that still failed structurally:

1) Search is performed by 3 nearby pods sweeping the FULL runtime mission area; no spawn-cell locking.
2) Task-3 flight altitude is kept at 500 m; SAM/no-fly avoidance is horizontal.
2) Candidate association and real/decoy confirmation are stricter.
4) A task may recruit only peers that are currently inside direct radio range.
4) Followers first use a close, wide-FOV seek formation, then expand after lock.
5) Every task member sends/maintains a fresh visual lock; reporting starts only
   after all three members have repeated fresh visual confirmations.
6) report_target uses the owner's fresh local observation and no invented ID.

Participant path:
competition/user_algorithms/adversarial_swarm/hf2026_v10.py
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from competition.sdk.core.commands import (
    Command,
    broadcast,
    fly_to,
    point_gimbal,
    report_target,
    set_gimbal_fov,
)
from competition.sdk.scenarios.adversarial_swarm import SwarmAgent
from competition.sdk.scenarios.adversarial_swarm.observation import SwarmObs


M_PER_DEG_LAT = 111_320.0


# ---------------------------------------------------------------------------
# Small models
# ---------------------------------------------------------------------------

@dataclass
class Area:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    @property
    def mid_lat(self) -> float:
        return 0.5 * (self.lat_min + self.lat_max)

    @property
    def mid_lon(self) -> float:
        return 0.5 * (self.lon_min + self.lon_max)


@dataclass
class Peer:
    uid: str
    idx: int
    lat: float
    lon: float
    state: str
    last_seen: float


@dataclass
class Candidate:
    started_at: float
    last_seen: float
    lat: float
    lon: float
    seen_count: int = 1
    conf_sum: float = 0.0
    samples: List[Tuple[float, float, float]] = field(default_factory=list)
    ground_votes: int = 0
    decoy_votes: int = 0
    speed_mps: Optional[float] = None
    fit_rms_m: Optional[float] = None
    v_east: float = 0.0
    v_north: float = 0.0
    real_ready: bool = False


@dataclass
class Task:
    owner_idx: int
    seq: int
    members: Tuple[int, int, int]
    lat: float
    lon: float
    v_east: float
    v_north: float
    created_at: float
    last_update: float
    last_local_seen: float = -999.0


@dataclass
class Cooldown:
    lat: float
    lon: float
    expires_at: float


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _m_per_deg_lon(lat: float) -> float:
    return M_PER_DEG_LAT * max(0.1, math.cos(math.radians(lat)))


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    ref = 0.5 * (lat1 + lat2)
    east = (lon2 - lon1) * _m_per_deg_lon(ref)
    north = (lat2 - lat1) * M_PER_DEG_LAT
    return math.hypot(east, north)


def _offset(
    lat: float,
    lon: float,
    east_m: float,
    north_m: float,
) -> Tuple[float, float]:
    return (
        lat + north_m / M_PER_DEG_LAT,
        lon + east_m / _m_per_deg_lon(lat),
    )


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    ref = 0.5 * (lat1 + lat2)
    east = (lon2 - lon1) * _m_per_deg_lon(ref)
    north = (lat2 - lat1) * M_PER_DEG_LAT
    return math.degrees(math.atan2(east, north)) % 360.0


def _wrap180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


def _uav_index(uid: str) -> int:
    digits = "".join(re.findall(r"\d", uid))
    if digits:
        return (int(digits) - 1) % 10
    return sum(ord(c) for c in uid) % 10


def _point_in_rect(
    p: Tuple[float, float],
    r: Tuple[float, float, float, float],
) -> bool:
    lat, lon = p
    lat0, lat1, lon0, lon1 = r
    return lat0 <= lat <= lat1 and lon0 <= lon <= lon1


def _segment_intersects_rect(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    r: Tuple[float, float, float, float],
) -> bool:
    """Liang-Barsky segment/AABB test in lon-lat coordinates."""
    if _point_in_rect(p0, r) or _point_in_rect(p1, r):
        return True

    lat0, lon0 = p0
    lat1, lon1 = p1
    rlat0, rlat1, rlon0, rlon1 = r

    x0, y0, x1, y1 = lon0, lat0, lon1, lat1
    dx, dy = x1 - x0, y1 - y0

    p = (-dx, dx, -dy, dy)
    q = (x0 - rlon0, rlon1 - x0, y0 - rlat0, rlat1 - y0)

    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if abs(pi) < 1e-15:
            if qi < 0:
                return False
            continue
        t = qi / pi
        if pi < 0:
            if t > u2:
                return False
            u1 = max(u1, t)
        else:
            if t < u1:
                return False
            u2 = min(u2, t)
    return True


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class HF2026V11Agent(SwarmAgent):
    """Pod-based global search + strict reachable 3-UAV visual coalition."""

    def configure(self, config) -> None:
        self._idx = _uav_index(self.my_uid)
        self._time = 0.0
        self._state = "S"

        # Runtime area/search initialization.
        self._area: Optional[Area] = None
        self._spawn: Optional[Tuple[float, float]] = None
        self._search_cell: Optional[Tuple[int, int]] = None
        self._search_pod = -1
        self._search_slot = -1
        self._search_route: List[Tuple[float, float]] = []
        self._search_i = 0

        # Flight/sensor.
        self._base_alt = 500.0
        self._search_speed = 38.0
        self._acquire_speed = 30.0
        self._track_speed = 34.0
        self._search_fov = 50.0
        self._acquire_fov = 46.0
        self._track_fov = 46.0

        # Candidate.
        self._candidate: Optional[Candidate] = None
        self._cooldowns: List[Cooldown] = []
        self._last_local_detection: Optional[Tuple[float, float, float, float]] = None

        # Task / coalition.
        self._task: Optional[Task] = None
        self._task_seq = 0
        self._known_tasks: Dict[Tuple[int, int], Task] = {}
        self._ack_seen: Dict[int, float] = {}
        self._visual_seen: Dict[int, float] = {}
        self._visual_hits: Dict[int, int] = {}
        self._last_reselect_t = -999.0
        self._task_destroy_count_at_start = 0

        # Communication.
        self._peers: Dict[str, Peer] = {}
        self._outbox: List[str] = []
        self._next_comm_t = 0.0
        self._last_hb_t = -999.0
        self._last_task_tx_t = -999.0
        self._last_ack_tx_t = -999.0
        self._last_visual_tx_t = -999.0
        self._last_report_t = -999.0

        # Navigation.
        self._last_nav_t = -999.0
        self._last_nav_goal: Optional[Tuple[float, float]] = None
        self._last_nav_mode = ""
        self._last_nav_alt = self._base_alt
        self._no_fly_detour: Optional[Tuple[float, float]] = None  # hard-zone horizontal detour

        # Jam.
        self._jam_escape_goal: Optional[Tuple[float, float]] = None
        self._was_jammed = False

        # Score.
        self._n_destroyed = 0

    def reset(self) -> None:
        self.configure(None)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def decide(self, obs: SwarmObs, dt: float) -> List[Command]:
        self._time += max(0.0, dt)
        self._ensure_runtime_geometry(obs)

        self._update_score(obs)
        self._ingest_messages(obs)
        self._prune()
        self._update_jam(obs)

        detections = self._valid_detections(obs)

        if self._task is not None:
            self._state = "T"
            self._update_task_from_detections(detections)
            self._maintain_owner_members(obs)
            cmds = self._track_commands(obs)
            self._maybe_finish_task()

        elif self._candidate is not None:
            self._state = "A"
            self._update_candidate(detections)
            self._classify_candidate()
            self._try_launch_ready_candidate(obs)

            if self._task is not None:
                self._state = "T"
                self._update_task_from_detections(detections)
                cmds = self._track_commands(obs)
            elif self._candidate is not None:
                cmds = self._acquire_commands(obs)
            else:
                cmds = self._search_or_jam_commands(obs)

        else:
            self._state = "S"
            self._maybe_start_candidate(detections)

            if self._candidate is not None:
                self._state = "A"
                cmds = self._acquire_commands(obs)
            else:
                cmds = self._search_or_jam_commands(obs)

        self._queue_periodic_messages(obs)
        self._flush_message(cmds)
        return cmds

    # ------------------------------------------------------------------
    # Runtime mission area and search route
    # ------------------------------------------------------------------

    def _ensure_runtime_geometry(self, obs: SwarmObs) -> None:
        if self._area is not None:
            return

        a = getattr(obs.briefing, "mission_area", None)
        if a is not None:
            lat_min = float(getattr(a, "lat_min", 26.95))
            lat_max = float(getattr(a, "lat_max", 27.05))
            lon_min = float(getattr(a, "lon_min", 124.95))
            lon_max = float(getattr(a, "lon_max", 125.05))
        else:
            # Fallback matches the current enhanced runner, NOT the obsolete
            # small handbook rectangle used by V1-V8.
            lat_min, lat_max = 26.95, 27.05
            lon_min, lon_max = 124.95, 125.05

        self._area = Area(lat_min, lat_max, lon_min, lon_max)
        self._spawn = (obs.self.lat, obs.self.lon)
        self._build_pod_search_route(obs)

    def _clamp_area(
        self,
        lat: float,
        lon: float,
        margin_m: float = 180.0,
    ) -> Tuple[float, float]:
        assert self._area is not None
        a = self._area
        dlat = margin_m / M_PER_DEG_LAT
        dlon = margin_m / _m_per_deg_lon(a.mid_lat)
        return (
            min(a.lat_max - dlat, max(a.lat_min + dlat, lat)),
            min(a.lon_max - dlon, max(a.lon_min + dlon, lon)),
        )

    def _build_pod_search_route(self, obs: SwarmObs) -> None:
        """
        V11 search architecture.

        V10 mapped each aircraft to the 2x5 cell containing its *spawn*.  Several
        aircraft start in the same region, so that duplicated cells and left
        large parts of the mission area unsearched.

        V11 instead keeps aircraft in nearby search pods so any detection can
        immediately recruit two radio-reachable partners, while each pod sweeps
        one full latitude band of the global mission area.  Pod members fly
        parallel lanes ~390 m apart (>200 m proximity threshold, <1 km radio
        range) and the three/four lanes advance together as a lawnmower.
        """
        assert self._area is not None
        a = self._area

        # 3 cooperative search pods.  The extra UAV reinforces the centre band.
        pods = ((0, 1, 2), (3, 4, 5, 9), (6, 7, 8))
        pod_id = 0
        slot = 0
        members = pods[0]
        for j, pp in enumerate(pods):
            if self._idx in pp:
                pod_id = j
                members = pp
                slot = pp.index(self._idx)
                break

        self._search_pod = pod_id
        self._search_slot = slot

        # Split the complete runtime mission area into three horizontal bands.
        # Use only global-boundary margins; do NOT infer a sector from spawn.
        margin_ns_m = 260.0
        margin_ew_m = 260.0
        dlat_global = margin_ns_m / M_PER_DEG_LAT
        dlon_global = margin_ew_m / _m_per_deg_lon(a.mid_lat)

        usable_lat0 = a.lat_min + dlat_global
        usable_lat1 = a.lat_max - dlat_global
        usable_lon0 = a.lon_min + dlon_global
        usable_lon1 = a.lon_max - dlon_global

        band_h = (usable_lat1 - usable_lat0) / 3.0
        band_lat0 = usable_lat0 + pod_id * band_h
        band_lat1 = band_lat0 + band_h

        # Keep members within radio reach while covering a broad collective
        # swath.  Centre pod has four members, so use a slightly smaller gap.
        lane_gap_m = 360.0 if len(members) == 4 else 390.0
        collective_step_m = lane_gap_m * len(members)
        band_height_m = max(1.0, (band_lat1 - band_lat0) * M_PER_DEG_LAT)
        n_passes = max(2, int(math.ceil(band_height_m / collective_step_m)) + 1)

        # Keep the full pod footprint inside its band.  Without this inset the
        # outer members would clamp onto the same boundary lane on the first/last
        # pass, immediately creating <200 m proximity events.
        half_spread_m = 0.5 * (len(members) - 1) * lane_gap_m
        centre_lat0 = band_lat0 + half_spread_m / M_PER_DEG_LAT
        centre_lat1 = band_lat1 - half_spread_m / M_PER_DEG_LAT
        if centre_lat1 < centre_lat0:
            centre_lat0 = centre_lat1 = 0.5 * (band_lat0 + band_lat1)

        route: List[Tuple[float, float]] = []
        for k in range(n_passes):
            # Collective centre progresses across the whole band.
            f = k / max(1, n_passes - 1)
            centre_lat = centre_lat0 + f * (centre_lat1 - centre_lat0)

            # Symmetric member offset around the collective centre.
            offset_m = (slot - 0.5 * (len(members) - 1)) * lane_gap_m
            lat = centre_lat + offset_m / M_PER_DEG_LAT

            # All members in a pod reverse together, preserving parallelism and
            # avoiding the tangled crossing paths seen in V9/V10.
            if k % 2 == 0:
                route.extend([(lat, usable_lon0), (lat, usable_lon1)])
            else:
                route.extend([(lat, usable_lon1), (lat, usable_lon0)])

        self._search_route = route

        # Start at the nearest endpoint, but keep the route's cyclic order.
        self._search_i = min(
            range(len(route)),
            key=lambda i: _dist_m(
                obs.self.lat, obs.self.lon, route[i][0], route[i][1]
            ),
        )

    def _goal_in_hard_zone(self, obs: SwarmObs, goal: Tuple[float, float]) -> bool:
        """Avoid getting stuck forever on an unreachable search waypoint."""
        for z in getattr(obs.briefing, "approximate_zones", ()) or ():
            if getattr(z, "kind", "") not in ("air_defense", "no_fly"):
                continue
            if _point_in_rect(goal, self._zone_rect(z, pad_m=120.0)):
                return True
        return False

    def _search_goal(self, obs: SwarmObs) -> Tuple[float, float]:
        if not self._search_route:
            self._build_pod_search_route(obs)

        # Skip waypoints that lie inside hard zones.  V10 could keep flying to a
        # detour corner because the original raster endpoint itself was illegal.
        for _ in range(len(self._search_route)):
            goal = self._search_route[self._search_i]
            if self._goal_in_hard_zone(obs, goal):
                self._search_i = (self._search_i + 1) % len(self._search_route)
                continue
            if _dist_m(obs.self.lat, obs.self.lon, goal[0], goal[1]) <= 230.0:
                self._search_i = (self._search_i + 1) % len(self._search_route)
                continue
            return goal

        # Degenerate fallback if a pathological briefing covers every endpoint.
        assert self._area is not None
        return self._clamp_area(self._area.mid_lat, self._area.mid_lon, margin_m=260.0)

    def _search_or_jam_commands(self, obs: SwarmObs) -> List[Command]:
        if obs.self.jammed:
            goal = self._jam_escape(obs)
            cmds = self._navigate(obs, goal, mode="search")
        else:
            cmds = self._navigate(obs, self._search_goal(obs), mode="search")

        phase = self._time + 0.63 * self._idx
        pan = 72.0 * math.sin(2.0 * math.pi * phase / 8.4)
        tilt = -51.0 - 9.0 * math.sin(2.0 * math.pi * phase / 5.1)

        cmds.append(set_gimbal_fov(self._search_fov))
        cmds.append(point_gimbal(pan, tilt))
        return cmds

    # ------------------------------------------------------------------
    # Detection and candidate classification
    # ------------------------------------------------------------------

    def _valid_detections(self, obs: SwarmObs):
        ds = list(getattr(obs.self, "detections", ()) or ())
        if not ds:
            d = getattr(obs.self, "detection", None)
            ds = [d] if d is not None and getattr(d, "detected", False) else []

        out = []
        for d in ds:
            if d is None or not getattr(d, "detected", False):
                continue
            lat = getattr(d, "target_lat", None)
            lon = getattr(d, "target_lon", None)
            if lat is None or lon is None:
                continue
            out.append(d)
        return out

    def _best_detection(
        self,
        ds,
        ref: Optional[Tuple[float, float]] = None,
    ):
        if not ds:
            return None

        if ref is None:
            return max(
                ds,
                key=lambda d: float(getattr(d, "confidence", 0.0)),
            )

        return min(
            ds,
            key=lambda d: _dist_m(
                float(d.target_lat),
                float(d.target_lon),
                ref[0],
                ref[1],
            ),
        )

    def _in_cooldown(self, lat: float, lon: float) -> bool:
        return any(
            c.expires_at > self._time
            and _dist_m(lat, lon, c.lat, c.lon) <= 180.0
            for c in self._cooldowns
        )

    def _near_known_task(self, lat: float, lon: float) -> bool:
        for t in self._known_tasks.values():
            if (
                self._time - t.last_update <= 5.0
                and _dist_m(lat, lon, t.lat, t.lon) <= 330.0
            ):
                return True
        return False

    @staticmethod
    def _vote_type(c: Candidate, d) -> None:
        kind = str(getattr(d, "target_type", "") or "").lower()
        if "decoy" in kind:
            c.decoy_votes += 1
        elif "ground" in kind or "vehicle" in kind:
            c.ground_votes += 1

    def _maybe_start_candidate(self, ds) -> None:
        d = self._best_detection(ds)
        if d is None:
            return

        lat = float(d.target_lat)
        lon = float(d.target_lon)

        if self._in_cooldown(lat, lon) or self._near_known_task(lat, lon):
            return

        conf = float(getattr(d, "confidence", 0.5))
        c = Candidate(
            started_at=self._time,
            last_seen=self._time,
            lat=lat,
            lon=lon,
            seen_count=1,
            conf_sum=conf,
            samples=[(self._time, lat, lon)],
        )
        self._vote_type(c, d)

        self._candidate = c
        self._last_local_detection = (self._time, lat, lon, conf)

    def _update_candidate(self, ds) -> None:
        c = self._candidate
        if c is None:
            return

        d = self._best_detection(ds, (c.lat, c.lon))
        if d is None:
            if self._time - c.last_seen > 2.0:
                self._reject_candidate(short=True)
            return

        lat = float(d.target_lat)
        lon = float(d.target_lon)

        # V9 used a 300 m association gate.  In a scene with many vehicles that
        # can splice two nearby tracks together and manufacture an unreal speed.
        # 190 m still tolerates the train detector's position noise while making
        # identity switches much less likely.
        if _dist_m(lat, lon, c.lat, c.lon) > 190.0:
            if self._time - c.last_seen > 2.0:
                self._reject_candidate(short=True)
            return

        conf = float(getattr(d, "confidence", 0.5))
        c.samples.append((self._time, lat, lon))
        if len(c.samples) > 400:
            c.samples.pop(0)

        c.seen_count += 1
        c.conf_sum += conf
        c.last_seen = self._time
        c.lat = 0.84 * c.lat + 0.16 * lat
        c.lon = 0.84 * c.lon + 0.16 * lon
        self._vote_type(c, d)
        self._last_local_detection = (self._time, lat, lon, conf)

    def _motion_fit(
        self,
        c: Candidate,
    ) -> Optional[Tuple[float, float, float, float, int, float]]:
        """
        0.75-s bins -> straight-line east/north regression.

        Returns:
          speed, ve, vn, rms, bin_count, net_speed
        """
        if len(c.samples) < 16:
            return None

        t0, ref_lat, ref_lon = c.samples[0]
        bins: Dict[int, List[Tuple[float, float, float]]] = {}

        for t, lat, lon in c.samples:
            k = int((t - t0) / 0.75)
            east = (lon - ref_lon) * _m_per_deg_lon(ref_lat)
            north = (lat - ref_lat) * M_PER_DEG_LAT
            bins.setdefault(k, []).append((t - t0, east, north))

        pts: List[Tuple[float, float, float]] = []
        for k in sorted(bins):
            arr = bins[k]
            pts.append((
                sum(v[0] for v in arr) / len(arr),
                sum(v[1] for v in arr) / len(arr),
                sum(v[2] for v in arr) / len(arr),
            ))

        if len(pts) < 6:
            return None

        ts = [p[0] for p in pts]

        def fit_axis(axis: int) -> Tuple[float, float]:
            ys = [p[axis] for p in pts]
            mt = sum(ts) / len(ts)
            my = sum(ys) / len(ys)
            den = sum((x - mt) ** 2 for x in ts)
            if den < 1e-9:
                return 0.0, my
            slope = sum(
                (x - mt) * (y - my)
                for x, y in zip(ts, ys)
            ) / den
            return slope, my - slope * mt

        ve, be = fit_axis(1)
        vn, bn = fit_axis(2)

        err2 = []
        for tt, ee, nn in pts:
            err2.append(
                (ee - (be + ve * tt)) ** 2
                + (nn - (bn + vn * tt)) ** 2
            )
        rms = math.sqrt(sum(err2) / max(1, len(err2)))

        dt_net = max(1.0, pts[-1][0] - pts[0][0])
        net_speed = math.hypot(
            pts[-1][1] - pts[0][1],
            pts[-1][2] - pts[0][2],
        ) / dt_net

        return math.hypot(ve, vn), ve, vn, rms, len(pts), net_speed

    def _classify_candidate(self) -> None:
        c = self._candidate
        if c is None or c.real_ready:
            return

        age = c.last_seen - c.started_at
        votes = c.ground_votes + c.decoy_votes

        # target_type is noisy, so it is never accepted as one-frame truth.
        # Repeated decoy votes are nevertheless useful as a veto.
        if votes >= 10 and c.decoy_votes / max(1, votes) >= 0.65:
            self._reject_candidate()
            return

        fit = self._motion_fit(c)
        if fit is None:
            return

        speed, ve, vn, rms, n_bins, net_speed = fit
        c.speed_mps = speed
        c.fit_rms_m = rms
        c.v_east = ve
        c.v_north = vn

        ground_ratio = c.ground_votes / max(1, votes)
        strong_ground = votes >= 10 and ground_ratio >= 0.72
        moderate_ground = votes >= 8 and ground_ratio >= 0.62

        # Train-mode moving decoys are injected at 5 m/s.  V9's late 5.35 m/s
        # fallback promoted too many ambiguous movers, so V10 requires either
        # clearly faster coherent motion or strong repeated ground-vehicle votes.
        if age >= 8.5 and n_bins >= 9:
            if speed >= 6.15 and net_speed >= 5.15 and rms <= 55.0:
                c.real_ready = True
                return
            if strong_ground and speed >= 5.75 and net_speed >= 5.00 and rms <= 55.0:
                c.real_ready = True
                return

        if age >= 11.5 and n_bins >= 11:
            if strong_ground and speed >= 5.55 and net_speed >= 4.90 and rms <= 62.0:
                c.real_ready = True
                return
            if speed <= 5.25 and rms <= 65.0:
                self._reject_candidate()
                return

        if age >= 15.5:
            if moderate_ground and speed >= 5.72 and net_speed >= 4.95 and rms <= 62.0:
                c.real_ready = True
            else:
                self._reject_candidate()

    def _reject_candidate(self, short: bool = False) -> None:
        c = self._candidate
        if c is not None:
            self._cooldowns.append(
                Cooldown(
                    c.lat,
                    c.lon,
                    self._time + (8.0 if short else 18.0),
                )
            )
        self._candidate = None

    def _acquire_commands(self, obs: SwarmObs) -> List[Command]:
        c = self._candidate
        if c is None:
            return self._search_or_jam_commands(obs)

        # Keep a moderate standoff.  Very small loiters caused the tight curls
        # visible in earlier versions.
        cmds = self._navigate(
            obs,
            (c.lat, c.lon),
            mode="acquire",
            loiter_radius=230.0,
        )
        cmds.append(set_gimbal_fov(self._acquire_fov))

        pan, tilt = self._gimbal_to(obs, c.lat, c.lon)
        cmds.append(point_gimbal(pan, tilt))
        return cmds

    # ------------------------------------------------------------------
    # Coalition
    # ------------------------------------------------------------------

    def _fresh_peers(self, obs: SwarmObs) -> List[Peer]:
        peers = [
            p for p in self._peers.values()
            if self._time - p.last_seen <= 2.5
            and p.idx != self._idx
        ]

        peers.sort(
            key=lambda p: (
                1 if p.state == "T" else 0,
                _dist_m(obs.self.lat, obs.self.lon, p.lat, p.lon),
                p.idx,
            )
        )
        return peers

    def _choose_two_members(
        self,
        obs: SwarmObs,
    ) -> Optional[Tuple[int, int]]:
        fresh = self._fresh_peers(obs)

        # A task broadcast is only useful if it can actually reach the selected
        # aircraft.  V9 fell back to fresh-but-distant peers when <2 were in
        # radio range, creating nominal coalitions that could not close K=3.
        pool = []
        for p in fresh:
            if _dist_m(obs.self.lat, obs.self.lon, p.lat, p.lon) > 900.0:
                continue
            # Do not steal an aircraft already tracking another task.  Existing
            # members of our current task are allowed during owner reselection.
            if p.state == "T":
                if self._task is None or p.idx not in self._task.members:
                    continue
            pool.append(p)

        pool.sort(
            key=lambda p: (
                1 if p.state == "T" else 0,
                1 if p.state == "A" else 0,
                _dist_m(obs.self.lat, obs.self.lon, p.lat, p.lon),
                p.idx,
            )
        )

        chosen: List[int] = []
        for p in pool:
            if p.idx in chosen:
                continue
            chosen.append(p.idx)
            if len(chosen) == 2:
                return chosen[0], chosen[1]
        return None

    def _try_launch_ready_candidate(self, obs: SwarmObs) -> None:
        c = self._candidate
        if c is None or not c.real_ready:
            return

        pair = self._choose_two_members(obs)
        if pair is None:
            # Keep observing for a short time while nearby helpers move into
            # range.  Never fabricate a coalition with unreachable aircraft.
            if self._time - c.started_at > 22.0:
                self._reject_candidate()
            return

        self._task_seq = (self._task_seq + 1) % 1000
        members = (self._idx, pair[0], pair[1])
        self._task = Task(
            owner_idx=self._idx,
            seq=self._task_seq,
            members=members,
            lat=c.lat,
            lon=c.lon,
            v_east=c.v_east,
            v_north=c.v_north,
            created_at=self._time,
            last_update=self._time,
            last_local_seen=self._time,
        )

        self._task_destroy_count_at_start = self._n_destroyed
        self._ack_seen = {self._idx: self._time}
        self._visual_seen = {self._idx: self._time}
        self._visual_hits = {self._idx: 1}
        self._candidate = None
        self._queue_task(force=True)

    def _predict_task(self, t: Optional[Task] = None) -> Tuple[float, float]:
        if t is None:
            t = self._task
        if t is None:
            if self._area is not None:
                return self._area.mid_lat, self._area.mid_lon
            return 27.0, 125.0

        dt = min(3.0, max(0.0, self._time - t.last_update))
        return _offset(
            t.lat,
            t.lon,
            t.v_east * dt,
            t.v_north * dt,
        )

    def _queue_task(self, force: bool = False) -> None:
        t = self._task
        if t is None or self._idx != t.owner_idx:
            return
        if not force and self._time - self._last_task_tx_t < 0.55:
            return

        self._last_task_tx_t = self._time
        _, m1, m2 = t.members

        msg = (
            f"T,{t.owner_idx},{t.seq},{m1},{m2},"
            f"{t.lat:.5f},{t.lon:.5f},"
            f"{t.v_east:.1f},{t.v_north:.1f}"
        )
        self._queue_msg(
            msg,
            priority=True,
            replace_prefix=f"T,{t.owner_idx},{t.seq},",
        )

    def _queue_ack(self, t: Task) -> None:
        # ACK is persistent but intentionally slower than V9 so follower visual
        # packets and heartbeat together remain below the 4 Hz communication cap.
        if self._time - self._last_ack_tx_t < 1.20:
            return
        self._last_ack_tx_t = self._time
        self._queue_msg(
            f"A,{t.owner_idx},{t.seq},{self._idx}",
            priority=True,
        )

    def _queue_visual(self, t: Task, lat: float, lon: float) -> None:
        if self._time - self._last_visual_tx_t < 0.65:
            return
        self._last_visual_tx_t = self._time
        self._queue_msg(
            f"V,{t.owner_idx},{t.seq},{self._idx},{lat:.5f},{lon:.5f}",
            priority=True,
            replace_prefix=f"V,{t.owner_idx},{t.seq},{self._idx},",
        )

    def _maintain_owner_members(self, obs: SwarmObs) -> None:
        t = self._task
        if t is None or self._idx != t.owner_idx:
            return

        age = self._time - t.created_at
        # Give the initial task broadcasts time to arrive before declaring an
        # ACK missing.  Without this guard, a freshly created task sees the
        # default -999 timestamp and immediately churns its member list.
        missing_ack = [] if age < 4.0 else [
            m for m in t.members[1:]
            if self._time - self._ack_seen.get(m, -999.0) > 3.5
        ]
        missing_visual = [
            m for m in t.members[1:]
            if self._time - self._visual_seen.get(m, -999.0) > 4.0
        ]

        # Missing ACK means task delivery failed.  Visual acquisition gets much
        # longer because a selected follower may need ~20 s to fly to target.
        need_reselect = bool(missing_ack) or (age >= 28.0 and bool(missing_visual))
        if not need_reselect:
            return
        if self._time - self._last_reselect_t < 5.0:
            return

        self._last_reselect_t = self._time
        pair = self._choose_two_members(obs)
        if pair is None:
            return

        new_members = (self._idx, pair[0], pair[1])
        if new_members != t.members:
            t.members = new_members
            self._ack_seen = {self._idx: self._time}
            self._visual_seen = {self._idx: self._time}
            self._visual_hits = {self._idx: max(1, self._visual_hits.get(self._idx, 1))}

        self._queue_task(force=True)

    def _update_task_from_detections(self, ds) -> None:
        t = self._task
        if t is None:
            return

        pred = self._predict_task(t)
        d = self._best_detection(ds, pred)
        if d is None:
            return

        lat = float(d.target_lat)
        lon = float(d.target_lon)
        # Tighter than V9's 300 m gate: task tracking must stay on the same
        # physical mover, especially in dense true+decoy traffic.
        if _dist_m(lat, lon, pred[0], pred[1]) > 240.0:
            return

        conf = float(getattr(d, "confidence", 0.5))
        self._last_local_detection = (self._time, lat, lon, conf)
        t.last_local_seen = self._time
        self._visual_seen[self._idx] = self._time
        self._visual_hits[self._idx] = self._visual_hits.get(self._idx, 0) + 1

        if self._idx == t.owner_idx:
            old_lat, old_lon, old_t = t.lat, t.lon, t.last_update
            t.lat = 0.72 * t.lat + 0.28 * lat
            t.lon = 0.72 * t.lon + 0.28 * lon
            t.last_update = self._time

            dtm = max(0.5, self._time - old_t)
            ve = (t.lon - old_lon) * _m_per_deg_lon(t.lat) / dtm
            vn = (t.lat - old_lat) * M_PER_DEG_LAT / dtm
            t.v_east = 0.90 * t.v_east + 0.10 * ve
            t.v_north = 0.90 * t.v_north + 0.10 * vn
        elif self._idx in t.members:
            self._queue_visual(t, lat, lon)

    def _track_slot_goal(self) -> Tuple[float, float]:
        assert self._area is not None
        t = self._task
        if t is None:
            return self._area.mid_lat, self._area.mid_lon

        center = self._predict_task(t)
        try:
            slot = t.members.index(self._idx)
        except ValueError:
            slot = 0

        # Acquisition formation: r=200 m, pairwise ~346 m, wide FOV.
        # Hold formation: r=260 m, pairwise ~450 m.  The close seek geometry
        # makes it much easier for a follower with a noisy shared position to
        # get its first local detection without violating the 200 m penalty rule.
        locally_locked = self._time - self._visual_seen.get(self._idx, -999.0) <= 2.2
        radius = 260.0 if locally_locked else 200.0
        angles = (0.0, 120.0, 240.0)

        raw = []
        for deg in angles:
            a = math.radians(deg)
            raw.append(
                _offset(
                    center[0], center[1],
                    radius * math.sin(a),
                    radius * math.cos(a),
                )
            )

        a = self._area
        margin = 190.0
        low_lat = a.lat_min + margin / M_PER_DEG_LAT
        high_lat = a.lat_max - margin / M_PER_DEG_LAT
        low_lon = a.lon_min + margin / _m_per_deg_lon(a.mid_lat)
        high_lon = a.lon_max - margin / _m_per_deg_lon(a.mid_lat)

        shift_lat = 0.0
        min_lat = min(p[0] for p in raw)
        max_lat = max(p[0] for p in raw)
        if min_lat < low_lat:
            shift_lat += low_lat - min_lat
        if max_lat + shift_lat > high_lat:
            shift_lat += high_lat - (max_lat + shift_lat)

        shift_lon = 0.0
        min_lon = min(p[1] for p in raw)
        max_lon = max(p[1] for p in raw)
        if min_lon < low_lon:
            shift_lon += low_lon - min_lon
        if max_lon + shift_lon > high_lon:
            shift_lon += high_lon - (max_lon + shift_lon)

        return self._clamp_area(
            raw[slot][0] + shift_lat,
            raw[slot][1] + shift_lon,
            margin_m=190.0,
        )

    def _track_commands(self, obs: SwarmObs) -> List[Command]:
        t = self._task
        if t is None:
            return self._search_or_jam_commands(obs)

        locked = self._time - self._visual_seen.get(self._idx, -999.0) <= 2.2
        cmds = self._navigate(
            obs,
            self._track_slot_goal(),
            mode="track",
            loiter_radius=90.0 if locked else 120.0,
        )

        target = self._predict_task(t)
        cmds.append(set_gimbal_fov(self._track_fov if locked else 50.0))
        pan, tilt = self._gimbal_to(obs, target[0], target[1])
        cmds.append(point_gimbal(pan, tilt))

        self._maybe_report(cmds)
        return cmds

    def _maybe_report(self, cmds: List[Command]) -> None:
        t = self._task
        if t is None or self._idx != t.owner_idx:
            return

        # Report only when the nominal coalition has become a real visual
        # coalition: each member must have repeated and fresh sightings.
        for m in t.members:
            if self._time - self._visual_seen.get(m, -999.0) > 2.2:
                return
            if self._visual_hits.get(m, 0) < 3:
                return

        if self._time - t.created_at < 2.0:
            return
        if self._time - self._last_report_t < 1.0:
            return
        if self._last_local_detection is None:
            return

        td, lat, lon, conf = self._last_local_detection
        if self._time - td > 0.70 or conf < 0.12:
            return

        pred = self._predict_task(t)
        if _dist_m(lat, lon, pred[0], pred[1]) > 180.0:
            return

        # Use the owner's fresh local observation.  This avoids the huge V9
        # error produced by reporting an incorrect/lagging task centre.
        cmds.append(report_target(lat, lon))
        self._last_report_t = self._time

    def _maybe_finish_task(self) -> None:
        t = self._task
        if t is None:
            return

        age = self._time - t.created_at

        if (
            self._n_destroyed > self._task_destroy_count_at_start
            and age >= 18.0
        ):
            if self._idx == t.owner_idx:
                self._queue_msg(f"D,{t.owner_idx},{t.seq}", priority=True)
            self._task = None
            return

        if self._idx == t.owner_idx:
            # Owner vision is indispensable; grace is only a few seconds.
            if age > 8.0 and self._time - t.last_local_seen > 4.0:
                self._queue_msg(f"D,{t.owner_idx},{t.seq}", priority=True)
                self._task = None
                return

            # A reachable follower may need ~20-25 s of transit.  Do not abort
            # before it has a fair chance to enter the seek formation.
            if age > 36.0:
                fresh_visuals = sum(
                    1 for m in t.members
                    if self._time - self._visual_seen.get(m, -999.0) <= 2.5
                )
                if fresh_visuals < 3:
                    self._queue_msg(f"D,{t.owner_idx},{t.seq}", priority=True)
                    self._task = None
                    return

            if age > 62.0:
                self._queue_msg(f"D,{t.owner_idx},{t.seq}", priority=True)
                self._task = None
                return
        else:
            if (
                self._time - t.last_update > 9.0
                and self._time - t.last_local_seen > 3.5
            ):
                self._task = None

    def _zone_rect(self, z, pad_m: float = 0.0) -> Tuple[float, float, float, float]:
        (lat0, lon0), (lat1, lon1) = z.bbox
        if pad_m <= 0.0:
            return lat0, lat1, lon0, lon1

        ref = 0.5 * (lat0 + lat1)
        dlat = pad_m / M_PER_DEG_LAT
        dlon = pad_m / _m_per_deg_lon(ref)
        return (
            lat0 - dlat,
            lat1 + dlat,
            lon0 - dlon,
            lon1 + dlon,
        )

    def _no_fly_safe_goal(
        self,
        obs: SwarmObs,
        desired: Tuple[float, float],
    ) -> Tuple[float, float]:
        """Horizontal avoidance for task-3 hard zones at fixed 500 m altitude."""
        cur = (obs.self.lat, obs.self.lon)

        # Persist one detour waypoint; constantly switching corners produces
        # curls and can send a fixed-wing back through the lethal rectangle.
        if self._no_fly_detour is not None:
            if _dist_m(cur[0], cur[1], self._no_fly_detour[0], self._no_fly_detour[1]) > 125.0:
                return self._no_fly_detour
            self._no_fly_detour = None

        hard = []
        for z in getattr(obs.briefing, "approximate_zones", ()) or ():
            if getattr(z, "kind", "") not in ("air_defense", "no_fly"):
                continue
            # Briefing bbox is already conservative; add only a modest fixed-wing
            # margin rather than V8's/V9's very large extra expansion.
            hard.append(self._zone_rect(z, pad_m=90.0))

        for r in hard:
            lat0, lat1, lon0, lon1 = r
            ref = 0.5 * (lat0 + lat1)

            # If already in the conservative box, leave through the nearest side
            # immediately.  This is the only case where the first segment begins
            # inside the rectangle.
            if _point_in_rect(cur, r):
                edge_pad = 130.0
                dlat = edge_pad / M_PER_DEG_LAT
                dlon = edge_pad / _m_per_deg_lon(ref)
                candidates = [
                    self._clamp_area(lat0 - dlat, cur[1], margin_m=180.0),
                    self._clamp_area(lat1 + dlat, cur[1], margin_m=180.0),
                    self._clamp_area(cur[0], lon0 - dlon, margin_m=180.0),
                    self._clamp_area(cur[0], lon1 + dlon, margin_m=180.0),
                ]
                self._no_fly_detour = min(
                    candidates,
                    key=lambda p: _dist_m(cur[0], cur[1], p[0], p[1]),
                )
                return self._no_fly_detour

            if not _segment_intersects_rect(cur, desired, r):
                continue

            corner_pad = 140.0
            dlat = corner_pad / M_PER_DEG_LAT
            dlon = corner_pad / _m_per_deg_lon(ref)
            corners = [
                self._clamp_area(lat0 - dlat, lon0 - dlon, margin_m=180.0),
                self._clamp_area(lat0 - dlat, lon1 + dlon, margin_m=180.0),
                self._clamp_area(lat1 + dlat, lon0 - dlon, margin_m=180.0),
                self._clamp_area(lat1 + dlat, lon1 + dlon, margin_m=180.0),
            ]

            # Prefer a corner reachable without crossing the hard rectangle.
            safe_corners = [
                p for p in corners
                if not _segment_intersects_rect(cur, p, r)
            ]
            if not safe_corners:
                safe_corners = corners

            self._no_fly_detour = min(
                safe_corners,
                key=lambda p: (
                    _dist_m(cur[0], cur[1], p[0], p[1])
                    + _dist_m(p[0], p[1], desired[0], desired[1])
                ),
            )
            return self._no_fly_detour

        return desired

    def _safe_altitude(
        self,
        obs: SwarmObs,
        goal: Tuple[float, float],
    ) -> float:
        # Task 3 flight altitude is fixed.  Static threats are handled only by
        # the horizontal detour above.
        return self._base_alt

    def _separate(
        self,
        obs: SwarmObs,
        goal: Tuple[float, float],
    ) -> Tuple[float, float]:
        east_push = 0.0
        north_push = 0.0

        for p in self._peers.values():
            if self._time - p.last_seen > 1.8:
                continue

            d = _dist_m(
                obs.self.lat,
                obs.self.lon,
                p.lat,
                p.lon,
            )
            if d >= 360.0 or d < 1.0:
                continue

            ref = 0.5 * (obs.self.lat + p.lat)
            de = (obs.self.lon - p.lon) * _m_per_deg_lon(ref)
            dn = (obs.self.lat - p.lat) * M_PER_DEG_LAT
            n = max(1.0, math.hypot(de, dn))

            gain = 230.0 * (360.0 - d) / 360.0
            east_push += gain * de / n
            north_push += gain * dn / n

        if abs(east_push) + abs(north_push) < 1.0:
            return goal

        return self._clamp_area(
            *_offset(
                goal[0],
                goal[1],
                east_push,
                north_push,
            ),
            margin_m=180.0,
        )

    def _navigate(
        self,
        obs: SwarmObs,
        desired: Tuple[float, float],
        *,
        mode: str,
        loiter_radius: Optional[float] = None,
    ) -> List[Command]:
        goal = self._clamp_area(*desired, margin_m=180.0)
        goal = self._no_fly_safe_goal(obs, goal)
        goal = self._separate(obs, goal)

        if mode == "track":
            speed = self._track_speed
            period = 0.70
            lr = 90.0 if loiter_radius is None else loiter_radius
        elif mode == "acquire":
            speed = self._acquire_speed
            period = 1.0
            lr = 230.0 if loiter_radius is None else loiter_radius
        else:
            speed = self._search_speed
            period = 1.5
            lr = 80.0 if loiter_radius is None else loiter_radius

        need = (
            self._last_nav_goal is None
            or self._last_nav_mode != mode
            or self._time - self._last_nav_t >= period
            or _dist_m(
                goal[0], goal[1],
                self._last_nav_goal[0], self._last_nav_goal[1],
            ) >= 180.0
        )

        if not need:
            return []

        self._last_nav_t = self._time
        self._last_nav_goal = goal
        self._last_nav_mode = mode
        self._last_nav_alt = self._base_alt

        return [
            fly_to(
                goal[0], goal[1],
                alt=self._base_alt,
                speed=speed,
                loiter_radius=lr,
                turn_direction="right",
            )
        ]

    def _gimbal_to(
        self,
        obs: SwarmObs,
        lat: float,
        lon: float,
    ) -> Tuple[float, float]:
        b = _bearing(
            obs.self.lat,
            obs.self.lon,
            lat,
            lon,
        )
        pan = _wrap180(b - obs.self.heading_deg)
        pan = max(-179.0, min(179.0, pan))

        horizontal = max(
            1.0,
            _dist_m(
                obs.self.lat,
                obs.self.lon,
                lat,
                lon,
            ),
        )
        vertical = max(1.0, obs.self.alt)
        tilt = -math.degrees(math.atan2(vertical, horizontal))
        tilt = max(-86.0, min(-15.0, tilt))
        return pan, tilt

    # ------------------------------------------------------------------
    # Jam
    # ------------------------------------------------------------------

    def _update_jam(self, obs: SwarmObs) -> None:
        if obs.self.jammed and not self._was_jammed:
            self._jam_escape_goal = None
        if not obs.self.jammed:
            self._jam_escape_goal = None
        self._was_jammed = bool(obs.self.jammed)

    def _jam_escape(self, obs: SwarmObs) -> Tuple[float, float]:
        if self._jam_escape_goal is not None:
            return self._jam_escape_goal

        params = getattr(obs.briefing, "params", {}) or {}
        q = params.get("comm_jam_random", {}) or {}
        radius = float(q.get("radius_m") or 350.0)

        run = max(650.0, 1.8 * radius)
        h = math.radians(obs.self.heading_deg)

        self._jam_escape_goal = self._clamp_area(
            *_offset(
                obs.self.lat,
                obs.self.lon,
                math.sin(h) * run,
                math.cos(h) * run,
            ),
            margin_m=180.0,
        )
        return self._jam_escape_goal

    # ------------------------------------------------------------------
    # Communication
    # ------------------------------------------------------------------

    def _queue_msg(
        self,
        payload: str,
        *,
        priority: bool = False,
        replace_prefix: Optional[str] = None,
    ) -> None:
        if len(payload.encode("utf-8")) > 50:
            return

        if replace_prefix is not None:
            self._outbox = [
                x for x in self._outbox
                if not x.startswith(replace_prefix)
            ]

        if payload in self._outbox:
            return

        if priority:
            self._outbox.insert(0, payload)
        else:
            self._outbox.append(payload)

    def _queue_periodic_messages(self, obs: SwarmObs) -> None:
        if self._time - self._last_hb_t >= 0.9:
            self._last_hb_t = self._time
            self._queue_msg(
                f"H,{self._idx},{obs.self.lat:.5f},{obs.self.lon:.5f},{self._state}",
                replace_prefix=f"H,{self._idx},",
            )

        if self._task is not None:
            if self._idx == self._task.owner_idx:
                self._queue_task()
            elif self._idx in self._task.members:
                self._queue_ack(self._task)

    def _flush_message(self, cmds: List[Command]) -> None:
        if self._time < self._next_comm_t or not self._outbox:
            return

        payload = self._outbox.pop(0)
        cmds.append(broadcast(payload))
        self._next_comm_t = self._time + 0.27  # below 4 Hz

    def _ingest_messages(self, obs: SwarmObs) -> None:
        for m in getattr(obs, "comm_inbox", ()) or ():
            parts = str(m.payload).split(",")
            if not parts:
                continue

            try:
                kind = parts[0]

                if kind == "H" and len(parts) >= 5:
                    self._peers[m.sender_uid] = Peer(
                        uid=m.sender_uid,
                        idx=int(parts[1]),
                        lat=float(parts[2]),
                        lon=float(parts[3]),
                        state=parts[4],
                        last_seen=self._time,
                    )

                elif kind == "T" and len(parts) >= 9:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    m1 = int(parts[3])
                    m2 = int(parts[4])
                    lat = float(parts[5])
                    lon = float(parts[6])
                    ve = float(parts[7])
                    vn = float(parts[8])
                    self._accept_task(
                        owner,
                        seq,
                        (owner, m1, m2),
                        lat,
                        lon,
                        ve,
                        vn,
                    )

                elif kind == "A" and len(parts) >= 4:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    idx = int(parts[3])

                    t = self._task
                    if (
                        t is not None
                        and self._idx == t.owner_idx == owner
                        and t.seq == seq
                        and idx in t.members
                    ):
                        self._ack_seen[idx] = self._time

                elif kind == "V" and len(parts) >= 6:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    idx = int(parts[3])
                    lat = float(parts[4])
                    lon = float(parts[5])

                    t = self._task
                    if (
                        t is not None
                        and self._idx == t.owner_idx == owner
                        and t.seq == seq
                        and idx in t.members
                        and _dist_m(lat, lon, t.lat, t.lon) <= 220.0
                    ):
                        self._ack_seen[idx] = self._time
                        self._visual_seen[idx] = self._time
                        self._visual_hits[idx] = self._visual_hits.get(idx, 0) + 1

                        # Very weak multi-view fusion; owner local vision remains
                        # the main target-state source.
                        t.lat = 0.97 * t.lat + 0.03 * lat
                        t.lon = 0.97 * t.lon + 0.03 * lon

                elif kind == "D" and len(parts) >= 3:
                    owner = int(parts[1])
                    seq = int(parts[2])
                    self._known_tasks.pop((owner, seq), None)

                    if (
                        self._task is not None
                        and self._task.owner_idx == owner
                        and self._task.seq == seq
                    ):
                        self._task = None
                        self._candidate = None

            except (ValueError, TypeError, IndexError):
                continue

    def _accept_task(
        self,
        owner: int,
        seq: int,
        members: Tuple[int, int, int],
        lat: float,
        lon: float,
        ve: float,
        vn: float,
    ) -> None:
        key = (owner, seq)
        old = self._known_tasks.get(key)

        task = Task(
            owner_idx=owner,
            seq=seq,
            members=members,
            lat=lat,
            lon=lon,
            v_east=ve,
            v_north=vn,
            created_at=old.created_at if old is not None else self._time,
            last_update=self._time,
        )
        self._known_tasks[key] = task

        # Update same task.
        if (
            self._task is not None
            and self._task.owner_idx == owner
            and self._task.seq == seq
        ):
            if self._idx not in members and self._idx != owner:
                self._task = None
                return

            self._task.members = members
            self._task.lat = lat
            self._task.lon = lon
            self._task.v_east = ve
            self._task.v_north = vn
            self._task.last_update = self._time
            return

        # Only explicitly selected followers adopt.
        if self._idx not in members or self._idx == owner:
            return

        # Deterministic conflict resolution for simultaneous nearby owners.
        # Lower (owner, seq) wins.  This prevents one aircraft from being
        # nominally selected by two coalitions while acknowledging neither
        # consistently.
        if self._task is not None and self._time - self._task.last_update <= 5.0:
            cur_key = (self._task.owner_idx, self._task.seq)
            new_key = (owner, seq)
            if new_key >= cur_key:
                return
            if self._idx == self._task.owner_idx:
                self._queue_msg(
                    f"D,{self._task.owner_idx},{self._task.seq}",
                    priority=True,
                )

        self._task = task
        self._candidate = None
        self._task_destroy_count_at_start = self._n_destroyed
        self._visual_seen[self._idx] = -999.0
        self._visual_hits[self._idx] = 0
        self._queue_ack(task)

    # ------------------------------------------------------------------
    # Score and pruning
    # ------------------------------------------------------------------

    def _update_score(self, obs: SwarmObs) -> None:
        score = getattr(obs.briefing, "score_view", None)
        if score is not None:
            self._n_destroyed = int(
                getattr(score, "n_destroyed", self._n_destroyed)
            )

    def _prune(self) -> None:
        self._cooldowns = [
            c for c in self._cooldowns
            if c.expires_at > self._time
        ]

        self._peers = {
            uid: p
            for uid, p in self._peers.items()
            if self._time - p.last_seen <= 6.0
        }

        self._known_tasks = {
            k: t
            for k, t in self._known_tasks.items()
            if self._time - t.last_update <= 12.0
        }


__all__ = ["HF2026V10Agent"]
